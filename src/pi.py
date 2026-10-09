#!/usr/bin/env python3
"""pi.py — Raspberry Pi side of picontrol: a console-only controller bridge.

Listens for JSON ops over HTTP (stdlib http.server, threaded), dispatches them
to gpiozero, and logs every transaction on the console. No UI beyond the log.

Third-party imports: gpiozero ONLY (enforced by tests/test_import_boundary.py),
plus the OPTIONAL Adafruit DHT driver, imported lazily inside _dht22() and used
only if the user installed it — gpiozero has no DHT22 class (issue #254).

Run on the Pi:
    pip install gpiozero              # or: apt install python3-gpiozero
    python3 pi.py --port 8642
    python3 pi.py --allow-code        # additionally let the GUI run scripts

Run on the PC (development: mock pins + synthetic sensor values):
    uv run python src/pi.py --dev

Part of the 3-file copy set for deployment: pi.py + pins.py + protocol.py
(flat siblings — copy them to any folder and `python3 pi.py` runs).
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gpiozero

import pins
import protocol
from pins import PinProfile, Role
from protocol import Op, OpResult, PinState, ProtocolError

# Direction strings reported by GET /state (kept small for the wire).
_DIRECTION = {
    Role.OUTPUT: "out",
    Role.INPUT: "in",
    Role.PWM: "pwm",
    Role.SENSOR: "sensor",
}

# Dev-mode synthetic sensor readings (DHT22 / HC-SR04 can't be timing-emulated
# through mock pins, so --dev serves smooth plausible values instead).
_SYNTHETIC = {
    "DHT22": lambda t: round(21.0 + 2.0 * math.sin(t / 60.0), 1),
    "HCSR04": lambda t: round(0.6 + 0.4 * math.sin(t / 15.0), 2),
    "LDR": lambda t: round(50.0 + 25.0 * math.sin(t / 45.0), 1),          # % bright
    "CPUTemperature": lambda t: round(48.0 + 6.0 * math.sin(t / 120.0), 1),
    "LoadAverage": lambda t: round(0.6 + 0.5 * math.sin(t / 30.0), 2),
}

# A tone op plays for this long, then stops — like pulse, but musical.
TONE_MS = 400

# A device that failed to construct is not retried on every poll; it is
# retried after this many seconds (unplugging/replugging a sensor recovers
# without restarting the bridge).
_DEVICE_RETRY_SECONDS = 30.0


class DeviceError(RuntimeError):
    """A known device-level failure.

    Reported as a structured error/fault (so the GUI card can show it) and
    logged WITHOUT a traceback — construction problems are not bugs.
    """


def _dht22(bcm: int):
    """Build a DHT22 temperature sensor on BCM `bcm`, exposing `.temperature`.

    gpiozero has never shipped a DHT driver (gpiozero issue #254 is still
    open), so gpiozero's own class is only used if a build ever gains one.
    Otherwise the optional Adafruit CircuitPython driver is used when the
    user installed it:

        pip install adafruit-circuitpython-dht   # Pi also: apt install libgpiod2

    With neither available this raises DeviceError carrying that advice —
    the card then shows the reason instead of an AttributeError.
    """
    cls = getattr(gpiozero, "DHT22", None)
    if cls is not None:
        return cls(bcm)
    try:
        import adafruit_circuitpython_dht as adafruit_dht
        import board
        pin = getattr(board, f"D{bcm}", None)
        if pin is None:
            raise DeviceError(f"board has no D{bcm} pin (BCM numbering expected)")
        return adafruit_dht.DHT22(pin)
    except DeviceError:
        raise
    except Exception as ex:  # ImportError on a PC, board unsupported, wiring ...
        raise DeviceError(
            f"no DHT22 driver: gpiozero has none (gpiozero issue #254) and "
            f"adafruit-circuitpython-dht is unavailable "
            f"({type(ex).__name__}: {ex})"
        ) from ex


class _LoadAverage:
    """Minimal stand-in for gpiozero.LoadAverage: `.load_average` + `.close()`.

    Why not gpiozero's: 1.6.2's implementation prints the open file object on
    every read (`print(repr(f))` shipped in the release), which floods the
    console once per poll. 2.0 fixed that, but reading /proc/loadavg is two
    stdlib lines and behaves identically on both versions.
    """

    def __init__(self, minutes: int = 1) -> None:
        if minutes not in (1, 5, 15):
            raise ValueError("minutes must be 1, 5, or 15")
        self._column = {1: 0, 5: 1, 15: 2}[minutes]

    @property
    def load_average(self) -> float:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[self._column])

    def close(self) -> None:
        pass  # no pin to release — kept for the Dispatcher device contract


def _log(req_id: int, description: str, result: OpResult) -> None:
    """One console line per transaction — pi.py's 'view'."""
    stamp = time.strftime("%H:%M:%S")
    mark = "ok" if result.ok else f"ERR {result.error}"
    print(f"{stamp} #{req_id} -> {description:<38} <- {mark}", flush=True)


