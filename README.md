# Picontrol app

Remote control of a Raspberry Pi over LAN: the Flet GUI (`src/main.py`) sends JSON ops
over HTTP to a console-only bridge (`src/pi.py`) that drives gpiozero. The two sides are
independent programs that never share imports — `tests/test_import_boundary.py` enforces it.

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
