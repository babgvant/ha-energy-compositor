"""Event-driven canonical energy and power sensors."""

from __future__ import annotations

import logging
import math
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from .const import BALANCE_TERMS, CHANNELS, DOMAIN, ENERGY_CHANNELS, HOME_POWER_TERMS, POWER_CHANNELS

_LOGGER = logging.getLogger(__name__)
_FACTORS = {
    "energy": {"Wh": 0.001, "kWh": 1, "MWh": 1000},
    "power": {"W": 1, "kW": 1000},
    "soc": {"%": 1},
}


def _kind(channel):
    return "energy" if channel in ENERGY_CHANNELS else "power" if channel in POWER_CHANNELS or channel == "balance_error_power" else "soc"


def _normalized(state, kind):
    """Return a canonical number or None for a missing/invalid source."""
    if state is None or state.state in ("unknown", "unavailable", "none", "None", ""):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit not in _FACTORS[kind]:
        return None
    try:
        value = float(state.state) * _FACTORS[kind][unit]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _energy_class(states):
    """Only expose statistics when all sources agree on cumulative semantics."""
    classes = {state.attributes.get("state_class") for state in states}
    if classes == {SensorStateClass.TOTAL}:
        resets = {state.attributes.get("last_reset") for state in states}
        return SensorStateClass.TOTAL if len(resets) == 1 else None
    if classes == {SensorStateClass.TOTAL_INCREASING}:
        return SensorStateClass.TOTAL_INCREASING
    return None


def _energy_reset(states):
    """Return a shared reset time for total sensors, if their cycles agree."""
    resets = {state.attributes.get("last_reset") for state in states}
    if len(resets) != 1:
        return None
    reset = resets.pop()
    if reset is None:
        return None
    try:
        parsed = datetime.fromisoformat(reset) if isinstance(reset, str) else reset
    except ValueError:
        return None
    return parsed if isinstance(parsed, datetime) and parsed.tzinfo is not None else None


async def async_setup_entry(hass, entry, async_add_entities):
    mappings = entry.options.get("channels", {})
    active = {key: value for key, value in mappings.items() if key in CHANNELS and value.get("mode") != "none"}
    sensors = {key: CompositorSensor(entry, key, active[key]) for key in active}
    sensors["balance_error_power"] = CompositorSensor(entry, "balance_error_power", {"mode": "calculated", "entities": []})

    watched = {entity for mapping in active.values() for entity in mapping.get("entities", [])}
    registry = er.async_get(hass)
    sources = []
    for entity_id in sorted(watched):
        source_entry = registry.async_get(entity_id)
        sources.append(SourceSensor(entry, entity_id, source_entry.id if source_entry else None))
    entities = [*sensors.values(), *sources]
    async_add_entities(entities)

    @callback
    def source_changed(event):
        for sensor in entities:
            sensor.async_write_ha_state()

    if watched:
        entry.async_on_unload(async_track_state_change_event(hass, list(watched), source_changed))

        @callback
        def registry_changed(event):
            old_id = event.data.get("old_entity_id")
            new_id = event.data.get("entity_id")
            if event.data.get("action") != "update" or old_id not in watched or not new_id:
                return
            updated = {
                channel: {**mapping, "entities": [new_id if item == old_id else item for item in mapping.get("entities", [])]}
                for channel, mapping in mappings.items()
            }
            hass.config_entries.async_update_entry(entry, options={**entry.options, "channels": updated})

        entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed))


