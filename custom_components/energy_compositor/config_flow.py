"""UI configuration for Energy Compositor."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import CHANNELS, DEFAULT_NAME, DOMAIN, GROUPS, HOME_POWER_TERMS


def validate_mapping(channel: str, mapping: dict, registry=None) -> str | None:
    """Return a translation error key for an invalid channel mapping."""
    mode = mapping.get("mode", "none")
    if mode not in ({"none", "entity"} if channel == "battery_soc" else {"none", "entity", "sum", "calculated"} if channel == "home_power" else {"none", "entity", "sum"}):
        return "invalid_mode"
    entities = mapping.get("entities", [])
    if mode == "none":
        return None
    if mode == "calculated":
        return None
    if not isinstance(entities, list) or not entities:
        return "missing_entities"
    if mode == "entity" and len(entities) != 1:
        return "one_entity_required"
    if len(entities) != len(set(entities)):
        return "duplicate_entities"
    if any(not isinstance(item, str) or not item.startswith("sensor.") for item in entities):
        return "invalid_entity"
    if registry is not None and any(
        (entry := registry.async_get(item)) is not None
        and entry.platform == DOMAIN for item in entities
    ):
        return "self_source"
    return None


def validate_mappings(mappings: dict, registry=None) -> str | None:
    """Validate all configured mappings and the calculated dependency graph."""
    for channel, mapping in mappings.items():
        if channel not in CHANNELS or not isinstance(mapping, dict):
            return "invalid_channel"
        if error := validate_mapping(channel, mapping, registry):
            return error
    if mappings.get("home_power", {}).get("mode") == "calculated":
        if not all(mappings.get(term, {}).get("mode") in ("entity", "sum") for term in HOME_POWER_TERMS):
            return "missing_dependency"
    return None


class EnergyCompositorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a single home/system instance."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input["name"], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("name", default=DEFAULT_NAME): str}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnergyCompositorOptionsFlow(config_entry)


class EnergyCompositorOptionsFlow(config_entries.OptionsFlow):
    """Edit channel mappings in four short sections."""

    def __init__(self, config_entry):
        self._entry = config_entry
        self._mappings = dict(config_entry.options.get("channels", {}))

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=list(GROUPS))

    def _schema(self, group: str) -> vol.Schema:
        fields = {}
        for channel in GROUPS[group]:
            current = self._mappings.get(channel, {})
            choices = ["none", "entity"]
            if channel != "battery_soc":
                choices.append("sum")
            if channel == "home_power":
                choices.append("calculated")
            fields[vol.Required(f"{channel}_mode", default=current.get("mode", "none"))] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=choices, mode=selector.SelectSelectorMode.DROPDOWN)
            )
            fields[vol.Optional(f"{channel}_entities", default=current.get("entities", []))] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            )
        return vol.Schema(fields)

    async def _step_group(self, group: str, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = dict(self._mappings)
            for channel in GROUPS[group]:
                mode = user_input[f"{channel}_mode"]
                entities = user_input.get(f"{channel}_entities", [])
                candidate[channel] = {"mode": mode, "entities": entities if mode in ("entity", "sum") else []}
                if error := validate_mapping(channel, candidate[channel], er.async_get(self.hass)):
                    errors[f"{channel}_entities"] = error
            if not errors:
                if error := validate_mappings(candidate, er.async_get(self.hass)):
                    errors["base"] = error
                else:
                    self._mappings = candidate
                    return self.async_create_entry(title="", data={"channels": candidate})
        return self.async_show_form(step_id=group, data_schema=self._schema(group), errors=errors)

    async def async_step_solar(self, user_input=None):
        return await self._step_group("solar", user_input)

    async def async_step_battery(self, user_input=None):
        return await self._step_group("battery", user_input)

    async def async_step_grid(self, user_input=None):
        return await self._step_group("grid", user_input)

    async def async_step_home(self, user_input=None):
        return await self._step_group("home", user_input)
