"""Behavioral tests for Energy Compositor."""

from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.energy_compositor.config_flow import validate_mapping, validate_mappings
from custom_components.energy_compositor import async_unload_entry
from custom_components.energy_compositor.sensor import CompositorSensor, SourceSensor
from custom_components.energy_compositor.sensor import async_setup_entry as async_setup_sensors


def make_sensor(hass, channel, mapping, channels=None):
    entry = SimpleNamespace(entry_id="stable-id", title="Energy Compositor", options={"channels": channels or {channel: mapping}})
    sensor = CompositorSensor(entry, channel, mapping)
    sensor.hass = hass
    return sensor


@pytest.mark.parametrize(
    ("channel", "sources", "expected", "unit", "device_class"),
    [
        ("home_power", [("sensor.a", 500, "W"), ("sensor.b", 1.2, "kW")], 1700, "W", SensorDeviceClass.POWER),
        ("home_energy", [("sensor.a", 500, "Wh"), ("sensor.b", 1.2, "kWh")], 1.7, "kWh", SensorDeviceClass.ENERGY),
        ("battery_soc", [("sensor.a", 48, "%")], 48, "%", SensorDeviceClass.BATTERY),
    ],
)
def test_normalization_and_metadata(hass, channel, sources, expected, unit, device_class):
    for entity_id, value, source_unit in sources:
        attrs = {"unit_of_measurement": source_unit}
        if channel.endswith("energy"):
            attrs["state_class"] = "total"
        hass.states.async_set(entity_id, value, attrs)
    mapping = {"mode": "sum" if len(sources) > 1 else "entity", "entities": [item[0] for item in sources]}
    sensor = make_sensor(hass, channel, mapping)
    assert sensor.native_value == pytest.approx(expected)
    assert sensor.native_unit_of_measurement == unit
    assert sensor.device_class == device_class
    assert sensor.unique_id == "stable-id_" + channel
    assert sensor.state_class == (SensorStateClass.TOTAL if channel.endswith("energy") else SensorStateClass.MEASUREMENT)