class SourceSensor(SensorEntity):
    """Expose a configured input on the virtual device for inspection."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, entity_id, registry_id=None):
        self._source_entity_id = entity_id
        self._attr_unique_id = f"{entry.entry_id}_source_{registry_id or entity_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Energy Compositor",
            model="Virtual energy system",
        )

    @property
    def _source(self):
        return self.hass.states.get(self._source_entity_id)

    @property
    def name(self):
        source = self._source
        return f"Source {source.attributes.get('friendly_name', self._source_entity_id) if source else self._source_entity_id}"

    @property
    def available(self):
        source = self._source
        return source is not None and source.state not in ("unknown", "unavailable")

    @property
    def native_value(self):
        return self._source.state if self.available else None

    @property
    def native_unit_of_measurement(self):
        return self._source.attributes.get("unit_of_measurement") if self._source else None

    @property
    def device_class(self):
        return self._source.attributes.get("device_class") if self._source else None

    @property
    def state_class(self):
        return self._source.attributes.get("state_class") if self._source else None

    @property
    def last_reset(self):
        return _energy_reset([self._source]) if self._source else None

    @property
    def extra_state_attributes(self):
        attributes = dict(self._source.attributes) if self._source else {}
        # SensorEntity supplies these from the corresponding properties.
        for key in ("friendly_name", "unit_of_measurement", "device_class", "state_class", "last_reset"):
            attributes.pop(key, None)
        return {**attributes, "source_entity": self._source_entity_id}


class CompositorSensor(SensorEntity):
    """A normalized channel backed by one or more HA entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, channel, mapping):
        self._entry = entry
        self._channel = channel
        self._mapping = mapping
        self._warned = set()
        self._attr_unique_id = f"{entry.entry_id}_{channel}"
        self._attr_name = channel.replace("_", " ").title()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Energy Compositor",
            model="Virtual energy system",
        )
        kind = _kind(channel)
        self._attr_device_class = {
            "energy": SensorDeviceClass.ENERGY,
            "power": SensorDeviceClass.POWER,
            "soc": SensorDeviceClass.BATTERY,
        }[kind]
        self._attr_native_unit_of_measurement = {
            "energy": UnitOfEnergy.KILO_WATT_HOUR,
            "power": UnitOfPower.WATT,
            "soc": PERCENTAGE,
        }[kind]

    @property
    def extra_state_attributes(self):
        return {
            "source_mode": self._mapping["mode"],
            "source_entities": self._source_entities(self._channel),
        }

    def _source_entities(self, channel, seen=None):
        """List every configured input, including calculated dependencies."""
        seen = seen or set()
        if channel in seen:
            return []
        seen = seen | {channel}
        mapping = self._mapping if channel == self._channel else self._entry.options.get("channels", {}).get(channel)
        if not mapping or mapping.get("mode") == "none":
            return []
        if mapping.get("mode") != "calculated":
            return list(mapping.get("entities", []))
        terms = BALANCE_TERMS if channel == "balance_error_power" else HOME_POWER_TERMS
        return list(dict.fromkeys(
            entity_id
            for source in terms
            for entity_id in self._source_entities(source, seen)
        ))

    def _value(self, channel, seen=None):
        """Evaluate one channel; dependencies are calculated in memory."""
        seen = seen or set()
        if channel in seen:
            return None
        seen = seen | {channel}
        mapping = self._mapping if channel == self._channel else self._entry.options.get("channels", {}).get(channel)
        if not mapping or mapping.get("mode") == "none":
            return None
        if mapping["mode"] == "calculated":
            terms = BALANCE_TERMS if channel == "balance_error_power" else HOME_POWER_TERMS
            values = [(sign, self._value(source, seen)) for source, sign in terms.items()]
            return sum(sign * value for sign, value in values) if all(value is not None for _, value in values) else None
        states = [self.hass.states.get(entity_id) for entity_id in mapping.get("entities", [])]
        values = []
        for entity_id, state in zip(mapping.get("entities", []), states):
            value = _normalized(state, _kind(channel))
            if value is None and state is not None and state.state not in ("unknown", "unavailable"):
                unit = state.attributes.get("unit_of_measurement")
                if unit not in _FACTORS[_kind(channel)] and (entity_id, unit) not in self._warned:
                    _LOGGER.warning("Invalid unit %r on %s for %s", unit, entity_id, channel)
                    self._warned.add((entity_id, unit))
            if value is None:
                return None
            values.append(value)
        return sum(values) if values else None

    @property
    def native_value(self):
        return self._value(self._channel)

    @property
    def available(self):
        return self.native_value is not None and (self.state_class is not None or _kind(self._channel) != "energy")

    @property
    def state_class(self):
        if _kind(self._channel) != "energy":
            return SensorStateClass.MEASUREMENT
        states = [self.hass.states.get(item) for item in self._mapping.get("entities", [])]
        if any(state is None for state in states) or not states:
            return None
        return _energy_class(states)

    @property
    def last_reset(self):
        if self.state_class != SensorStateClass.TOTAL:
            return None
        states = [self.hass.states.get(item) for item in self._mapping.get("entities", [])]
        return _energy_reset(states)
