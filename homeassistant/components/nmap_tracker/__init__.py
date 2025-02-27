"""The Nmap Tracker integration."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
import logging

import aiohttp
from getmac import get_mac_address
from mac_vendor_lookup import AsyncMacLookup
from nmap import PortScanner, PortScannerError

from homeassistant.components.device_tracker.const import CONF_SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EXCLUDE, CONF_HOSTS, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util

from .const import (
    CONF_HOME_INTERVAL,
    CONF_OPTIONS,
    DOMAIN,
    NMAP_TRACKED_DEVICES,
    PLATFORMS,
    TRACKER_SCAN_INTERVAL,
)

# Some version of nmap will fail with 'Assertion failed: htn.toclock_running == true (Target.cc: stopTimeOutClock: 503)\n'
NMAP_TRANSIENT_FAILURE = "Assertion failed: htn.toclock_running == true"
MAX_SCAN_ATTEMPTS = 16
OFFLINE_SCANS_TO_MARK_UNAVAILABLE = 3


def short_hostname(hostname):
    """Return the first part of the hostname."""
    if hostname is None:
        return None
    return hostname.split(".")[0]


def human_readable_name(hostname, vendor, mac_address):
    """Generate a human readable name."""
    if hostname:
        return short_hostname(hostname)
    if vendor:
        return f"{vendor} {mac_address[-8:]}"
    return f"Nmap Tracker {mac_address}"


@dataclass
class NmapDevice:
    """Class for keeping track of an nmap tracked device."""

    mac_address: str
    hostname: str
    name: str
    ipv4: str
    manufacturer: str
    reason: str
    last_update: datetime.datetime
    offline_scans: int


class NmapTrackedDevices:
    """Storage class for all nmap trackers."""

    def __init__(self) -> None:
        """Initialize the data."""
        self.tracked: dict = {}
        self.ipv4_last_mac: dict = {}
        self.config_entry_owner: dict = {}


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nmap Tracker from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    devices = domain_data.setdefault(NMAP_TRACKED_DEVICES, NmapTrackedDevices())
    scanner = domain_data[entry.entry_id] = NmapDeviceScanner(hass, entry, devices)
    await scanner.async_setup()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    hass.config_entries.async_setup_platforms(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        _async_untrack_devices(hass, entry)
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


@callback
def _async_untrack_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove tracking for devices owned by this config entry."""
    devices = hass.data[DOMAIN][NMAP_TRACKED_DEVICES]
    remove_mac_addresses = [
        mac_address
        for mac_address, entry_id in devices.config_entry_owner.items()
        if entry_id == entry.entry_id
    ]
    for mac_address in remove_mac_addresses:
        if device := devices.tracked.pop(mac_address, None):
            devices.ipv4_last_mac.pop(device.ipv4, None)
        del devices.config_entry_owner[mac_address]


def signal_device_update(mac_address) -> str:
    """Signal specific per nmap tracker entry to signal updates in device."""
    return f"{DOMAIN}-device-update-{mac_address}"