def test_unavailable_and_recovery(hass):
    hass.states.async_set("sensor.a", 100, {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.b", 200, {"unit_of_measurement": "W"})
    sensor = make_sensor(hass, "home_power", {"mode": "sum", "entities": ["sensor.a", "sensor.b"]})
    assert sensor.native_value == 300
    hass.states.async_set("sensor.b", "unavailable", {"unit_of_measurement": "W"})
    assert sensor.available is False
    hass.states.async_set("sensor.b", 250, {"unit_of_measurement": "W"})
    assert sensor.native_value == 350
    hass.states.async_set("sensor.b", 250, {"unit_of_measurement": "V"})
    assert sensor.available is False


def test_energy_class_must_agree(hass):
    hass.states.async_set("sensor.a", 1, {"unit_of_measurement": "kWh", "state_class": "total"})
    hass.states.async_set("sensor.b", 2, {"unit_of_measurement": "kWh", "state_class": "total_increasing"})
    sensor = make_sensor(hass, "home_energy", {"mode": "sum", "entities": ["sensor.a", "sensor.b"]})
    assert sensor.state_class is None
    assert sensor.available is False


def test_energy_reset_is_preserved(hass):
    reset = datetime(2026, 9, 27, tzinfo=timezone.utc)
    hass.states.async_set("sensor.daily", 78.8, {
        "unit_of_measurement": "kWh", "state_class": "total",
        "last_reset": reset.isoformat(),
    })
    sensor = make_sensor(hass, "home_energy", {"mode": "entity", "entities": ["sensor.daily"]})
    assert sensor.last_reset == reset


def test_mixed_energy_reset_cycles_are_unavailable(hass):
    for entity_id, reset in (("sensor.a", "2026-09-27T00:00:00+00:00"),
                             ("sensor.b", "2026-09-01T00:00:00+00:00")):
        hass.states.async_set(entity_id, 1, {
            "unit_of_measurement": "kWh", "state_class": "total", "last_reset": reset,
        })
    sensor = make_sensor(hass, "home_energy", {"mode": "sum", "entities": ["sensor.a", "sensor.b"]})
    assert sensor.state_class is None
    assert sensor.available is False


def test_calculated_home_and_balance(hass):
    terms = {"solar_power": 2000, "grid_import_power": 300, "battery_discharge_power": 100, "grid_export_power": 200, "battery_charge_power": 50}
    channels = {}
    for channel, value in terms.items():
        entity_id = "sensor." + channel
        hass.states.async_set(entity_id, value, {"unit_of_measurement": "W"})
        channels[channel] = {"mode": "entity", "entities": [entity_id]}
    channels["home_power"] = {"mode": "calculated", "entities": []}
    home = make_sensor(hass, "home_power", channels["home_power"], channels)
    assert home.native_value == 2150
    channels["home_power"] = {"mode": "entity", "entities": ["sensor.home"]}
    hass.states.async_set("sensor.home", 2100, {"unit_of_measurement": "W"})
    balance = make_sensor(hass, "balance_error_power", {"mode": "calculated", "entities": []}, channels)
    assert balance.native_value == 50


@pytest.mark.parametrize("mode, entities", [
    ("entity", ["sensor.solar"]),
    ("sum", ["sensor.solar_a", "sensor.solar_b"]),
])
def test_direct_source_attributes(hass, mode, entities):
    sensor = make_sensor(hass, "solar_power", {"mode": mode, "entities": entities})
    assert sensor.extra_state_attributes == {
        "source_mode": mode, "source_entities": entities,
    }


def test_calculated_source_attributes_include_all_dependencies(hass):
    channels = {
        "solar_power": {"mode": "sum", "entities": ["sensor.pv_a", "sensor.pv_b"]},
        "grid_import_power": {"mode": "entity", "entities": ["sensor.import"]},
        "battery_discharge_power": {"mode": "entity", "entities": ["sensor.discharge"]},
        "grid_export_power": {"mode": "entity", "entities": ["sensor.export"]},
        "battery_charge_power": {"mode": "entity", "entities": ["sensor.charge"]},
        "home_power": {"mode": "calculated", "entities": []},
    }
    expected = ["sensor.pv_a", "sensor.pv_b", "sensor.import", "sensor.discharge", "sensor.export", "sensor.charge"]
    home = make_sensor(hass, "home_power", channels["home_power"], channels)
    balance = make_sensor(hass, "balance_error_power", {"mode": "calculated", "entities": []}, channels)
    # Missing states must not hide configured inputs. Calculated home repeats
    # the balance inputs, which should each be listed only once.
    assert home.available is False
    assert home.extra_state_attributes["source_entities"] == expected
    assert balance.extra_state_attributes["source_entities"] == expected
    channels["home_power"] = {"mode": "sum", "entities": ["sensor.load_a", "sensor.load_b"]}
    assert balance.extra_state_attributes["source_entities"] == expected + ["sensor.load_a", "sensor.load_b"]


def test_calculated_source_attributes_skip_missing_and_disabled_channels(hass):
    channels = {
        "solar_power": {"mode": "entity", "entities": ["sensor.pv"]},
        "grid_import_power": {"mode": "none", "entities": ["sensor.unused"]},
    }
    balance = make_sensor(hass, "balance_error_power", {"mode": "calculated", "entities": []}, channels)
    assert balance.extra_state_attributes["source_entities"] == ["sensor.pv"]


def test_mapping_validation():
    assert validate_mapping("home_power", {"mode": "sum", "entities": []}) == "missing_entities"
    assert validate_mapping("home_power", {"mode": "sum", "entities": ["sensor.a", "sensor.a"]}) == "duplicate_entities"
    assert validate_mapping("battery_soc", {"mode": "entity", "entities": ["sensor.a", "sensor.b"]}) == "one_entity_required"
    registry = Mock()
    registry.async_get.return_value = SimpleNamespace(platform="energy_compositor")
    assert validate_mapping("home_power", {"mode": "entity", "entities": ["sensor.a"]}, registry) == "self_source"
    assert validate_mappings({"home_power": {"mode": "calculated", "entities": []}}) == "missing_dependency"


def test_source_sensor_preserves_metadata_and_recovers(hass):
    entry = SimpleNamespace(entry_id="stable-id", title="Energy Compositor")
    source = SourceSensor(entry, "sensor.pv", "registry-id")
    source.hass = hass
    attributes = {
        "friendly_name": "PV Total", "unit_of_measurement": "MWh",
        "device_class": "energy", "state_class": "total",
        "last_reset": "2026-09-27T00:00:00+00:00", "meter_status": "online",
    }
    assert source.available is False
    assert source.extra_state_attributes == {"source_entity": "sensor.pv"}
    hass.states.async_set("sensor.pv", "1.234", attributes)
    assert source.available is True
    assert source.native_value == "1.234"
    assert source.name == "Source PV Total"
    assert source.native_unit_of_measurement == "MWh"
    assert source.device_class == "energy"
    assert source.state_class == "total"
    assert source.last_reset == datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert source.extra_state_attributes == {"meter_status": "online", "source_entity": "sensor.pv"}
    hass.states.async_set("sensor.pv", "unavailable", attributes)
    assert source.available is False
    assert source.native_value is None
    hass.states.async_set("sensor.pv", "1.235", attributes)
    assert source.available is True
    assert source.native_value == "1.235"
    renamed = SourceSensor(entry, "sensor.renamed_pv", "registry-id")
    assert renamed.unique_id == source.unique_id


async def test_all_configured_sources_are_exposed_once(hass):
    entry = SimpleNamespace(
        entry_id="stable-id", title="Energy Compositor",
        options={"channels": {
            "solar_power": {"mode": "sum", "entities": ["sensor.pv_a", "sensor.pv_b"]},
            "home_power": {"mode": "sum", "entities": ["sensor.pv_a", "sensor.load"]},
            "grid_import_power": {"mode": "none", "entities": ["sensor.unused"]},
        }},
        async_on_unload=Mock(),
    )
    added = []
    await async_setup_sensors(hass, entry, added.extend)
    sources = [sensor for sensor in added if isinstance(sensor, SourceSensor)]
    assert [sensor._source_entity_id for sensor in sources] == ["sensor.load", "sensor.pv_a", "sensor.pv_b"]
    for sensor in added:
        sensor.hass = hass
        sensor.async_write_ha_state = Mock()
    hass.states.async_set("sensor.pv_b", "2.5", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    pv_b = next(sensor for sensor in sources if sensor._source_entity_id == "sensor.pv_b")
    assert pv_b.native_value == "2.5"
    pv_b.async_write_ha_state.assert_called()


async def test_shared_state_listener(hass):
    entry = SimpleNamespace(
        entry_id="stable-id", title="Energy Compositor",
        options={"channels": {"home_power": {"mode": "entity", "entities": ["sensor.source"]}}},
        async_on_unload=Mock(),
    )
    added = []
    hass.states.async_set("sensor.source", 100, {"unit_of_measurement": "W"})
    await async_setup_sensors(hass, entry, added.extend)
    for item in added:
        item.hass = hass
        item.async_write_ha_state = Mock()
    sensor = next(item for item in added if item._channel == "home_power")
    hass.states.async_set("sensor.source", 200, {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called()
    assert sensor.native_value == 200


async def test_unload_delegates_cleanup_to_home_assistant(hass):
    entry = Mock()
    with patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True) as unload:
        assert await async_unload_entry(hass, entry)
    unload.assert_awaited_once_with(entry, ["sensor"])
    assert not entry.async_unload.called


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_config_and_options_flow(hass):
    result = await hass.config_entries.flow.async_init("energy_compositor", context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "My Home"})
    assert result["type"] == "create_entry"
    entry = result["result"]
    assert entry.title == "My Home"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "solar"})
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "solar_energy_mode": "entity", "solar_energy_entities": ["sensor.pv_energy"],
        "solar_power_mode": "none", "solar_power_entities": [],
    })
    assert result["type"] == "create_entry"
    assert entry.options["channels"]["solar_energy"]["entities"] == ["sensor.pv_energy"]