def _describe(op: Op) -> str:
    if op.op in protocol.SYSTEM_OPS:
        return op.op
    text = f"{op.op} pin {op.pin}"
    if op.value is not None:
        text += f" = {op.value}"
    return text


def _level(value) -> bool | None:
    """Accept true/1/false/0 as a wire level; anything else -> None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


class Dispatcher:
    """Validates ops against the pin profile, then executes them via gpiozero.

    Devices are constructed once and cached (dict bcm -> device); a device
    that fails to construct is cached as an error for _DEVICE_RETRY_SECONDS
    instead of being retried on every request. Construction and execution
    happen in whatever thread the HTTP server hands us; a coarse lock guards
    the cache so two concurrent requests never build a device twice.
    """

    def __init__(self, *, dev: bool = False) -> None:
        self.dev = dev
        self._devices: dict[int, object] = {}
        self._failed: dict[int, tuple[str, float]] = {}   # bcm -> (msg, retry_at)
        self._ctor_lock = threading.Lock()
        self._sensor_lock = threading.Lock()  # non-gpiozero sensor reads
        self._started = time.monotonic()
        # Set while a GUI-submitted script job owns the GPIO: every pin op
        # (except noop/all_off) is refused with a clear error.
        self.halted = False
        self.script: "ScriptRunner | None" = None

    # ------------------------------------------------------------- devices --

    def _device(self, profile: PinProfile):
        assert profile.bcm is not None, "cannot build a device without a BCM pin"
        bcm = profile.bcm
        dev = self._devices.get(bcm)
        if dev is not None:
            return dev
        failure = self._failed.get(bcm)
        if failure is not None and time.monotonic() < failure[1]:
            raise DeviceError(failure[0])
        with self._ctor_lock:
            dev = self._devices.get(bcm)
            if dev is not None:
                return dev
            failure = self._failed.get(bcm)
            if failure is not None and time.monotonic() < failure[1]:
                raise DeviceError(failure[0])
            try:
                dev = self._create(profile)
            except Exception as ex:
                # Cache the failure so a dead/broken device costs one clear
                # line per call instead of a construction attempt (and a
                # traceback) every 1.5 s. Retried after _DEVICE_RETRY_SECONDS.
                # Callers already know which pin/device they asked about, so
                # the message stays unprefixed.
                message = str(ex) if isinstance(ex, DeviceError) \
                    else f"{type(ex).__name__}: {ex}"
                self._failed[bcm] = (message,
                                     time.monotonic() + _DEVICE_RETRY_SECONDS)
                raise DeviceError(message) from None
            self._devices[bcm] = dev
            self._failed.pop(bcm, None)
        return dev

    def release_all(self) -> int:
        """Close every constructed device and clear caches.

        Called before a script job starts: gpiozero can't share a pin across
        processes, so the bridge must let go of the GPIO before the child
        process claims it. Devices are rebuilt lazily on the next op.
        """
        with self._ctor_lock:
            devices = list(self._devices.values())
            self._devices.clear()
            self._failed.clear()
        closed = 0
        for dev in devices:
            try:
                dev.close()
                closed += 1
            except Exception:  # noqa: BLE001 — best effort; never block the script
                pass
        return closed

    def _create(self, profile: PinProfile):
        drv, role, bcm = profile.driver, profile.role, profile.bcm
        if role is Role.OUTPUT:
            if drv == "Buzzer":
                return gpiozero.Buzzer(bcm)
            if drv == "TonalBuzzer":
                return gpiozero.TonalBuzzer(bcm)
            if drv == "RGBLED":
                if len(profile.aux) != 2:
                    raise RuntimeError(
                        f"{profile.name}: RGBLED needs aux=(green, blue) pins")
                return gpiozero.RGBLED(red=bcm, green=profile.aux[0],
                                       blue=profile.aux[1])
            return gpiozero.LED(bcm, active_high=not profile.active_low)
        if role is Role.INPUT:
            if drv == "MotionSensor":
                return gpiozero.MotionSensor(bcm)
            return gpiozero.Button(bcm, pull_up=profile.pull_up)
        if role is Role.PWM:
            if drv == "Motor":
                if not profile.aux:
                    raise RuntimeError(f"{profile.name}: Motor needs an aux (backward) pin")
                return gpiozero.Motor(forward=bcm, backward=profile.aux[0])
            return gpiozero.PWMLED(bcm)
        if role is Role.SENSOR:
            if drv == "DHT22":
                return _dht22(bcm)
            if drv == "HCSR04":
                # partial=True: never block on the sample queue filling up —
                # with no echo wired the default (partial=False) would hang
                # GET /state forever and leak a thread per poll.
                return gpiozero.DistanceSensor(trigger=bcm, echo=profile.echo,
                                               partial=True)
            if drv == "LDR":
                # gpiozero's LightSensor takes an analog pin; the MCP3008 on
                # SPI0 (CE0) provides it. Needs spidev + SPI enabled.
                return gpiozero.LightSensor(gpiozero.MCP3008(channel=0))
            if drv == "CPUTemperature":
                return gpiozero.CPUTemperature()
            if drv == "LoadAverage":
                # our stand-in: gpiozero 1.6.2's prints the file on every read
                return _LoadAverage(minutes=1)
        raise RuntimeError(f"cannot build a device for {profile.name} ({drv}/{role})")

    # ------------------------------------------------------------- sensors --

    def _synthetic(self, profile: PinProfile) -> float:
        fn = _SYNTHETIC.get(profile.driver)
        if fn is None:
            raise RuntimeError(f"no synthetic reading for driver {profile.driver!r}")
        return fn(time.monotonic() - self._started)

    def _read_sensor(self, profile: PinProfile) -> float:
        if profile.needs_adc:
            raise RuntimeError("needs an ADC (not fitted)")
        if self.dev:
            return self._synthetic(profile)
        dev = self._device(profile)
        if profile.driver == "DHT22":
            with self._sensor_lock:  # Adafruit's driver isn't multi-reader safe
                value = dev.temperature
            if value is None:
                raise RuntimeError("DHT22: no reading yet")
            return float(value)
        if profile.driver == "HCSR04":
            return float(dev.distance)
        if profile.driver == "LDR":
            # LightSensor.value: 0 (dark) .. 1 (bright) — report as % bright.
            return round(float(dev.value) * 100.0, 1)
        if profile.driver == "CPUTemperature":
            return round(float(dev.temperature), 1)
        if profile.driver == "LoadAverage":
            return float(dev.load_average)
        raise RuntimeError(f"unknown sensor driver {profile.driver!r}")

    # ------------------------------------------------------------ execution --

    def dispatch(self, op: Op) -> OpResult:
        """Entry point for POST /op: never raises, never leaks a traceback."""
        try:
            result = self._dispatch(op)
        except DeviceError as ex:
            # known device problem (card shows it too) — not a bug, no traceback
            result = OpResult(ok=False, req_id=op.req_id, error=str(ex))
        except Exception as ex:  # noqa: BLE001 — boundary: log it, report it
            traceback.print_exc()
            result = OpResult(ok=False, req_id=op.req_id,
                              error=f"internal error: {type(ex).__name__}: {ex}")
        _log(op.req_id, _describe(op), result)
        return result

    def _dispatch(self, op: Op) -> OpResult:
        req = op.req_id
        if op.op == "noop":
            return OpResult(ok=True, req_id=req)
        if op.op == "all_off":
            # A script job may own the GPIO right now — silence it first so
            # all_off always means "make everything quiet", including the
            # subprocess's pins.
            if self.script is not None:
                self.script.stop()
            return OpResult(ok=True, req_id=req, value=self.all_off())
        if self.halted:
            return OpResult(ok=False, req_id=req,
                            error="script is running — stop it first")

        profile = pins.PROFILE.get(op.pin)
        if profile is None:
            return OpResult(ok=False, req_id=req,
                            error=f"pin {op.pin} is not in the profile")
        if profile.needs_adc:
            return OpResult(ok=False, req_id=req,
                            error=f"{profile.name}: needs an ADC (not fitted)")
        if op.op not in pins.ops_for(profile.role):
            return OpResult(
                ok=False, req_id=req,
                error=f"'{op.op}' not allowed on {profile.name} ({profile.role.value})")

        if op.op == "set_input" and not self.dev:
            return OpResult(ok=False, req_id=req,
                            error="set_input is only available in --dev mode")

        if op.op == "digital_write":
            level = _level(op.value)
            if level is None:
                return OpResult(ok=False, req_id=req,
                                error="value must be true/false or 1/0")
            dev = self._device(profile)
            if profile.driver == "TonalBuzzer":
                # TonalBuzzer has no on/off — a "high" is a 440 Hz tone.
                dev.play(440.0) if level else dev.stop()
            else:
                dev.on() if level else dev.off()
            return OpResult(ok=True, req_id=req, value=bool(level))

        if op.op == "pwm_write":
            value = op.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return OpResult(ok=False, req_id=req,
                                error="value must be a number 0..100")
            if not 0 <= value <= 100:
                return OpResult(ok=False, req_id=req,
                                error="PWM value must be 0..100")
            dev = self._device(profile)
            dev.value = float(value) / 100.0
            return OpResult(ok=True, req_id=req, value=float(value))

        if op.op == "pulse":
            duration_ms = 500 if op.value is None else op.value
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
                return OpResult(ok=False, req_id=req,
                                error="value must be a duration in ms (1..10000)")
            if not 1 <= duration_ms <= 10000:
                return OpResult(ok=False, req_id=req,
                                error="pulse duration must be 1..10000 ms")
            dev = self._device(profile)
            if profile.driver == "TonalBuzzer":
                dev.play(440.0)
            else:
                dev.on()
            try:
                time.sleep(duration_ms / 1000.0)
            finally:
                # always leave the pin quiet, even on interrupt
                (dev.stop() if profile.driver == "TonalBuzzer" else dev.off())
            return OpResult(ok=True, req_id=req, value=float(duration_ms))

        if op.op == "motor_write":
            value = op.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return OpResult(ok=False, req_id=req,
                                error="value must be a number -100..100")
            if not -100 <= value <= 100:
                return OpResult(ok=False, req_id=req,
                                error="motor value must be -100..100")
            if profile.driver != "Motor":
                return OpResult(ok=False, req_id=req,
                                error=f"{profile.name} is not a motor")
            dev = self._device(profile)
            dev.value = float(value) / 100.0   # gpiozero: -1..1 signed speed
            return OpResult(ok=True, req_id=req, value=float(value))

        if op.op == "rgb_write":
            value = op.value
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or int(value) != value:
                return OpResult(ok=False, req_id=req,
                                error="value must be an int 0..16777215 (0xRRGGBB)")
            value = int(value)
            if not 0 <= value <= 0xFFFFFF:
                return OpResult(ok=False, req_id=req,
                                error="colour must be 0..16777215 (0xRRGGBB)")
            if profile.driver != "RGBLED":
                return OpResult(ok=False, req_id=req,
                                error=f"{profile.name} is not an RGB LED")
            dev = self._device(profile)
            dev.value = (
                ((value >> 16) & 0xFF) / 255.0,
                ((value >> 8) & 0xFF) / 255.0,
                (value & 0xFF) / 255.0,
            )
            return OpResult(ok=True, req_id=req, value=value)

        if op.op == "tone":
            value = op.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return OpResult(ok=False, req_id=req,
                                error="value must be a frequency in Hz (200..5000)")
            if not 200 <= value <= 5000:
                return OpResult(ok=False, req_id=req,
                                error="frequency must be 200..5000 Hz")
            if profile.driver != "TonalBuzzer":
                return OpResult(ok=False, req_id=req,
                                error=f"{profile.name} is not a buzzer")
            dev = self._device(profile)
            try:
                dev.play(float(value))
            except ValueError as ex:  # outside this buzzer's tone range
                return OpResult(ok=False, req_id=req, error=f"tone: {ex}")
            try:
                time.sleep(TONE_MS / 1000.0)
            finally:
                dev.stop()
            return OpResult(ok=True, req_id=req, value=float(value))

        if op.op == "digital_read":
            dev = self._device(profile)
            return OpResult(ok=True, req_id=req, value=bool(dev.value))

        if op.op == "read":
            return OpResult(ok=True, req_id=req, value=self._read_sensor(profile))

        if op.op == "set_input":
            level = _level(op.value)
            if level is None:
                return OpResult(ok=False, req_id=req,
                                error="value must be true/false or 1/0")
            dev = self._device(profile)
            # pull-up input (Button): pressed pulls the wire LOW;
            # active-high input (MotionSensor): pressed drives it HIGH.
            wire_high = (not level) if dev.pull_up else level
            (dev.pin.drive_high() if wire_high else dev.pin.drive_low())
            return OpResult(ok=True, req_id=req, value=bool(level))

        return OpResult(ok=False, req_id=req, error=f"unhandled op '{op.op}'")

    def all_off(self) -> int:
        """Safety net: de-energise every output/PWM device. Returns count."""
        off = 0
        for profile in pins.PROFILE.values():
            if profile.role not in (Role.OUTPUT, Role.PWM) or profile.needs_adc:
                continue
            try:
                dev = self._device(profile)
                if profile.role is Role.PWM:
                    dev.value = 0.0
                elif profile.driver == "TonalBuzzer":
                    dev.stop()   # no off() on TonalBuzzer
                else:
                    dev.off()
                off += 1
            except Exception as ex:  # noqa: BLE001 — one line per device, keep going
                print(f"all_off: {profile.name} failed: "
                      f"{type(ex).__name__}: {ex}",
                      file=sys.stderr, flush=True)
        return off

    # --------------------------------------------------------------- state --

    def state(self) -> list[PinState]:
        """One row per wired device — a single GET refreshes the whole GUI."""
        if self.halted:
            return [self._halted_row(p) for p in pins.PROFILE.values()]
        return [self._state_row(p) for p in pins.PROFILE.values()]

    @staticmethod
    def _halted_row(profile: PinProfile) -> PinState:
        return PinState(bcm=profile.bcm,
                        direction=_DIRECTION.get(profile.role, "out"),
                        unit=profile.unit,
                        fault="script running (GPIO released)")

    def _state_row(self, profile: PinProfile) -> PinState:
        direction = _DIRECTION.get(profile.role, "out")
        if profile.needs_adc:
            return PinState(bcm=profile.bcm, direction=direction, unit=profile.unit,
                            fault="needs an ADC (not fitted)")
        try:
            if profile.role is Role.SENSOR:
                return PinState(bcm=profile.bcm, direction=direction,
                                value=self._read_sensor(profile), unit=profile.unit)
            dev = self._device(profile)
            if profile.role is Role.PWM:
                # PWMLED: 0..100; Motor: signed -100..100 (value is -1..1).
                value: float | int | bool = round(dev.value * 100.0, 1)
                return PinState(bcm=profile.bcm, direction=direction, value=value,
                                unit="%")
            if profile.driver == "TonalBuzzer":
                # value is None when silent, else a float that is 0.0 at the
                # mid tone — so test for None, not truthiness.
                return PinState(bcm=profile.bcm, direction=direction,
                                value=dev.value is not None)
            if profile.driver == "RGBLED":
                # packed 0xRRGGBB: truthy for any non-black colour, 0 = off.
                r, g, b = (round(c * 255) for c in dev.value)
                return PinState(bcm=profile.bcm, direction=direction,
                                value=(r << 16) | (g << 8) | b)
            return PinState(bcm=profile.bcm, direction=direction,
                            value=bool(dev.value))
        except Exception as ex:  # noqa: BLE001 — surface on the card, keep serving
            return PinState(bcm=profile.bcm, direction=direction, unit=profile.unit,
                            fault=f"{type(ex).__name__}: {ex}")


class ScriptRunner:
    """Runs GUI-submitted scripts as subprocess jobs (opt-in via --allow-code).

    A script is a whole Python program typed into the GUI and run as a child
    process (`python -c code`) on the Pi. Safety model for a home/school lab:

    - Off by default: pi.py must be started with --allow-code.
    - One job at a time; starting one releases every GPIO device first
      (gpiozero can't share pins across processes) and halts pin ops.
    - Stop = SIGTERM (then SIGKILL after 2 s) — the GUI's panic button.
    - stdout+stderr are merged into a bounded tail the GUI can display.
    """

    OUTPUT_TAIL_LINES = 200   # keep the last N lines for GET /state

    def __init__(self, dispatcher: Dispatcher, *, enabled: bool = False,
                 echo_code: bool = False) -> None:
        self.dispatcher = dispatcher
        self.enabled = enabled
        self.echo_code = echo_code  # --dev + --allow-code: print the source
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._running = False      # cleared only by the reader's finalization
        self._job = 0
        self._exit: int | None = None
        self._lines: deque[str] = deque(maxlen=self.OUTPUT_TAIL_LINES)

    def start(self, code: str, req_id: int = 0) -> OpResult:
        if not self.enabled:
            return OpResult(ok=False, req_id=req_id,
                            error="code execution disabled — start pi.py with --allow-code")
        with self._lock:
            if self._running:
                return OpResult(ok=False, req_id=req_id,
                                error=f"job {self._job} is still running — stop it first")
            released = self.dispatcher.release_all()
            self.dispatcher.halted = True
            self._job += 1
            job = self._job
            self._exit = None
            self._lines.clear()
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, "-c", code],
                    stdin=subprocess.DEVNULL,      # never block on input()
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,      # merge: one output stream
                    text=True, encoding="utf-8", errors="replace",
                    env=os.environ.copy(),         # inherits mock-pin vars in --dev
                )
            except OSError as ex:
                self._proc = None
                self._running = False
                self.dispatcher.halted = False
                return OpResult(ok=False, req_id=req_id,
                                error=f"cannot start script: {ex}")
            self._running = True
            proc = self._proc
            threading.Thread(target=self._read_output,
                             args=(proc, job), daemon=True).start()
        print(f"script: job {job} started (released {released} device(s))",
              flush=True)
        if self.echo_code:
            print(f"script: job {job} source:", flush=True)
            print(code, end="" if code.endswith("\n") else "\n", flush=True)
            print(f"script: job {job} source end", flush=True)
        return OpResult(ok=True, req_id=req_id, value=job)

    def _read_output(self, proc: subprocess.Popen, job: int) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    with self._lock:
                        self._lines.append(line.rstrip("\n"))
            proc.wait()
        finally:
            with self._lock:
                # Finalize atomically: once status() reports "not running",
                # halted must already be False (no observer race).
                if self._proc is proc:
                    self._exit = proc.returncode
                    self._running = False
                    self.dispatcher.halted = False
            print(f"script: job {job} exited with code {proc.returncode}",
                  flush=True)

    def stop(self) -> OpResult:
        """Terminate the running job (SIGTERM, then SIGKILL after 2 s)."""
        with self._lock:
            proc = self._proc
            running = self._running
        if proc is None or not running:
            return OpResult(ok=False, error="no script is running")
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return OpResult(ok=True)

    def status(self) -> protocol.ScriptStatus:
        with self._lock:
            return protocol.ScriptStatus(
                enabled=self.enabled,
                running=self._running,
                job=self._job,
                exit=self._exit,
                output="\n".join(self._lines),
            )


class _Handler(BaseHTTPRequestHandler):
    """Three endpoints: POST /op, POST /script(/stop), GET /state. JSON only."""

    dispatcher: Dispatcher | None = None  # bound in main()
    runner: ScriptRunner | None = None    # bound in main()
    server_version = "picontrol"
    sys_version = ""  # don't advertise the Python version

    def _reply(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def do_OPTIONS(self) -> None:  # CORS preflight (web-build clients)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(min(max(length, 0), limit))

    def do_POST(self) -> None:
        path = self._path()
        if path == "/op":
            self._handle_op()
        elif path == "/script":
            self._handle_script()
        elif path == "/script/stop":
            self._handle_script_stop()
        else:
            self._reply(404, protocol.encode_result(OpResult(
                ok=False,
                error="unknown endpoint (use POST /op, POST /script, "
                      "POST /script/stop)")))

    def _handle_op(self) -> None:
        body = self._read_body(protocol.MAX_BODY + 1)
        try:
            op = protocol.parse_op(body)
        except ProtocolError as ex:
            result = OpResult(ok=False, error=str(ex))
            _log(0, f"malformed: {body[:60]!r}", result)
            self._reply(400, protocol.encode_result(result))
            return
        result = self.dispatcher.dispatch(op)
        self._reply(200 if result.ok else 400, protocol.encode_result(result))

    def _handle_script(self) -> None:
        body = self._read_body(protocol.MAX_SCRIPT_BODY + 256)
        try:
            code, req_id = protocol.parse_script(body)
        except ProtocolError as ex:
            result = OpResult(ok=False, error=str(ex))
            _log(0, f"malformed script: {len(body)} bytes", result)
            self._reply(400, protocol.encode_result(result))
            return
        if self.runner is None:
            self._reply(400, protocol.encode_result(
                OpResult(ok=False, req_id=req_id,
                         error="code execution disabled — start pi.py with --allow-code")))
            return
        result = self.runner.start(code, req_id)
        self._reply(200 if result.ok else 400, protocol.encode_result(result))

    def _handle_script_stop(self) -> None:
        if self.runner is None:
            self._reply(400, protocol.encode_result(
                OpResult(ok=False,
                         error="code execution disabled — start pi.py with --allow-code")))
            return
        result = self.runner.stop()
        self._reply(200 if result.ok else 400, protocol.encode_result(result))

    def do_GET(self) -> None:
        if self._path() != "/state":
            self._reply(404, protocol.encode_result(
                OpResult(ok=False, error="unknown endpoint (use GET /state)")))
            return
        try:
            states = self.dispatcher.state()
        except Exception as ex:  # noqa: BLE001 — last-resort boundary
            traceback.print_exc()
            self._reply(500, protocol.encode_result(
                OpResult(ok=False, error=f"state failed: {type(ex).__name__}")))
            return
        script = self.runner.status() if self.runner is not None else None
        self._reply(200, protocol.encode_state(states, script=script))

    def log_message(self, fmt: str, *args) -> None:
        pass  # transaction log above replaces the default stderr access log


def _resolve_pin_factory(dev: bool = False) -> type:
    """Make gpiozero pick its pin factory now; return the factory class.

    gpiozero >= 2.0 exposes Device.ensure_pin_factory(); 1.6.x resolves the
    factory lazily inside Device.__init__ (via Device._default_pin_factory())
    and raises BadPinFactory when nothing loads. Cache the result on Device
    the same way Device.__init__ would, so this file runs unchanged on both.

    Dev mode builds MockFactory with the pin CLASS rather than the
    GPIOZERO_MOCK_PIN_CLASS name: 1.6.x resolves name strings through
    pkg_resources, which a slim venv need not have.
    """
    if dev:
        from gpiozero.pins.mock import MockFactory, MockPWMPin
        gpiozero.Device.pin_factory = MockFactory(pin_class=MockPWMPin)
        return MockFactory
    ensure = getattr(gpiozero.Device, "ensure_pin_factory", None)
    if ensure is not None:
        ensure()
        return type(gpiozero.Device.pin_factory)
    factory = gpiozero.Device._default_pin_factory()
    gpiozero.Device.pin_factory = factory
    return type(factory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi.py", description="picontrol controller bridge for Raspberry Pi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="endpoints: POST /op (one op), GET /state (all states + script\n"
               "status), POST /script and POST /script/stop (with --allow-code).\n"
               "\n"
               "examples:\n"
               "  python3 pi.py                        # live GPIO on a Pi\n"
               "  python3 pi.py --port 8643            # different port\n"
               "  python3 pi.py --dev                  # PC: no hardware\n"
               "  python3 pi.py --dev --allow-code     # PC: also let the GUI run\n"
               "                                       # scripts (source echoed\n"
               "                                       # to the console)\n"
               "  python3 pi.py --allow-code           # Pi: scripts allowed,\n"
               "                                       # no source echo\n")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"TCP port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("--dev", action="store_true",
                        help="mock pins + synthetic sensor values (no Pi hardware)")
    parser.add_argument("--allow-code", action="store_true",
                        help="allow the GUI to run Python scripts on this machine "
                             "(POST /script; off by default; with --dev the "
                             "script source is echoed to the console)")
    args = parser.parse_args(argv)

    if args.dev:
        # Must be set before the first device construction (the pin factory
        # reads these lazily on first use).
        os.environ["GPIOZERO_PIN_FACTORY"] = "mock"
        os.environ["GPIOZERO_MOCK_PIN_CLASS"] = "mockpwmpin"

    # Resolve the pin factory NOW so a machine without GPIO (e.g. a PC
    # started without --dev) fails fast with a clear message instead of
    # serving and exploding on the first request / at shutdown.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # hide gpiozero's fallback spam
            factory = _resolve_pin_factory(dev=args.dev)
    except Exception as ex:
        print(f"pi.py: no usable GPIO pin factory "
              f"({type(ex).__name__}: {ex})",
              file=sys.stderr, flush=True)
        if "pkg_resources" in str(ex):
            # gpiozero 1.6.x's pins.mock imports pkg_resources at module
            # level — every mock-pin route dies without it, and setuptools
            # removed pkg_resources in 81.
            print('pi.py:   gpiozero 1.6.x needs pkg_resources: '
                  'pip install "setuptools<81"',
                  file=sys.stderr, flush=True)
        print("pi.py:   on a Raspberry Pi: install lgpio "
              "(sudo apt install python3-lgpio) or RPi.GPIO",
              file=sys.stderr, flush=True)
        print("pi.py:   on a PC without hardware: run with --dev "
              "(mock pins + synthetic sensors)",
              file=sys.stderr, flush=True)
        return 1

    # gpiozero 2.x falls back to MockFactory SILENTLY when it can't load a
    # real factory — live mode would then serve happily while nothing
    # electrical ever happens (1.6.x raises instead, caught above). Refuse
    # rather than pretend.
    if (not args.dev
            and factory.__name__ == "MockFactory"):
        print("pi.py: no real GPIO pin factory — live mode would only drive "
              "MOCK pins (nothing electrical would happen)",
              file=sys.stderr, flush=True)
        print("pi.py:   on a Raspberry Pi: install lgpio "
              "(sudo apt install python3-lgpio), then re-run",
              file=sys.stderr, flush=True)
        print("pi.py:   on a PC without hardware: run with --dev "
              "(mock pins + synthetic sensors)",
              file=sys.stderr, flush=True)
        return 1

    dispatcher = Dispatcher(dev=args.dev)
    runner = ScriptRunner(dispatcher, enabled=args.allow_code,
                          echo_code=args.dev and args.allow_code)
    dispatcher.script = runner
    handler = type("BoundHandler", (_Handler,),
                   {"dispatcher": dispatcher, "runner": runner})
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as ex:
        print(f"pi.py: cannot bind {args.host}:{args.port}: {ex}",
              file=sys.stderr, flush=True)
        return 1

    if args.dev:
        mode = "dev: mock pins + synthetic sensors"
    else:
        mode = f"live GPIO via {factory.__name__}"
    if args.allow_code:
        mode += ", script execution ALLOWED (--allow-code)"
    else:
        mode += ", script execution disabled"
    print(f"picontrol bridge listening on {args.host}:{args.port} [{mode}]",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\npi.py: shutting down", flush=True)
    finally:
        turned_off = dispatcher.all_off()  # leave the hardware quiet
        if turned_off:
            print(f"pi.py: switched off {turned_off} output(s)", flush=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
