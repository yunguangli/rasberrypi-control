# Picontrol app

Remote control of a Raspberry Pi over LAN: the Flet GUI (`src/main.py`) sends JSON ops
over HTTP to a console-only bridge (`src/pi.py`) that drives gpiozero. The two sides are
independent programs that never share imports — `tests/test_import_boundary.py` enforces it.

You can drive the board two ways:

- **Directly** — switches, sliders, RGB swatches, buzzer notes and the Script editor.
- **By typing** — the **Agent** page turns “turn on the status LED” into validated GPIO
  ops via an OpenAI-compatible model (local Ollama or a cloud endpoint such as
  OpenRouter). See [Agent — type a command](#agent--type-a-command).

New to the agent? Start with [How it works](#how-it-works-plain-english) — one
command traced end to end, with diagrams.

**Current focus:** the agent controls the board and reads the beginner sensors —
light, temperature, distance, motion, button, CPU temp/load. Robot cars and
hands are future skill layers on the same agent; see [Roadmap](#roadmap).

## Pi bridge (Raspberry Pi)

Only these three flat sibling files are copied to the Pi: `pi.py`, `pins.py`,
`protocol.py` (from `src/`).

```bash
pip install "gpiozero==1.6.2" "setuptools<81"   # or: sudo apt install python3-gpiozero
python3 pi.py --port 8642         # defaults: --host 0.0.0.0, port 8642
python3 pi.py --allow-code        # additionally: let the GUI run scripts
python3 pi.py --allow-code --console  # ...and echo their output on this terminal
```

Target environment on the Pi: **Python 3.11 + gpiozero 1.6.2** with the old
RPi.GPIO backend. Pin `gpiozero==1.6.2` (or `<2`): an unpinned
`pip install gpiozero` now resolves to 2.x, whose pin-factory handling differs.
Keep `setuptools<81` too — gpiozero 1.6.x imports `pkg_resources` for mock pins,
and setuptools 81 removed it.

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address. |
| `--port` | `8642` | TCP port the GUI connects to. |
| `--dev` | off | Run without Pi hardware (on a PC): mock pins, synthetic sensor values, plus the dev-only `set_input` op that simulates button/PIR presses. |
| `--allow-code` | **off** | Enable the GUI's Script section (`POST /script`): run a Python program typed into the GUI as a subprocess on this machine. Without the flag every script submission is refused with `code execution disabled — start pi.py with --allow-code`. |
| `--console` | off | With `--allow-code`: also print the script's stdout+stderr on this terminal while it runs (the GUI console keeps streaming its bounded tail either way). |

**Endpoints:** `POST /op` (one op), `GET /state` (all pin states + script status),
and with `--allow-code`: `POST /script` (start a job) and `POST /script/stop`
(SIGTERM to the job's process group, then SIGKILL after 2 s). Every response is
structured JSON — never a traceback. Logs one line per transaction.

**Script safety:** starting a job releases every gpiozero device first (gpiozero
can't share pins across processes) and refuses pin ops until the job ends;
`all_off` stops a running job before switching things off, and shutting the
bridge down (`Ctrl-C` or SIGTERM) stops the job's whole process tree before
releasing the GPIO. The Script page's console shows merged stdout+stderr: the
last 200 lines / 16 KiB, with an `… earlier output truncated …` marker when
older output was dropped. Since
`--allow-code` lets anyone on the LAN execute code on the Pi, only enable it
on a network you trust (home/school LAN).

**Temperature sensor note:** gpiozero has no DHT22 driver (gpiozero issue #254 is still
open), so the "Room temp" card shows a clear fault until the optional Adafruit driver
is installed on the Pi:

```bash
sudo apt install libgpiod2
pip install adafruit-circuitpython-dht
```

The module that package installs is `adafruit_dht`; the bridge builds
`DHT22(pin, use_pulseio=False)` (bit-banging, as Adafruit recommends on Linux
SBCs) and releases it through the driver's `exit()` method.

**Light sensor note:** the "Light (ADC)" card reads an MCP3008 over SPI0. With
the `spidev` package installed and SPI enabled it uses hardware SPI; otherwise
gpiozero bit-bangs SPI (works, but slower) and the bridge prints one startup
hint instead of a Python `SPISoftwareFallback` warning:

```bash
pip install spidev                 # or: sudo apt install python3-spidev
sudo raspi-config                  # Interface Options → SPI → enable, then reboot
```

Run the bridge on a PC instead (no hardware — mock pins + synthetic sensor values, plus
the dev-only `set_input` op to simulate button/PIR presses):

```powershell
uv run python src/pi.py --dev
```

## Run the app

### uv

Run as a desktop app:

```bash
uv run flet run
```

Run as a web app:

```bash
uv run flet run --web
```

For more details on running the app, refer to the [Getting Started Guide](https://flet.dev/docs/).

## Agent — type a command

The **Agent** page is the fourth tab (wide screens) or the fourth bottom-nav item
(portrait). Type a sentence, press Send, and the agent turns it into real GPIO
actions on the Pi — no code required.

```
you:    turn on the status LED
agent:  status_led.on → on
        Status LED is on.
```

The chips are **receipts**: a green chip is an action that actually reached the
Pi, a red one is a problem. The next sections explain the whole path; the setup
details start at [Configure a model](#configure-a-model).

**In this section:** [How it works](#how-it-works-plain-english) ·
[What the Pi receives](#what-the-pi-actually-receives) ·
[From JSON to a pin](#from-json-to-a-voltage-on-a-pin) ·
[Configure a model](#configure-a-model) ·
[Tools](#what-the-agent-can-do) ·
[Sensors](#reading-sensors-with-the-agent) ·
[Safety](#behaviour-and-safety) ·
[Pi traffic](#see-exactly-what-goes-to-the-pi) ·
[Troubleshooting](#when-something-goes-wrong) ·
[Roadmap](#roadmap)

### How it works (plain English)

**In one sentence:** your sentence → the model picks a tool → the PC turns it
into an op → the Pi applies it to a pin → the result comes back as a chip.

Your sentence passes through three programs. Only the PC app is this project:

```
   You                PC (Agent)                  Model                 Pi (pi.py)
    │  "turn on LED"     │                          │                       │
    ├───────────────────►│  messages + tool menu    │                       │
    │                    ├─────────────────────────►│                       │
    │                    │  tool_call: status_led   │                       │
    │                    │◄─────────────────────────┤                       │
    │                    │  {"op":"digital_write", …}                       │
    │                    ├─────────────────────────────────────────────────►│
    │                    │                          │      gpiozero.on()    │
    │                    │                          │      BCM 17 = 3.3 V   │
    │                    │  {"ok":true, …}          │                       │
    │                    │◄─────────────────────────────────────────────────┤
    │   green chip       │                          │                       │
    │◄───────────────────┤                          │                       │
```

> The PC talks to the model and to the Pi **separately**. The Pi never contacts
> the model, and your GPIO ops stay on the local network.

1. **You → the PC.** You type; nothing leaves the machine yet. The GUI attaches
   your sentence to a **tool menu** built from `pins.PROFILE`.
2. **PC → model.** It sends the conversation and the menu to the model
   (OpenRouter over HTTPS, or Ollama locally). The model reads the menu and
   chooses one entry: a **tool call**, e.g. `status_led` with
   `{"action": "on"}`. It is JSON, not Python.
3. **PC → PC.** `capabilities.py` checks that the device exists, that the action
   is allowed for it, and that the value is in range. It compiles the call into
   an **op** — a small JSON object (`op`, `req_id`, `pin`, `value`). A bad call
   never reaches the Pi; the model receives a short error and can try again.
4. **PC → Pi.** The op is sent as one HTTP POST over WiFi/LAN:
   `{"op":"digital_write","req_id":3,"pin":17,"value":true}`.
5. **Pi → hardware.** `pi.py` looks up pin 17, gets the gpiozero device declared
   in `pins.PROFILE`, and calls the matching method (`dev.on()`). gpiozero talks
   to the Linux GPIO driver; the header pin goes to 3.3 V.
6. **Pi → PC.** `pi.py` replies `{"ok":true,…}`; the GUI adds a chip and the
   model receives the result as a tool message, so it can explain what happened.

> **Why not send Python?** Because the Pi would then have to run arbitrary code.
> Instead the Pi only accepts a fixed vocabulary of small ops, so the worst a
> bad model can do is ask for an invalid op — which is rejected. The one
> exception is `write_script`, and that never runs by itself
> (see [Behaviour and safety](#behaviour-and-safety)).

### What the Pi actually receives

The model's plan is **not** sent to the Pi. The PC expands it into individual
ops and sends those one at a time. For example, a song:

```
model says:  run_sequence {"steps":[
               {"device":"buzzer","action":"tone","hz":262},
               {"wait_ms":100},
               {"device":"buzzer","action":"tone","hz":262}, …]}
                          │
                          ▼  expanded on the PC — the list itself is never sent
POST /op {"op":"tone","req_id":20,"pin":23,"value":262}  ──► Pi plays 400 ms
        sleep 100 ms on the PC        (nothing is sent)
POST /op {"op":"tone","req_id":20,"pin":23,"value":262}  ──► Pi plays 400 ms
        sleep 100 ms on the PC        (nothing is sent)
POST /op {"op":"tone","req_id":20,"pin":23,"value":294}  ──► Pi plays 400 ms
```

So the Pi never sees a melody, a list, a duration, or a file. It sees one
`tone` at a time and has no idea it is playing a song. The gaps are `sleep()`
calls on the PC, not network traffic.

### From JSON to a voltage on a pin

This is the part that happens inside `pi.py`:

```
{"op":"tone","pin":23,"value":262}
  → parse_op()                 Op(op="tone", pin=23, value=262)
  → dispatch()                 pins.PROFILE[23] → driver "TonalBuzzer"
  → gpiozero.TonalBuzzer(23)   built once, then cached
  → dev.play(262)              PWM at 262 Hz, 50 % duty
  → RPi.GPIO / lgpio           Linux GPIO driver
  → BCM 23 (physical pin 16)   3.3 V square wave
  → passive piezo buzzer       262 Hz note
```

`pi.py` then sleeps 400 ms and calls `dev.stop()`; the pin goes low and the
sound stops. For an LED, `dev.on()` simply drives the pin high. The device
classes and pin numbers are declared in `pins.py`, so the JSON can only select
from devices the Pi already knows.

### The four sizes of action

The model has four ways to act, from smallest to largest:

| Tier | Tool | Example | What the Pi receives |
|---|---|---|---|
| 1 | one device tool (`status_led`, `buzzer`, …) | “turn on the status LED” | one `POST /op` |
| 2 | `blink` | “blink it 3 times” | 6 POSTs (3 on + 3 off); gaps are local |
| 3 | `run_sequence` | “play Happy Birthday” | one POST per note (plus local waits; ≤ 64 steps) |
| 4 | `write_script` | “write a script that …” | **nothing** until *you* press Run on Pi |

`all_off`, `ask_user` and `remember` are helpers rather than hardware tiers.

### Words used here

| Word | In plain English |
|---|---|
| **Model** | The text AI (OpenRouter in the cloud or Ollama on your machine) that reads your sentence and picks a tool. |
| **Tool call** | The model's JSON answer, e.g. “call `status_led` with `action: on`”. The model cannot send executable code. |
| **Op** | The tiny command the Pi understands: `op`, `pin`, `value`, `req_id`. |
| **Bridge** | `pi.py`, the small HTTP server on the Pi that validates ops and drives the pins. |
| **gpiozero** | The Python library the bridge uses to talk to GPIO pins. |
| **BCM / physical pin** | Two numbering schemes for the same header: BCM 17 = physical pin 11, for example. |
| **PWM** | Very fast on/off switching used for dimming, motor speed, and buzzer tones. |

### Configure a model

Click the gear on the Agent page (or set the environment variables below). Any
OpenAI-compatible `/chat/completions` endpoint that supports **tool calling** works.

| Field | OpenRouter | Local Ollama |
|---|---|---|
| Base URL | `https://openrouter.ai/api/v1` | `http://localhost:11434/v1` |
| Model | any tool-capable model, e.g. `vendor/model` | any tool-capable model, e.g. `qwen2.5:7b` |
| API key | your `sk-or-…` key | leave blank |

> **Pick a specific model** for predictable tool calls, e.g.
> `nvidia/nemotron-3-ultra-550b-a55b:free`. `openrouter/free` is a *router*: it
> chooses a different free model each turn, so behavior (and tool-call quality)
> varies.

Settings are stored outside the repo, at
`$XDG_CONFIG_HOME/picontrol/agent.json` (usually `~/.config/picontrol/agent.json`),
with mode `0600` because it contains the API key in plain text. The key is only
sent to the configured base URL.

Environment defaults (a saved setting wins over the environment):

| Variable | Meaning |
|---|---|
| `PICONTROL_LLM_BASE` | Base URL; default `http://localhost:11434/v1`. |
| `PICONTROL_LLM_MODEL` | Model name. Empty = not configured (Send stays disabled). |
| `PICONTROL_LLM_KEY` | Bearer token for the endpoint. |
| `PICONTROL_FACTS_PATH` | Where learned aliases are stored; default `~/.config/picontrol/facts.json`. |
| `PICONTROL_STT_MODEL` | Reserved for the future voice layer; unused today. |

Send is enabled only when a model is configured **and** the Pi is connected. The
status line reads `ready · <model> @ <host>/<path>`, `no model configured`, or
`waiting for the Pi…`. The settings dialog has one-tap **OpenRouter** / **Ollama**
fill buttons and previews the exact URL it will call
(`POST …/chat/completions`), so a mistyped path is visible before saving. A path
ending in `/vi` or `/vl` is repaired to `/v1` automatically — the digit `1` is
easy to mistype for a letter.

### What the agent can do

Device tools are generated from `pins.PROFILE`, so adding a device to `pins.py`
adds its tool automatically:

| Tool | Actions |
|---|---|
| `status_led`, `relay_1`, `relay_2` | `on`, `off`, `pulse` |
| `rgb_led` | `on`, `off`, `color` (`rgb` = `0xRRGGBB`) |
| `buzzer` | `on`, `off`, `pulse`, `tone` (`hz` 200–5000) |
| `desk_dimmer` | `set` (`percent` 0–100) |
| `motor` | `drive` (`percent` −100…100, negative = reverse) |
| `door_button`, `pir_sensor` | `read` |
| `room_temp`, `distance`, `light_adc`, `cpu_temp`, `system_load` | `read` |

Plus six global tools:

- `all_off` — switch every output off.
- `blink` — N on/off cycles on one output (`device`, `times`, `on_ms`, `off_ms`);
  this is what "blink the status LED 3 times" uses.
- `run_sequence` — custom timing patterns with optional `wait_ms` pauses
  (beep, sweep, melodies; up to 64 steps). Each buzzer `tone` lasts 400 ms on
  the Pi, so `wait_ms` controls the rhythm.
- `ask_user` — ask a clarifying question and wait for the answer.
- `remember` — persist an alias or fact (e.g. `led = status_led`) to the facts file.
- `write_script` — offer a Python program as a transcript card; **Open in Script**
  loads it into the editor. **It is never run automatically**; you review it and
  press **Run on Pi** (which still needs `pi.py --allow-code`).

### Reading sensors with the agent

Sensors are read-only: the agent gets a **read**, never a write. Ask in plain
English and check the chip:

| You say | Tool | What the Pi returns |
|---|---|---|
| “What's the room temperature?” | `room_temp` | °C from the DHT22 on BCM 4 |
| “How hot is the Pi?” | `cpu_temp` | the Pi's die temperature |
| “How bright is it?” | `light_adc` | 0–100 % from the LDR on the MCP3008 |
| “How far is the wall?” | `distance` | metres from the HC-SR04 |
| “Is there motion?” | `pir_sensor` | motion / idle |
| “Is the door open?” | `door_button` | pressed / released |
| “What's the system load?” | `system_load` | 1-minute load average |

For example, “what's the room temperature?” becomes one read op —
`{"op":"read","req_id":9,"pin":4}` — and no pin is driven.

Some sensors need a little setup on the Pi; the [Pi bridge](#pi-bridge-raspberry-pi)
section has the details:

- **DHT22 / temperature:** needs the optional Adafruit driver. Without it the
  card/chip shows a readable fault instead of crashing.
- **Light:** needs the MCP3008 and SPI0 enabled; without them you get a readable
  fault (or the slower software-SPI fallback).
- **Distance:** HC-SR04 echo must go through a level divider.
- **Heating elements:** a temperature *sensor* is safe to read; a heater is an
  actuator. Switch it with a relay and its own supply, add a fuse and a thermal
  cutoff, and never power it from the Pi's 3V3/5V rail.

In `pi.py --dev` mode the timing-based sensors (DHT22, HC-SR04, LDR, CPU/load)
serve synthetic values, and the dev-only `set_input` op simulates the button and
PIR, so you can practise the agent commands before the hardware is wired.

Adding another instance of an existing sensor (a second DHT22, say) is one
`PinProfile` row in `pins.py`; a **new sensor type** also needs a small driver
branch in `pi.py` (build the device + read its value) and a synthetic value for
`--dev`. The card and the agent tool then appear automatically.

Wiring and physical checks live in `testing.md`: the wiring table in §0, the
manual sensor tests HW-50…HW-71, and the agent read tests HW-153…HW-160.

### Behaviour and safety

- **Execution is immediate**, like the switches: the agent acts and then reports
  with an action chip. There is no approve step. Use **Stop** to cancel between
  steps (the request already in flight still completes).
- The model can only emit the whitelisted ops above. It cannot invent a pin, run
  shell commands, or bypass `pi.py`'s validation; out-of-range values and unknown
  devices come back as readable errors, which the model sees and corrects.
- **Ambiguity is asked once.** “Turn on the LED” when both the Status LED and the
  RGB LED exist produces a question card with tappable options. The answer is
  stored with `remember`, so the next turn goes straight to the device.
- **Facts** are a flat JSON map of learned aliases at `PICONTROL_FACTS_PATH`
  (default `~/.config/picontrol/facts.json`, next to the settings), written
  atomically with mode `0600`. Delete the file to make the agent ask again.
- The transcript keeps the last 40 rows; **Clear** also resets the model's
  conversation history (each turn is stored whole, so a tool reply is never
  orphaned from its call).
- Errors from the endpoint (bad key, unreachable host, malformed reply) appear
  as a red chip; the GUI stays usable and Send re-enables.

### See exactly what goes to the Pi

Click the **cable icon** in the Agent toolbar to show the **Pi traffic** panel.
For every op the agent sends it shows the exact JSON body and the reply:

```
12:03:44 → {"op":"digital_write","pin":17,"value":true,"req_id":3}
         ← {"ok":true,"req_id":3,"value":true}
```

A song produces one pair of lines per note, not one big message:

```
12:04:10 → {"op":"tone","req_id":20,"pin":23,"value":262}
         ← {"ok":true,"req_id":20,"value":262.0}
12:04:11 → {"op":"tone","req_id":20,"pin":23,"value":262}
         ← {"ok":true,"req_id":20,"value":262.0}
```

Reading a sensor shows a `read` op instead of a write:

```
12:05:00 → {"op":"read","req_id":12,"pin":4}
         ← {"ok":true,"req_id":12,"value":22.4}
```

The gaps between notes do not appear because nothing is sent during them.

The panel is hidden by default and the same icon hides it again. It keeps the
last 40 lines, scrolls with new traffic, and **Clear** empties it along with the
transcript. Only agent ops are shown there; the Control page's activity log
still records direct switch/slider actions.

### When something goes wrong

If a turn fails, the red chip names the endpoint it called:

| Chip | Cause |
|---|---|
| `HTTP 404 … /api/vi/chat/completions` (or `/api/vl`) | Base URL typo — it must end with `/v1` (`https://openrouter.ai/api/v1`). |
| `HTTP 401 …` | Wrong/expired API key, or a key sent to a local endpoint that does not need one. |
| `HTTP 400 … model …` | The model name is wrong or that model cannot call tools. |
| `model ran out of output tokens … (finish_reason=length)` | A reasoning model spent the whole output budget before calling a tool. Use a specific model, a shorter request, or raise `MAX_OUTPUT_TOKENS` in `agent.py`. |
| `model unreachable at …` | Host/port/network; for Ollama check `ollama serve` and the `:11434` port. |

## Roadmap

The long-term goal: one agent controls the board and whatever is attached to it,
by typed or spoken commands. The current focus is the sensor/actuator bench.

| Stage | What | State |
|---|---|---|
| **Now** | Typed agent, all current actuators and sensors, Pi traffic panel, provider-quirk handling | done |
| **Next** | Beginner-sensor bench: light, DHT22, HC-SR04, PIR, button, CPU temp/load — wire, read and document each physical check | in progress |
| **Then** | Voice input: push-to-talk → speech-to-text → editable transcript → the same agent | planned |
| **Later** | Robot skills: car (differential drive + watchdog) and hand (servo poses), reusing the skill layer | future |
| **Later** | MCP server exposing the same tools; named scenes | future |

The seams are already there: `capabilities.py` and `agent.py` have no Flet or
gpiozero imports, so the same skills can be served over MCP or fed by a
speech-to-text front end. A car or hand adds a **skill layer** — higher-level
tools plus Pi-side safety such as a motor watchdog and servo angle limits — not
a rewrite of the agent.

## Build the app

### Android

```bash
flet build apk -v
```

For more details on building and signing `.apk` or `.aab`, refer to the [Android Packaging Guide](https://flet.dev/docs/publish/android/).

### iOS

```bash
flet build ipa -v
```

For more details on building and signing `.ipa`, refer to the [iOS Packaging Guide](https://flet.dev/docs/publish/ios/).

### macOS

```bash
flet build macos -v
```

For more details on building macOS package, refer to the [macOS Packaging Guide](https://flet.dev/docs/publish/macos/).

### Linux

```bash
flet build linux -v
```

For more details on building Linux package, refer to the [Linux Packaging Guide](https://flet.dev/docs/publish/linux/).

### Windows

```bash
flet build windows -v
```

For more details on building Windows package, refer to the [Windows Packaging Guide](https://flet.dev/docs/publish/windows/).

### Web

```bash
flet build web -v
```

For more details on building Web app, refer to the [Web Packaging Guide](https://flet.dev/docs/publish/web/).
