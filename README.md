# Energy Compositor

Energy Compositor is a vendor-neutral Home Assistant custom integration. It combines the source sensors you trust into canonical energy, power, and battery SOC entities under one virtual device. It never polls or accumulates power into an invented energy counter.

## Install and configure

To install with HACS, add `https://github.com/babgvant/ha-energy-compositor` as a **custom repository** of type **Integration**, then install **Energy Compositor** and restart Home Assistant. HACS uses the `custom_components/energy_compositor` directory in this repository. The integration's local icon and logo are included under `brand/` for Home Assistant 2026.3 and later.

For manual installation, copy `custom_components/energy_compositor` into the same directory in your Home Assistant configuration and restart Home Assistant. Then add **Energy Compositor** in **Settings → Devices & services**. Open its options to configure Solar, Battery, Grid, and Home channels. Only configured channels create entities; the balance error diagnostic is always created.

Each channel has an **entity** mode (one sensor), a **sum** mode (all selected sensors), or **none**. Home power also offers **calculated** mode:

`solar + grid import + battery discharge − grid export − battery charge`

Calculated home power needs all five input power channels configured. Home energy has no calculated mode because subtracting cumulative meters can cause decreases that corrupt long-term statistics. Use a direct or summed cumulative home energy source instead.

Entity selection accepts any sensor so custom and template sensors remain usable. Sources must report appropriate units: Wh, kWh or MWh for energy; W or kW for power; `%` for SOC. Outputs use kWh, W and `%`. If any member of a sum is unavailable or has an invalid unit, the output becomes unavailable. Each entity lists its source mode and entity IDs in attributes.

Energy outputs require source `state_class` metadata. One source's `total` or `total_increasing` is preserved. A sum receives a state class only when every source reports the same class; mixed or missing classes make it unavailable. This avoids claiming incorrect Energy Dashboard statistics. For `total_increasing` sums, ensure all meters reset together: independent resets can cause a misleading decline. Prefer lifetime `total` meters when combining independent sources. Changing source mappings can change a cumulative baseline; review long-term statistics after such changes.

## Energy Dashboard

In **Settings → Dashboards → Energy**, use:

| Dashboard input | Compositor entity |
| --- | --- |
| Grid consumption | Energy Compositor Grid Import Energy |
| Return to grid | Energy Compositor Grid Export Energy |
| Solar production | Energy Compositor Solar Energy |
| Battery energy in | Energy Compositor Battery Charge Energy |
| Battery energy out | Energy Compositor Battery Discharge Energy |
| Battery SOC | Energy Compositor Battery SOC |

Configure only channels backed by valid cumulative sensors. Power sensors and home energy are useful elsewhere in dashboards.

## Example topology and balance diagnostic

For a home with HS, HN, ADU, PB Garage, and Smart Loads meters, set Home Energy and Home Power to **sum** of those five high-level sources. An EVSE downstream of Smart Loads is already counted there; adding it again would double-count it.

The **Balance Error Power** sensor reports `solar + grid import + battery discharge − grid export − battery charge − home`. It becomes unavailable if any input is missing. A sustained nonzero value can indicate missing loads, meter timing differences, or incorrect source directions.
