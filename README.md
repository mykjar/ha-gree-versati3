# Gree Versati 3 Local (Home Assistant)

> **Fork notice:** This is a fork of [`sarunasr/ha-gree-versati3`](https://github.com/sarunasr/ha-gree-versati3). Changes made in this fork are developed using [Claude Code](https://claude.com/claude-code), Anthropic's AI coding assistant.

Custom Home Assistant integration for local LAN control of a Gree Versati 3 heat pump over the device UDP JSON protocol.

This README is in two parts: [what this fork adds](#added-in-this-fork), and [the original integration](#from-the-original-repository) it builds on.

## Installation

1. In Home Assistant, open HACS.
2. Open the menu (`⋮`) and choose `Custom repositories`.
3. Add `https://github.com/mykjar/ha-gree-versati3` with category `Integration`.
4. Download `Gree Versati 3 Local`, then restart Home Assistant.
5. Add the integration: `Settings` -> `Devices & Services` -> `Add Integration` -> `Gree Versati 3 Local`. For the fields, and how to get the device ID and key, see [Configuration Fields](#configuration-fields).

Updates come as [GitHub releases](https://github.com/mykjar/ha-gree-versati3/releases), which HACS offers like any other update. If you downloaded it before the first release (v0.2.0), HACS may not offer updates. In that case, use `⋮` -> `Redownload` once and pick the latest version.

## Added in this fork

| Version | What's new |
| --- | --- |
| [v0.2.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.2.0) | Measured water and tank temperature sensors |
| [v0.3.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.3.0) | Floor heating control, advisory only |
| [v0.4.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.4.0) | Floor heating control can set the pump; tuning from a dashboard |
| [v0.5.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.5.0) | Floor heating dashboard card |
| [v0.6.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.6.0) | Heat + hot water priority |
| [v0.7.0](https://github.com/mykjar/ha-gree-versati3/releases/tag/v0.7.0) | Floor heating control uses the weather forecast |

`Configure` on the integration now opens a menu: `Polling` holds the original polling interval, and `Floor heating control` holds the new settings.

### Measured temperature sensors

Sensors in °C, decoded from `<name>Hi`/`<name>Lo` pairs as `(Hi - 100) + Lo * 0.1`:

- Water outlet temperature (`AllOutWatTem`)
- Water inlet temperature (`AllInWatTem`)
- Water tank temperature (`WatBoxTem`)
- Heat pump water outlet temperature (`HepOutWatTem`, disabled by default)
- Remote room temperature (`RmoHomTem`, disabled by default)

They are polled in a separate request, so a unit that does not report them keeps all other entities working. A sensor reporting -30.0 °C is treated as not fitted and shown as unavailable.

### Heat + hot water priority

**Heat + hot water priority**, under Configuration on the pump's device, chooses what goes first in Heat + hot water mode: **Heating** (the floor) or **Hot water** (the tank). It is the Gree app's **Priority selection** (parameter `HetHtWter`: 0 = Heating, 1 = Hot water), so a change in either place shows in both.

### Floor heating control (optional)

Sets the heating water setpoint (`HeWatOutTemSet`) for underfloor heating. It follows an outdoor-temperature curve, with a small correction from the indoor temperature. The aim is a steady room temperature at the lowest water temperature that holds it, which is also when a heat pump runs most efficiently.

It is off by default. Once enabled it starts in **advisory mode**: it shows the setpoint it would use and changes nothing until you turn on **Write to pump**.

#### How it works

```text
water = curve(outdoor) + (1 + slope) × (room target − 21 °C)   two-point curve, flat beyond its points
      + Kp × (room target − indoor)                            room correction, Kp 2.5 by default
      + learned offset                                         slow, within ±3 °C, for a curve that is off
```

The result is kept within 20–30 °C by default and rounded to whole degrees. The setpoint changes by 1 °C at a time, at least 30 minutes apart, and only once the target has moved 0.75 °C. The defaults were tuned on a simulated house with a screed floor, a cold snap, sunny afternoons and noisy sensors. In that simulation the room stays within ±0.5 °C at least 95 % of the time, with at most 12 changes a day (`tests/test_heating_simulation.py`).

#### Before you start

- The pump must control by water temperature, with its own weather curve (W-depend) off. If W-depend is on, the controller won't write and says why.
- You need indoor and outdoor temperature sensors from any integration. Averages work best.
- Optional: a weather entity with an hourly forecast. Home Assistant's built-in `weather.forecast_home` (Met.no) works.

#### Setting it up

1. `Settings` -> `Devices & Services` -> `Gree Versati 3 Local` -> `Configure` -> `Floor heating control`.
2. Tick `Enable`, pick the indoor and outdoor sensors, and set `Highest water setpoint` to your floor's limit. Leave `Weather forecast` on `weather.forecast_home` (suggested when it exists) to use the forecast.
3. Run it in advisory mode for a few days. While the house is comfortable on your usual setpoint, press **Calibrate curve to current setpoint** once. Then compare **Suggested water setpoint** with what you would choose.
4. Turn on **Write to pump**.

What it never does: change power, mode, hot water or cooling; write outside the configured range; or fight a setpoint changed on the pump or in an app. A manual change pauses it for 6 hours by default. If Home Assistant stops, the pump keeps its last setpoint.

#### Weather forecast

A concrete floor reacts hours late, so with a weather forecast chosen the controller looks ahead in two ways:

- **Look-ahead:** the curve uses the average of the outdoor temperature now and the forecast for each half hour of the next 4 hours.
- **Sun cut:** when the forecast shows sun 2–6 hours ahead, the water is lowered by up to 2 °C, so the floor isn't full of heat when the sun arrives. It uses the sun's real position at your home.

The forecast is fetched every 30 minutes. Without a usable forecast the controller works exactly as it would without one, and says so once in the log. `Look ahead` and `Sun cut` under `Configure` tune both; 0 turns each off.

In the simulated house, with forecasts that are wrong in realistic ways, the room stayed within ±0.5 °C noticeably more often: about 83 % → 93 % on sunny autumn days, with 70 % less overheating, and 95 % → 99 % in a winter fortnight. That held even when 30 % of days were forecast wrong. No run was colder, and energy use was the same: this is about comfort, not savings.

#### Entities (on the pump's device)

| Entity | Purpose |
| --- | --- |
| Floor heating (thermostat) | The room temperature to aim for. Off pauses control and leaves the pump as it is. |
| Write to pump | Off: advisory only. On: the controller sets the pump's water setpoint. |
| Suggested water setpoint | The whole-degree setpoint it would set now. |
| Target water temperature, Curve water temperature | The calculation before rounding, and the curve's part of it. |
| Learned curve offset | What learning currently adds to the curve. |
| Smoothed outdoor temperature | The outdoor temperature the curve uses, after averaging (and looking ahead, with a forecast). |
| Forecast outdoor temperature, Sun cut | What the forecast contributes: the look-ahead average, and how far the water is lowered for sun. |
| Floor heating status | See below. |
| Curve cold point / mild point (outdoor, water), Room correction | Tuning, also available under `Configure`. |
| Learn curve offset, Reset learned offset | Turn learning on or off, or forget what it learned. |
| Calibrate curve to current setpoint | Shift the curve so it reproduces today's setpoint. |
| Resume floor heating control | End a pause after a manual change. |

#### Statuses

| Status | Meaning |
| --- | --- |
| Active | In control; changes the setpoint when needed. |
| Advisory only | Calculating; `Write to pump` is off. |
| Waiting to change the setpoint | A change is due but waits for the 30-minute gap. |
| Paused after a manual change | The setpoint was changed elsewhere; control waits. |
| Paused | The thermostat is Off. |
| Pump off, Pump not in a heating mode, Pump error, Pump unavailable | Nothing is written until the pump is heating normally again. |
| Pump's own weather curve is on | W-depend is on, so the pump would ignore the setpoint. |
| Sensor unavailable | The indoor or outdoor reading is missing, or has been silent for 12 hours. |
| Setpoint change failed, Pump ignored the setpoint change | The last change didn't take; it is retried after 30 minutes. |

#### Tuning

| If the room is… | Try |
| --- | --- |
| Too cold or too warm in all weather | Press `Calibrate`, or move both water points by the same amount. |
| Fine when mild, too cold in frost | Raise the cold point's water temperature. |
| Fine in frost, too warm when mild | Lower the mild point's water temperature. |
| Overshooting after changes | Lower `Room correction`. |
| Slow to recover after a cold spell | Raise `Room correction`. |

If `Learned curve offset` sits near ±3 °C, the curve is off by about that much. Calibrating fixes the curve and clears the offset.

#### Dashboard card

The integration includes a card for watching the controller. It loads automatically once control is enabled, with no separate install. Edit a dashboard, `Add card`, and search for **Gree Versati floor heating**. It picks your Floor heating thermostat by itself. In YAML:

```yaml
type: custom:gree-versati-floor-heating-card
entity: climate.gree_versati_<device_id>_floor_heating
hours_to_show: 72  # optional
```

It shows up to three graphs:
- **Floor heating water:** the controller's target and the curve on its own, against the pump's setpoint and the measured water. In advisory mode this is where you see what it would do.
- **Room and outdoor:** the room against its target, with heating periods shaded, the outdoor temperature the curve uses and, with a forecast, the outdoor temperature over the next hours.
- **Adjustments:** the sun cut and the learned offset, in °C.

### Development and tests

Run the tests with:

```bash
pip install -r requirements_test.txt
pytest
```

They cover the integration running in Home Assistant, the control maths, a simulated house the controller's defaults were tuned on (`tests/test_heating_simulation.py`), and the dashboard card's logic, run in an embedded JavaScript engine (QuickJS).

## From the original repository

Everything below comes from [`sarunasr/ha-gree-versati3`](https://github.com/sarunasr/ha-gree-versati3) and describes the integration this fork builds on. Installation is covered [above](#installation).

This integration is inspired by protocol behavior observed in [`tomikaa87/gree-remote`](https://github.com/tomikaa87/gree-remote), but implemented from scratch in this repository without vendoring or subprocess calls.

### Features

- Local UDP communication (no cloud dependency)
- AES `pack` payload support for both ECB and GCM devices (auto-detected)
- Config Flow + Config Entry setup
- DataUpdateCoordinator polling (default 30s, configurable)
- Number entities:
  - `HeWatOutTemSet` (20..60, step 1)
  - `WatBoxTemSet` (20..60, step 1)
- Switch entity:
  - `Pow` (power on/off)
- Select entity:
  - `Mod` (mode selector)
- Sensor entities (unavailable when missing from device response):
  - `HeWatOutTemSet`
  - `WatBoxTemSet`
  - `TemUn`
  - `AllErr`
  - `Mod`
  - `Pow`
- Services:
  - `gree_versati.set_param`
  - `gree_versati.get_params`

### Configuration Fields

- `host`: Heat pump IP or hostname
- `port`: UDP port (default `7000`)
- `device_id`: Device identifier used by protocol envelope
- `key`: 16-byte AES key string
- `timeout`: UDP timeout in seconds (default `5`)

Options flow:
- `scan_interval`: Polling interval in seconds (default `30`)

### Notes

- The AES key must be exactly 16 bytes (UTF-8 byte length).
- The integration does not log the key.
- If a parameter is not returned by the device, corresponding entities are marked unavailable.

### Getting `device_id` and `key`

If you do not know the `device_id` and `key`, you can obtain them with `gree-remote`:

1. Clone the tool:

```bash
git clone https://github.com/tomikaa87/gree-remote
cd gree-remote
```

2. Search/bind to retrieve device credentials:

```bash
python3 PythonCLI/gree.py search -b <gree-ip>
```

The output should include the discovered device ID and bound key.

Note: in many networks, `-b` should be the subnet broadcast address (for example `192.168.1.255`), but some setups may work using the device IP directly.

3. Verify credentials by reading a known parameter:

```bash
python3 PythonCLI/gree.py -c <gree-ip> -i <id> -k <key> get HeWatOutTemSet
```

If this returns a value, use the same `host`, `device_id`, and `key` in this Home Assistant integration.

#### Troubleshooting key retrieval

- Ensure your laptop/host and the heat pump are on the same subnet.
- Avoid guest Wi-Fi networks (client/AP isolation can block UDP device discovery).
- Try broadcast address for your subnet (for `/24`, usually `x.x.x.255`).
- If you have multiple interfaces (Ethernet + Wi-Fi), bind `gree.py` to the correct one:

```bash
python3 PythonCLI/gree.py --socket-interface <iface> search -b <broadcast_ip>
```

- If `search` fails but direct `get` works with known credentials, discovery traffic may be blocked while unicast traffic still works.

### Legal / Licensing Note

This repository is original MIT-licensed code.

Protocol behavior was implemented based on reverse engineering and public interoperability knowledge. No direct source copy from `gree-remote` is included.

You are responsible for complying with local laws, device warranties, and network/security policies when using reverse-engineered protocols.

### Development

Repository layout follows Home Assistant custom integration conventions with `custom_components/gree_versati`.
