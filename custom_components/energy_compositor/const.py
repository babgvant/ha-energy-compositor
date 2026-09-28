"""Constants and channel definitions for Energy Compositor."""

DOMAIN = "energy_compositor"
DEFAULT_NAME = "Energy Compositor"
GROUPS = {
    "solar": ("solar_energy", "solar_power"),
    "battery": (
        "battery_charge_energy", "battery_discharge_energy",
        "battery_charge_power", "battery_discharge_power", "battery_soc",
    ),
    "grid": (
        "grid_import_energy", "grid_export_energy",
        "grid_import_power", "grid_export_power",
    ),
    "home": ("home_energy", "home_power"),
}
CHANNELS = tuple(channel for group in GROUPS.values() for channel in group)
POWER_CHANNELS = tuple(channel for channel in CHANNELS if channel.endswith("_power"))
ENERGY_CHANNELS = tuple(channel for channel in CHANNELS if channel.endswith("_energy"))
HOME_POWER_TERMS = {
    "solar_power": 1,
    "grid_import_power": 1,
    "battery_discharge_power": 1,
    "grid_export_power": -1,
    "battery_charge_power": -1,
}
BALANCE_TERMS = {**HOME_POWER_TERMS, "home_power": -1}