class NmapDeviceScanner:
    """This class scans for devices using nmap."""

    def __init__(self, hass, entry, devices):
        """Initialize the scanner."""
        self.devices = devices
        self.home_interval = None

        self._hass = hass
        self._entry = entry

        self._scan_lock = None
        self._stopping = False
        self._scanner = None

        self._entry_id = entry.entry_id
        self._hosts = None
        self._options = None
        self._exclude = None
        self._scan_interval = None

        self._known_mac_addresses = {}
        self._finished_first_scan = False
        self._last_results = []
        self._mac_vendor_lookup = None

    async def async_setup(self):
        """Set up the tracker."""
        config = self._entry.options
        self._scan_interval = timedelta(
            seconds=config.get(CONF_SCAN_INTERVAL, TRACKER_SCAN_INTERVAL)
        )
        hosts_list = cv.ensure_list_csv(config[CONF_HOSTS])
        self._hosts = [host for host in hosts_list if host != ""]
        excludes_list = cv.ensure_list_csv(config[CONF_EXCLUDE])
        self._exclude = [exclude for exclude in excludes_list if exclude != ""]
        self._options = config[CONF_OPTIONS]
        self.home_interval = timedelta(
            minutes=cv.positive_int(config[CONF_HOME_INTERVAL])
        )
        self._scan_lock = asyncio.Lock()
        if self._hass.state == CoreState.running:
            await self._async_start_scanner()
            return

        self._entry.async_on_unload(
            self._hass.bus.async_listen(
                EVENT_HOMEASSISTANT_STARTED, self._async_start_scanner
            )
        )
        registry = er.async_get(self._hass)
        self._known_mac_addresses = {
            entry.unique_id: entry.original_name
            for entry in registry.entities.values()
            if entry.config_entry_id == self._entry_id
        }

    @property
    def signal_device_new(self) -> str:
        """Signal specific per nmap tracker entry to signal new device."""
        return f"{DOMAIN}-device-new-{self._entry_id}"

    @property
    def signal_device_missing(self) -> str:
        """Signal specific per nmap tracker entry to signal a missing device."""
        return f"{DOMAIN}-device-missing-{self._entry_id}"

    @callback
    def _async_get_vendor(self, mac_address):
        """Lookup the vendor."""
        oui = self._mac_vendor_lookup.sanitise(mac_address)[:6]
        return self._mac_vendor_lookup.prefixes.get(oui)

    @callback
    def _async_stop(self):
        """Stop the scanner."""
        self._stopping = True

    async def _async_start_scanner(self, *_):
        """Start the scanner."""
        self._entry.async_on_unload(self._async_stop)
        self._entry.async_on_unload(
            async_track_time_interval(
                self._hass,
                self._async_scan_devices,
                self._scan_interval,
            )
        )
        self._mac_vendor_lookup = AsyncMacLookup()
        with contextlib.suppress((asyncio.TimeoutError, aiohttp.ClientError)):
            # We don't care if this fails since it only
            # improves the data when we don't have it from nmap
            await self._mac_vendor_lookup.load_vendors()
        self._hass.async_create_task(self._async_scan_devices())

    def _build_options(self):
        """Build the command line and strip out last results that do not need to be updated."""
        options = self._options
        if self.home_interval:
            boundary = dt_util.now() - self.home_interval
            last_results = [
                device for device in self._last_results if device.last_update > boundary
            ]
            if last_results:
                exclude_hosts = self._exclude + [device.ipv4 for device in last_results]
            else:
                exclude_hosts = self._exclude
        else:
            last_results = []
            exclude_hosts = self._exclude
        if exclude_hosts:
            options += f" --exclude {','.join(exclude_hosts)}"
        # Report reason
        if "--reason" not in options:
            options += " --reason"
        # Report down hosts
        if "-v" not in options:
            options += " -v"
        self._last_results = last_results
        return options

    async def _async_scan_devices(self, *_):
        """Scan devices and dispatch."""
        if self._scan_lock.locked():
            _LOGGER.debug(
                "Nmap scanning is taking longer than the scheduled interval: %s",
                TRACKER_SCAN_INTERVAL,
            )
            return

        async with self._scan_lock:
            try:
                await self._async_run_nmap_scan()
            except PortScannerError as ex:
                _LOGGER.error("Nmap scanning failed: %s", ex)

        if not self._finished_first_scan:
            self._finished_first_scan = True
            await self._async_mark_missing_devices_as_not_home()

    async def _async_mark_missing_devices_as_not_home(self):
        # After all config entries have finished their first
        # scan we mark devices that were not found as not_home
        # from unavailable
        now = dt_util.now()
        for mac_address, original_name in self._known_mac_addresses.items():
            if mac_address in self.devices.tracked:
                continue
            self.devices.config_entry_owner[mac_address] = self._entry_id
            self.devices.tracked[mac_address] = NmapDevice(
                mac_address,
                None,
                original_name,
                None,
                self._async_get_vendor(mac_address),
                "Device not found in initial scan",
                now,
                1,
            )
            async_dispatcher_send(self._hass, self.signal_device_missing, mac_address)

    def _run_nmap_scan(self):
        """Run nmap and return the result."""
        options = self._build_options()
        if not self._scanner:
            self._scanner = PortScanner()
        _LOGGER.debug("Scanning %s with args: %s", self._hosts, options)
        for attempt in range(MAX_SCAN_ATTEMPTS):
            try:
                result = self._scanner.scan(
                    hosts=" ".join(self._hosts),
                    arguments=options,
                    timeout=TRACKER_SCAN_INTERVAL * 10,
                )
                break
            except PortScannerError as ex:
                if attempt < (MAX_SCAN_ATTEMPTS - 1) and NMAP_TRANSIENT_FAILURE in str(
                    ex
                ):
                    _LOGGER.debug("Nmap saw transient error %s", NMAP_TRANSIENT_FAILURE)
                    continue
                raise
        _LOGGER.debug(
            "Finished scanning %s with args: %s",
            self._hosts,
            options,
        )
        return result

    @callback
    def _async_increment_device_offline(self, ipv4, reason):
        """Mark an IP offline."""
        if not (formatted_mac := self.devices.ipv4_last_mac.get(ipv4)):
            return
        if not (device := self.devices.tracked.get(formatted_mac)):
            # Device was unloaded
            return
        device.offline_scans += 1
        if device.offline_scans < OFFLINE_SCANS_TO_MARK_UNAVAILABLE:
            return
        device.reason = reason
        async_dispatcher_send(self._hass, signal_device_update(formatted_mac), False)
        del self.devices.ipv4_last_mac[ipv4]

    async def _async_run_nmap_scan(self):
        """Scan the network for devices and dispatch events."""
        result = await self._hass.async_add_executor_job(self._run_nmap_scan)
        if self._stopping:
            return

        devices = self.devices
        entry_id = self._entry_id
        now = dt_util.now()
        for ipv4, info in result["scan"].items():
            status = info["status"]
            reason = status["reason"]
            if status["state"] != "up":
                self._async_increment_device_offline(ipv4, reason)
                continue
            # Mac address only returned if nmap ran as root
            mac = info["addresses"].get(
                "mac"
            ) or await self._hass.async_add_executor_job(
                partial(get_mac_address, ip=ipv4)
            )
            if mac is None:
                self._async_increment_device_offline(ipv4, "No MAC address found")
                _LOGGER.info("No MAC address found for %s", ipv4)
                continue

            formatted_mac = format_mac(mac)
            new = formatted_mac not in devices.tracked
            if (
                new
                and formatted_mac not in devices.tracked
                and formatted_mac not in self._known_mac_addresses
            ):
                continue

            if (
                devices.config_entry_owner.setdefault(formatted_mac, entry_id)
                != entry_id
            ):
                continue

            hostname = info["hostnames"][0]["name"] if info["hostnames"] else ipv4
            vendor = info.get("vendor", {}).get(mac) or self._async_get_vendor(mac)
            name = human_readable_name(hostname, vendor, mac)
            device = NmapDevice(
                formatted_mac, hostname, name, ipv4, vendor, reason, now, 0
            )

            devices.tracked[formatted_mac] = device
            devices.ipv4_last_mac[ipv4] = formatted_mac
            self._last_results.append(device)

            if new:
                async_dispatcher_send(self._hass, self.signal_device_new, formatted_mac)
            else:
                async_dispatcher_send(
                    self._hass, signal_device_update(formatted_mac), True
                )
