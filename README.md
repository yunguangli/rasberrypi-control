# Picontrol app

Remote control of a Raspberry Pi over LAN: the Flet GUI (`src/main.py`) sends JSON ops
over HTTP to a console-only bridge (`src/pi.py`) that drives gpiozero. The two sides are
independent programs that never share imports — `tests/test_import_boundary.py` enforces it.

## Pi bridge (Raspberry Pi)

Only these three flat sibling files are copied to the Pi: `pi.py`, `pins.py`,
`protocol.py` (from `src/`).

```bash
pip install gpiozero              # or: sudo apt install python3-gpiozero
python3 pi.py --port 8642         # defaults: --host 0.0.0.0, port 8642
python3 pi.py --allow-code        # additionally: let the GUI run scripts
```

**Flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address. |
| `--port` | `8642` | TCP port the GUI connects to. |
| `--dev` | off | Run without Pi hardware (on a PC): mock pins, synthetic sensor values, plus the dev-only `set_input` op that simulates button/PIR presses. |
| `--allow-code` | **off** | Enable the GUI's Script section (`POST /script`): run a Python program typed into the GUI as a subprocess on this machine. Without the flag every script submission is refused with `code execution disabled — start pi.py with --allow-code`. |

**Endpoints:** `POST /op` (one op), `GET /state` (all pin states + script status),
and with `--allow-code`: `POST /script` (start a job) and `POST /script/stop`
(SIGTERM, then SIGKILL after 2 s). Every response is structured JSON — never a
traceback. Logs one line per transaction.

**Script safety:** starting a job releases every gpiozero device first (gpiozero
can't share pins across processes) and refuses pin ops until the job ends;
`all_off` stops a running job before switching things off. Since
`--allow-code` lets anyone on the LAN execute code on the Pi, only enable it
on a network you trust (home/school LAN).

**Temperature sensor note:** gpiozero has no DHT22 driver (gpiozero issue #254 is still
open), so the "Room temp" card shows a clear fault until the optional Adafruit driver
is installed on the Pi:

```bash
sudo apt install libgpiod2
pip install adafruit-circuitpython-dht
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
