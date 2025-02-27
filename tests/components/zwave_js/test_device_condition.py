"""The tests for Z-Wave JS device conditions."""
from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
import voluptuous_serialize
from zwave_js_server.const import CommandClass
from zwave_js_server.event import Event

from homeassistant.components import automation
from homeassistant.components.zwave_js import DOMAIN, device_condition
from homeassistant.components.zwave_js.helpers import get_zwave_value_from_config
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry
from homeassistant.setup import async_setup_component

from tests.common import async_get_device_automations, async_mock_service


@pytest.fixture
def calls(hass):
    """Track calls to a mock service."""
    return async_mock_service(hass, "test", "automation")


async def test_get_conditions(hass, client, lock_schlage_be469, integration) -> None:
    """Test we get the expected onditions from a zwave_js."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]
    config_value = list(lock_schlage_be469.get_configuration_values().values())[0]
    value_id = config_value.value_id
    name = config_value.property_name

    expected_conditions = [
        {
            "condition": "device",
            "domain": DOMAIN,
            "type": "node_status",
            "device_id": device.id,
        },
        {
            "condition": "device",
            "domain": DOMAIN,
            "type": "config_parameter",
            "device_id": device.id,
            "value_id": value_id,
            "subtype": f"{value_id} ({name})",
        },
        {
            "condition": "device",
            "domain": DOMAIN,
            "type": "value",
            "device_id": device.id,
        },
    ]
    conditions = await async_get_device_automations(hass, "condition", device.id)
    for condition in expected_conditions:
        assert condition in conditions


async def test_node_status_state(
    hass, client, lock_schlage_be469, integration, calls
) -> None:
    """Test for node_status conditions."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {"platform": "event", "event_type": "test_event1"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "node_status",
                            "status": "alive",
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "alive - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
                {
                    "trigger": {"platform": "event", "event_type": "test_event2"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "node_status",
                            "status": "awake",
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "awake - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
                {
                    "trigger": {"platform": "event", "event_type": "test_event3"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "node_status",
                            "status": "asleep",
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "asleep - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
                {
                    "trigger": {"platform": "event", "event_type": "test_event4"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "node_status",
                            "status": "dead",
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "dead - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
            ]
        },
    )

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    hass.bus.async_fire("test_event3")
    hass.bus.async_fire("test_event4")
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["some"] == "alive - event - test_event1"

    event = Event(
        "wake up",
        data={
            "source": "node",
            "event": "wake up",
            "nodeId": lock_schlage_be469.node_id,
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    hass.bus.async_fire("test_event3")
    hass.bus.async_fire("test_event4")
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert calls[1].data["some"] == "awake - event - test_event2"

    event = Event(
        "sleep",
        data={"source": "node", "event": "sleep", "nodeId": lock_schlage_be469.node_id},
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    hass.bus.async_fire("test_event3")
    hass.bus.async_fire("test_event4")
    await hass.async_block_till_done()
    assert len(calls) == 3
    assert calls[2].data["some"] == "asleep - event - test_event3"

    event = Event(
        "dead",
        data={"source": "node", "event": "dead", "nodeId": lock_schlage_be469.node_id},
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    hass.bus.async_fire("test_event3")
    hass.bus.async_fire("test_event4")
    await hass.async_block_till_done()
    assert len(calls) == 4
    assert calls[3].data["some"] == "dead - event - test_event4"

    event = Event(
        "unknown",
        data={
            "source": "node",
            "event": "unknown",
            "nodeId": lock_schlage_be469.node_id,
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()


async def test_config_parameter_state(
    hass, client, lock_schlage_be469, integration, calls
) -> None:
    """Test for config_parameter conditions."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {"platform": "event", "event_type": "test_event1"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "config_parameter",
                            "value_id": f"{lock_schlage_be469.node_id}-112-0-3",
                            "subtype": f"{lock_schlage_be469.node_id}-112-0-3 (Beeper)",
                            "value": 255,
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "Beeper - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
                {
                    "trigger": {"platform": "event", "event_type": "test_event2"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "config_parameter",
                            "value_id": f"{lock_schlage_be469.node_id}-112-0-6",
                            "subtype": f"{lock_schlage_be469.node_id}-112-0-6 (User Slot Status)",
                            "value": 1,
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "User Slot Status - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
            ]
        },
    )

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["some"] == "Beeper - event - test_event1"

    # Flip Beeper state to not match condition
    event = Event(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClassName": "Configuration",
                "commandClass": 112,
                "endpoint": 0,
                "property": 3,
                "newValue": 0,
                "prevValue": 255,
            },
        },
    )
    lock_schlage_be469.receive_event(event)

    # Flip User Slot Status to match condition
    event = Event(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClassName": "Configuration",
                "commandClass": 112,
                "endpoint": 0,
                "property": 6,
                "newValue": 1,
                "prevValue": 117440512,
            },
        },
    )
    lock_schlage_be469.receive_event(event)

    hass.bus.async_fire("test_event1")
    hass.bus.async_fire("test_event2")
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert calls[1].data["some"] == "User Slot Status - event - test_event2"


async def test_value_state(
    hass, client, lock_schlage_be469, integration, calls
) -> None:
    """Test for value conditions."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {"platform": "event", "event_type": "test_event1"},
                    "condition": [
                        {
                            "condition": "device",
                            "domain": DOMAIN,
                            "device_id": device.id,
                            "type": "value",
                            "command_class": 112,
                            "property": 3,
                            "value": 255,
                        }
                    ],
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "some": "value - {{ trigger.platform }} - {{ trigger.event.event_type }}"
                        },
                    },
                },
            ]
        },
    )

    hass.bus.async_fire("test_event1")
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["some"] == "value - event - test_event1"


async def test_get_condition_capabilities_node_status(
    hass, client, lock_schlage_be469, integration
):
    """Test we don't get capabilities from a node_status condition."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    capabilities = await device_condition.async_get_condition_capabilities(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "node_status",
        },
    )
    assert capabilities and "extra_fields" in capabilities
    assert voluptuous_serialize.convert(
        capabilities["extra_fields"], custom_serializer=cv.custom_serializer
    ) == [
        {
            "name": "status",
            "required": True,
            "type": "select",
            "options": [
                ("asleep", "asleep"),
                ("awake", "awake"),
                ("dead", "dead"),
                ("alive", "alive"),
            ],
        }
    ]


async def test_get_condition_capabilities_value(
    hass, client, lock_schlage_be469, integration
):
    """Test we get the expected capabilities from a value condition."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    capabilities = await device_condition.async_get_condition_capabilities(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "value",
        },
    )
    assert capabilities and "extra_fields" in capabilities

    cc_options = [
        (133, "ASSOCIATION"),
        (128, "BATTERY"),
        (112, "CONFIGURATION"),
        (98, "DOOR_LOCK"),
        (122, "FIRMWARE_UPDATE_MD"),
        (114, "MANUFACTURER_SPECIFIC"),
        (113, "ALARM"),
        (152, "SECURITY"),
        (99, "USER_CODE"),
        (134, "VERSION"),
    ]

    assert voluptuous_serialize.convert(
        capabilities["extra_fields"], custom_serializer=cv.custom_serializer
    ) == [
        {
            "name": "command_class",
            "required": True,
            "options": cc_options,
            "type": "select",
        },
        {"name": "property", "required": True, "type": "string"},
        {"name": "property_key", "optional": True, "type": "string"},
        {"name": "endpoint", "optional": True, "type": "string"},
        {"name": "value", "required": True, "type": "string"},
    ]


async def test_get_condition_capabilities_config_parameter(
    hass, client, climate_radio_thermostat_ct100_plus, integration
):
    """Test we get the expected capabilities from a config_parameter condition."""
    node = climate_radio_thermostat_ct100_plus
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    # Test enumerated type param
    capabilities = await device_condition.async_get_condition_capabilities(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "config_parameter",
            "value_id": f"{node.node_id}-112-0-1",
            "subtype": f"{node.node_id}-112-0-1 (Temperature Reporting Threshold)",
        },
    )
    assert capabilities and "extra_fields" in capabilities

    assert voluptuous_serialize.convert(
        capabilities["extra_fields"], custom_serializer=cv.custom_serializer
    ) == [
        {
            "name": "value",
            "required": True,
            "options": [
                (0, "Disabled"),
                (1, "0.5° F"),
                (2, "1.0° F"),
                (3, "1.5° F"),
                (4, "2.0° F"),
            ],
            "type": "select",
        }
    ]

    # Test range type param
    capabilities = await device_condition.async_get_condition_capabilities(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "config_parameter",
            "value_id": f"{node.node_id}-112-0-10",
            "subtype": f"{node.node_id}-112-0-10 (Temperature Reporting Filter)",
        },
    )
    assert capabilities and "extra_fields" in capabilities

    assert voluptuous_serialize.convert(
        capabilities["extra_fields"], custom_serializer=cv.custom_serializer
    ) == [
        {
            "name": "value",
            "required": True,
            "valueMin": 0,
            "valueMax": 124,
        }
    ]

    # Test undefined type param
    capabilities = await device_condition.async_get_condition_capabilities(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "config_parameter",
            "value_id": f"{node.node_id}-112-0-2",
            "subtype": f"{node.node_id}-112-0-2 (HVAC Settings)",
        },
    )
    assert not capabilities


async def test_failure_scenarios(hass, client, hank_binary_switch, integration):
    """Test failure scenarios."""
    dev_reg = device_registry.async_get(hass)
    device = device_registry.async_entries_for_config_entry(
        dev_reg, integration.entry_id
    )[0]

    with pytest.raises(HomeAssistantError):
        await device_condition.async_condition_from_config(
            {"type": "failed.test", "device_id": device.id}, False
        )

    with patch(
        "homeassistant.components.zwave_js.device_condition.async_get_node_from_device_id",
        return_value=None,
    ), patch(
        "homeassistant.components.zwave_js.device_condition.get_zwave_value_from_config",
        return_value=None,
    ):
        assert (
            await device_condition.async_get_condition_capabilities(
                hass, {"type": "failed.test", "device_id": device.id}
            )
            == {}
        )


async def test_get_value_from_config_failure(
    hass, client, hank_binary_switch, integration
):
    """Test get_value_from_config invalid value ID."""
    with pytest.raises(vol.Invalid):
        get_zwave_value_from_config(
            hank_binary_switch,
            {
                "command_class": CommandClass.SCENE_ACTIVATION.value,
                "property": "sceneId",
                "property_key": 15,
                "endpoint": 10,
            },
        )
