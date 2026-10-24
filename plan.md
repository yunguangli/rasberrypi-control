# picontrol — implementation plan

Design decisions settled in conversation with the user (2026-10-06). `plan.txt` is the
original brief; this file is the concrete plan derived from it.

## 1. Goal

Remote control of a Raspberry Pi's GPIO from a PC/laptop:

- **PC side** — Flet GUI (`src/main.py`) sends formatted gpiozero-intent commands to the Pi.
- **Pi side** — `pi.py` (`src/pi.py`, console-only transceiver — a controller bridge, no UI)
  interprets them into real gpiozero operations, logs every op on the console, and returns
  status / sensor values.

**Hard constraints (user, 2026-10-06):**

1. **Easy separation** — deployment to the Pi = copying 3 files (see §6). PC and Pi code are
   developed together but fenced by an enforced import boundary (§7).
2. **Pi side has exactly one third-party dependency: gpiozero.** No aiohttp/Flask/uvicorn —
   the webserver is stdlib `http.server`. Verified feasible: stdlib suffices; Flet's internal
   server cannot host custom routes (it only speaks Flet's control protocol), so it's no option.
3. **Low resource footprint on the Pi** — one small threaded stdlib server, no framework
   runtime, no asyncio event loop; idle cost ≈ a few MB of RAM and near-zero CPU between
   requests. See §5.

## 2. Decisions (settled)

| Question | Decision |
|---|---|
| Transport | **Mini webserver on the Pi** (Pi = HTTP server, PC = client). Curl-debuggable, no inbound hole on the PC side. lanlink/P2P rejected: fax call semantics (ring/busy/pacing) don't fit interactive commands; its `discovery.py` may be lifted later for auto-discovery. |
| Command format | **Whitelisted JSON ops** — never `eval` gpiozero strings (that would be RCE on the Pi). |
| Sensor reads | **Poll on demand** — GUI polls every 1–2 s; no push channel in v1. |
| Pin list | **Shared pin profile** — one module drives *both* `pi.py` validation and GUI layout, so they cannot drift. |
| Devices planned | All four roles: outputs (LED/relay/buzzer), inputs (button/PIR), sensors (temp/distance/light), PWM (dimmer/motor). |
| Light sensor | **Implemented via an MCP3008 ADC** on SPI0 (`MCP3008(channel=0)`); the profile entry is a sentinel BCM (`-1`) and `needs_adc` stays `False`. `needs_adc=True` remains for a future analog pin with no ADC fitted (rendered disabled). |
| Board visualization | **Hybrid**: image-2 pinout cell design (function-family colors, physical # + BCM + alt-function) arranged image-1 style as a **2×20 horizontal strip** — the reference diagram rotated 90° clockwise: its left column (1,3…39) is the top row and its right column (2,4…40) the second row, pins 1/2 at the right end — plus live state overlay. Generated from the header table — never transcribed (image 1 had pins 12/14 swapped). |
| External emulator (nosix/raspberry-gpio-emulator) | **Not a dependency.** Abandoned py3.5-era code, separate Tk window, can't emulate timing-based sensors anyway. Optional later spike: `pi.py --dev-pins=emulator` + `GPIOZERO_PIN_FACTORY=rpigpio`. Mock pins + our own Board panel cover it. |
| Pi webserver | **stdlib `http.server`** (`ThreadingHTTPServer`), ~50 lines. No pip-installable framework anywhere on the Pi; gpiozero is the only third-party import. Flet's internal server ruled out (no custom-route API). |
| Server model | **Threaded, not asyncio.** `ThreadingHTTPServer` gives each request its own thread, so blocking gpiozero calls run directly in the handler — no executor, no event loop (simpler than the original asyncio design). |
| Pi deployment | **Flat 3-file copy set**: `pi.py` + `pins.py` + `protocol.py` sit as siblings in `src/`; copy them to any folder on the Pi and `python3 pi.py` runs. Enforced by boundary tests (§7). |

## 3. Architecture: MVC over a shared domain layer

Professional layering — MVC on each side, with a shared, dependency-free domain/model package
both sides import so the wire contract has exactly one definition:

```
┌──────────────────────────── PC (Flet app, src/main.py) ────────────────────────────┐
│  VIEW      Flet controls: Board strip, section cards, activity log                 │
│  CONTROLLER  async handlers: send ops, poll loop, optimistic update + rollback      │
└───────────────┬─────────────────────────────────────────────────────────────────────┘
                │  HTTP + JSON  (src/protocol.py: encode/decode/validate)
┌───────────────▼──────────────── Raspberry Pi (src/pi.py) ───────────────────────────┐
│  CONTROLLER  stdlib ThreadingHTTPServer + op dispatcher (whitelist vs pin profile)  │
│  MODEL       gpiozero devices, created once and cached; each request = own thread   │
│  VIEW        console transceiver log: every op in, every reply out                  │
└───────────────┬─────────────────────────────────────────────────────────────────────┘
                │
        ┌───────▼──────────────── shared model (src/) ────────────────────────────────┐
        │  pins.py     dataclasses: PinProfile, HEADER table (40 positions)           │
        │  protocol.py dataclasses: Op, OpResult, PinState; roles, validation         │
        └─────────────────────────────────────────────────────────────────────────────┘
```

- **Model** = pin profile + protocol + gpiozero state (Pi) / cached remote state (PC).
- **View** = Flet controls (PC) / console log (Pi). Views render models; never talk HTTP.
- **Controller** = Flet event handlers + poll task (PC) / HTTP dispatch (Pi).
- `protocol.py`/`pins.py` are **stdlib-only, Flet-free, gpiozero-free** (same rule as
  lanlink modules) → unit-testable without either side running.
- **Import boundary (enforced by tests, §7):**

| File | Runs on | May import |
|---|---|---|
| `main.py` | PC | flet, stdlib, `pins`, `protocol` |
| `pi.py` | Pi (+ PC in `--dev`) | stdlib, **gpiozero**, `pins`, `protocol` |
| `pins.py`, `protocol.py` | both | **stdlib only** |

### Dataclasses (core contract)

```python
# src/pins.py
@dataclass(frozen=True)
class PinProfile:
    name: str                 # "Living room lamp" — shown on cards
    bcm: int                  # BCM number for gpiozero (None for non-GPIO cells)
    physical: int             # header position 1..40 (Board strip placement)
    role: Role                # OUTPUT | INPUT | PWM | SENSOR | POWER | GND | ID
    function: FunctionColor   # GPIO | POWER | GND | UART | SPI | I2C | ID (strip ring color)
    alt_function: str = ""    # "SPI0_MOSI", "SDA1 I2C" …
    unit: str = ""            # "°C", "m", "lx"
    driver: str = ""          # gpiozero class hint: "DHT22", "HCSR04", "LED" …
    active_low: bool = False  # relay boards, pull-up buttons
    needs_adc: bool = False   # renders disabled until hardware exists

HEADER: tuple[PinProfile, ...]      # all 40 positions, generated once
PROFILE: dict[int, PinProfile]      # bcm -> profile for wired pins (the deployable set)

# src/protocol.py
@dataclass(frozen=True)
class Op:
    op: str                  # "digital_write" | "pwm_write" | "read" | "all_off" | …
    pin: int = 0
    value: float | int | None = None
    req_id: int = 0          # correlates log lines PC <-> Pi

@dataclass(frozen=True)
class OpResult:
    ok: bool
    req_id: int = 0
    value: float | int | bool | None = None   # read results
    error: str = ""                             # short reason, never a traceback

@dataclass(frozen=True)
class PinState:
    bcm: int
    direction: str           # "out" | "in" | "pwm"
    value: float | int | bool | None  # level, duty %, or sensor reading
    unit: str = ""
    fault: str = ""          # device error surfaced to the card
```

Role → allowed ops table lives next to the profile: `pi.py` rejects `pwm_write` on an INPUT
with `400 {"ok": false, …}`; the GUI never builds a control that would fail.

## 4. Wire protocol (v1)

- `POST /op` — body `{"op": "digital_write", "pin": 17, "value": 1, "req_id": 42}`
  → `200 {"ok": true, "req_id": 42}` or `400 {"ok": false, "error": "pin 17 is input-only"}`
- `GET /state` — one call returns `PinState[]` for every readable pin (one poll refreshes
  the whole UI); unknown/faulted pins carry `fault`.
- Ops: `digital_write`, `digital_read`, `pwm_write`, `pulse` (momentary), `read` (sensor),
  `all_off` (panic button), `noop` (liveness).
- Errors are always structured `OpResult`s — no stack traces on the wire.
- CORS: every reply carries `Access-Control-Allow-Origin: *` and `OPTIONS` preflight is
  answered (204), so a **web-build client** (phone browser) can call the API cross-origin;
  native/desktop clients are unaffected. Keep all of this plain http (no https→http).

## 5. Pi side — `src/pi.py` (console-only controller bridge)

**No UI on the Pi beyond log lines.** It is a bridge: socket in → gpiozero → socket out.

- **Webserver = stdlib `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`**
  (~50 lines: `do_POST` for `/op`, `do_GET` for `/state`). No third-party framework, ever.
- **Threaded, not asyncio**: each request runs in its own thread, so blocking gpiozero calls
  (`DistanceSensor` pulse timing included) execute directly in the handler — no executor, no
  event loop, no async plumbing.
- **Resource budget (user requirement)**: zero framework runtime; idle = 1 listening socket
  + the main thread, active = one short-lived thread per request. Expected footprint a few
  MB of RSS and near-zero CPU between polls. Devices are constructed **once** and cached
  (dict bcm → device), never per request.
- **Third-party imports: gpiozero only.** Everything else (`http.server`, `json`, `socket`,
  `logging`, `argparse`) is stdlib. Enforced by the import-boundary test (§7). One optional
  exception: the DHT22 temperature sensor — gpiozero has **no DHT driver** (gpiozero issue
  #254 is still open), so `pi.py` uses `gpiozero.DHT22` if a build ever ships one, else the
  Adafruit CircuitPython driver **only when installed** (`pip install
  adafruit-circuitpython-dht`, which installs the `adafruit_dht` module; built with
  `use_pulseio=False` and released with `exit()`); with neither present the card shows a
  clear fault instead of an AttributeError. Both imports are lazy, so they are listed in
  the boundary allowlist.
- Console view: one line per transaction, e.g.
  `12:01:04 #42 → digital_write 17 = 1    ← ok`.
- Script jobs (`--allow-code`): the child's merged stdout+stderr is captured by the bridge
  (so it does **not** appear on the Pi console unless `--console` is given) and returned with
  `GET /state` as a tail of the last 200 lines / 16 KiB; when older output is dropped the tail
  starts with `… earlier output truncated …`. A script run directly from a shell prints to
  that shell.
- CLI: `python3 pi.py --port 8642 [--dev] [--allow-code [--console]] [--host 0.0.0.0]`.
- Modes:
  - default — real pins (lgpio/RPi.GPIO as provided by Raspberry Pi OS),
  - `--dev` — sets `GPIOZERO_PIN_FACTORY=mock` before gpiozero import, plus synthetic sensor
    values (DHT/HC-SR04 can't be timing-emulated) so the GUI is fully developable on the PC,
  - `--dev` also whitelists a `set_input` op so clicking an input cell on the Board strip
    simulates a button press (the one useful idea from the nosix emulator, ~20 lines).

### Deployment to the Pi (easy separation)

Everything Pi-side lives as **flat siblings in `src/`** — no packages, no `sys.path` tricks,
no `src.` prefixes, so imports (`import pins`) resolve identically on both machines:

```
copy to Pi:  src/pi.py  src/pins.py  src/protocol.py   → any folder, e.g. ~/picontrol/
run:         pip install "gpiozero==1.6.2" "setuptools<81"   # or: apt install python3-gpiozero
             python3 pi.py --port 8642
```

Target Pi environment: **Python 3.11 + gpiozero 1.6.2** (RPi.GPIO backend). Pin the version:
unpinned, pip now installs gpiozero 2.x, which resolves pin factories differently. gpiozero
1.6.x also imports `pkg_resources` for its mock pins, so `setuptools<81` must stay installed
for `--dev` and the test suite. One teardown quirk to know about: 1.6.x's
`DistanceSensor.close()` sets `_trigger = None` *before* stopping its sampler thread, so a
read in flight dies with `AttributeError: 'NoneType' object has no attribute 'pin'`; the
dispatcher stops the sampler thread first (`_queue.stop()`) and then closes the device.

`main.py` (Flet) never ships to the Pi; the Pi set never imports flet. **The copy set is
self-contained by test**: a deploy-smoke test copies exactly those 3 files into a temp dir
and imports `pi` there (§7) — if the set ever stops being self-contained, CI fails before
you discover it on the Pi.

### Testing the Pi side on a PC (no Pi yet)

`python3 pi.py` **without** `--dev` resolves the real gpiozero pin factory at startup and
exits with a hint if none loads (a PC has no GPIO):

```
pi.py: no usable GPIO pin factory (BadPinFactory: Unable to load any default pin factory!)
pi.py:   on a Raspberry Pi: install lgpio (sudo apt install python3-lgpio) or RPi.GPIO
pi.py:   on a PC without hardware: run with --dev (mock pins + synthetic sensors)
```

For PC development use `--dev`, which switches to gpiozero's **mock pins**
(`GPIOZERO_PIN_FACTORY=mock` + `GPIOZERO_MOCK_PIN_CLASS=mockpwmpin` for PWM support) and
serves **synthetic** DHT22/HC-SR04 values. Outputs/PWM behave exactly as on hardware, and
the dev-only `set_input` op drives button/PIR levels so the GUI shows real input changes:

```powershell
uv run python src/pi.py --dev        # terminal 1: mock bridge on :8642
uv run flet run                      # terminal 2: GUI → Connect (127.0.0.1:8642)

# simulate a door-button press / release (dev mode only), from any terminal:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8642/op `
  -ContentType application/json -Body '{"op":"set_input","pin":24,"value":1}'
Invoke-RestMethod http://127.0.0.1:8642/state
```

On a live bridge, `set_input` is rejected with
`set_input is only available in --dev mode`, so a simulation can never be mistaken for a
real hardware control.

## 6. PC side GUI — `src/main.py`

Flet 1.0 facts verified via Flet MCP: `Switch`/`GridView` exist; **`Timer` does not exist**
→ polling is `page.run_task` + `asyncio.sleep` loop; **`Wrap` does not exist** → `GridView`
or fixed `Row`s; **`LayoutBuilder` does not exist** → responsive via `page.on_resize`.

### Phone portrait mode (user requirement)

The GUI must fit a phone held in portrait and be installable on it. Verified Flet 1.0
hooks: `page.on_resize` delivers `width`/`height` (logical px), and
`page.set_allowed_device_orientations([DeviceOrientation.PORTRAIT_UP])` locks orientation
on mobile — it **raises on desktop**, so call it only when `page.platform` is android/ios.

- **One breakpoint**: `width < 600` = narrow mode; re-render the shell on `on_resize`.
- **Narrow layout**: navigation moves to a bottom `NavigationBar` with three pages —
  **Board | Control | Script**; the Board page shows the full **vertical pinout**
  (20 rows × 2 cols, labels beside cells — the pinout.xyz style fits portrait naturally)
  with maximum detail per pin (physical #, BCM, alt function, family, wired device, live
  state, fault). Control starts directly with the device cards (no 1000 px board above
  them) and Script has the whole page for editor + console.
- **Wide layout**: a fixed header, the compact **2×20 board strip pinned** above the paged
  area (so the whole header stays visible while scrolling), and `Tabs` for
  **Board | Control | Script**. The Board tab is the same detailed view as the phone page.
- **Install routes** (either way the phone reaches `pi.py`'s host:port over the same WiFi):
  1. **Native APK** — `flet build apk -v` (documented in README); needs Android SDK + JDK
     on the PC. HTTP runs from Python, not a browser → no CORS concerns.
  2. **Web build** — `flet build web` served over the LAN, add-to-home-screen. Browser
     client → `pi.py` must answer CORS (see §4); also keep everything plain http to avoid
     https→http mixed-content blocking.

App shell (the shell never scrolls; only the selected page does):

```
Header (fixed)   "picontrol" · host:port field · quick-targets popup · Connect ·
                 status chip (dot + "connected · 12 ms" / "can't reach the Pi")
Navigation       wide: Tabs (Board | Control | Script) under the pinned strip
                 narrow: NavigationBar at the bottom
Board page       every pin as a detail card: #physical, name, BCM, alt function,
                 family colour+label, wired device, live state chip, fault text,
                 legend, "wired only" filter; tap a wired output = toggle
Control page     Controls grid  (LEDs, relays, RGB swatches, buzzer pulse+tones,
                                 dimmer/motor sliders; "ALL OFF" in the heading)
                 Monitoring      (input indicators + sensor value cards)
                 Activity        (last 16 ops, collapsible, clearable)
Script page      status chip · Run / Stop · Clear / Indent / Outdent / Examples,
                 editor (monospace) and console (stdout+stderr tail) side by side
                 on wide screens, stacked on narrow ones
```

Type scale (deliberately generous — read at arm's length, often on a phone):
12 px pin numbers/meta, 13 px subtitles and log, 15 px body, 17 px card titles,
20 px section headings, 28 px sensor values, 14 px script editor, 13 px console.
Minimum contrast for secondary text is `#52606D` on white (was `#78909C`).

Controller behaviours:

- **State ownership**: widgets show *Pi* state. Toggle → optimistic flip → revert + red
  flash if reply isn't ok; poll loop reconciles drift every 1–2 s.
- **Connection resilience**: unreachable Pi → red dot, widgets disabled (never frozen),
  backoff retry; every failure logged to the activity log.
- Sections **rendered from `PROFILE`** — adding a pin is a one-line edit.

## 7. Testing

- pytest config already in `pyproject.toml` (`testpaths=tests`, `pythonpath=src`,
  `asyncio_mode=auto`); run `uv run pytest`, single test `uv run pytest tests/test_main.py`.
- Known env prerequisite: Flutter integration tests need **Windows Developer Mode**
  (symlink support) — see AGENTS.md.
- Layers:
  1. `protocol`/`pins` — pure unit tests (encode/decode/validate/role-ops table).
  2. **Import-boundary test** — AST-parse all four files; fail if `pi.py`/`pins.py`/
     `protocol.py` import anything outside `{stdlib} + gpiozero`, or if `main.py` imports
     gpiozero. Fences the separation rule from day one.
  3. **Deploy-smoke test** — copy `pi.py`+`pins.py`+`protocol.py` into `tmp_path`, run
     `python -c "import pi"` there with `GPIOZERO_PIN_FACTORY=mock`. This executes the
     actual copy-to-Pi workflow on every test run.
  4. `pi.py` dispatcher — `GPIOZERO_MOCK_PIN_FACTORY`, no network needed; assert op →
     `OpResult`, bad op → structured error.
  5. GUI — existing `flet_app` fixture with the HTTP layer stubbed.
- The pytest suite uses mock pins; **the suite never needs a real Pi**.

## 8. Build order — status 2026-10-06

1. ✅ `src/pins.py` — `PinProfile`, 40-position `HEADER`, wired `PROFILE`, role/ops table.
2. ✅ `src/protocol.py` — `Op`/`OpResult`/`PinState`, encode/decode/validate.
3. ✅ Unit tests for 1–2 **plus the import-boundary test** — boundary fenced first.
4. ✅ `src/pi.py` — stdlib threaded HTTP + dispatcher, console log, `--dev` mode
   (mock pins need `GPIOZERO_MOCK_PIN_CLASS=mockpwmpin` for PWM; inputs are driven
   with `pin.drive_high/drive_low`), plus the deploy-smoke test (flat 3-file copy
   imported from a temp dir).
5. ✅ Dispatcher + HTTP integration tests (real server on an ephemeral port).
   `uv run pytest` → **89 passed** for everything except the GUI test.
6. ✅ `src/main.py` — portrait-first responsive GUI (breakpoint 600 px: wide =
   horizontal strip + wrap cards, narrow = 20-row vertical pinout + portrait lock),
   `tests/test_main.py` drives it via the flet pytest plugin. **Blocked on this
   machine** by Windows Developer Mode (symlink support) — see AGENTS.md; enable
   it once, then the test runs everywhere after.
7. ✅ E2E smoke: `src/pi.py --dev` booted as a process and driven over HTTP with
   every op (incl. structured errors + CORS) — console log verified. Remaining:
   manual `uv run flet run` look and a real-Pi run.
8. ✅ `pyproject.toml`: gpiozero in the dev group (2.x for `uv run pytest`; the Pi
   target 1.6.2 is exercised with a separate 3.11/1.6.2 environment — see AGENTS.md).

## 9. Out of scope (v1)

- lanlink-based discovery (host:port entry only; lift `discovery.py` later if typing IPs
  gets old), push/SSE sensor streaming, light sensor without ADC, auth (LAN-only; add a
  shared token if the network isn't trusted).
