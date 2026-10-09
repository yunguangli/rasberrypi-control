"""Dispatcher tests for src/pi.py — mock pins, no network, no Pi.

Plus one HTTP-level test that boots the real ThreadingHTTPServer on an
ephemeral port and drives it with urllib, exercising the full Pi side.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import gpiozero
import pytest

import pins
import pi
import protocol
from protocol import Op, parse_result, parse_state


@pytest.fixture()
def dsp() -> pi.Dispatcher:
    """A fresh dispatcher that releases its pins on teardown.

    The mock pin factory is process-global: without an explicit close(),
    an active device (e.g. LED 17) survives via reference cycles and can
    still own its pin when a later test builds one — GPIOPinInUse.
    """
    d = pi.Dispatcher(dev=True)
    yield d
    d.release_all()


def wait_for(predicate, timeout: float = 2.0) -> bool:
    """Poll until predicate holds — for MotionSensor's smoothing queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def state_by_bcm(dsp) -> dict:
    return {s.bcm: s for s in dsp.state()}


# --------------------------------------------------------------- outputs ----

def test_digital_write_then_read(dsp):
    res = dsp.dispatch(Op("digital_write", pin=17, value=1, req_id=1))
    assert res.ok and res.value is True
    assert state_by_bcm(dsp)[17].value is True

    res = dsp.dispatch(Op("digital_read", pin=17, req_id=2))
    assert res.ok and res.value is True

    dsp.dispatch(Op("digital_write", pin=17, value=0))
    assert state_by_bcm(dsp)[17].value is False


def test_digital_write_rejects_bad_level(dsp):
    res = dsp.dispatch(Op("digital_write", pin=17, value=42))
    assert not res.ok and "1/0" in res.error


def test_active_low_relay_tracks_logically(dsp):
    """Relay on BCM 22 is active_low: logical on == value True in state."""
    res = dsp.dispatch(Op("digital_write", pin=22, value=1))
    assert res.ok
    assert state_by_bcm(dsp)[22].value is True


def test_pulse_returns_and_leaves_pin_off(dsp):
    res = dsp.dispatch(Op("pulse", pin=23, value=30, req_id=5))
    assert res.ok and res.value == 30
    assert state_by_bcm(dsp)[23].value is False


def test_pulse_rejects_absurd_duration(dsp):
    res = dsp.dispatch(Op("pulse", pin=23, value=99999))
    assert not res.ok and "1..10000" in res.error


def test_tonal_buzzer_tone_plays_then_stops(dsp):
    res = dsp.dispatch(Op("tone", pin=23, value=440, req_id=9))
    assert res.ok and res.value == 440.0
    assert state_by_bcm(dsp)[23].value is False  # silence after the note


def test_tone_rejects_bad_values(dsp):
    assert not dsp.dispatch(Op("tone", pin=23, value=50)).ok        # too low
    assert not dsp.dispatch(Op("tone", pin=23, value=99999)).ok     # too high
    assert not dsp.dispatch(Op("tone", pin=23, value="440")).ok     # not a number
    res = dsp.dispatch(Op("tone", pin=17, value=440))               # not a buzzer
    assert not res.ok and "not a buzzer" in res.error


def test_tonal_buzzer_reports_silent_not_zero(dsp):
    """value is 0.0 at the mid tone — the row must use `is not None`."""
    assert dsp.dispatch(Op("tone", pin=23, value=440)).ok
    dev = dsp._device(pins.PROFILE[23])
    dev.play(440.0)
    assert dev.value == 0.0                      # falsy, but buzzing!
    dsp.dispatch(Op("digital_write", pin=23, value=0))
    assert state_by_bcm(dsp)[23].value is False  # truly silent
    dev.stop()


# ---------------------------------------------------------------- inputs ----

def test_button_initially_released(dsp):
    res = dsp.dispatch(Op("digital_read", pin=24))
    assert res.ok and res.value is False


def test_set_input_press_and_release(dsp):
    assert dsp.dispatch(Op("set_input", pin=24, value=1)).ok
    assert wait_for(lambda: state_by_bcm(dsp)[24].value is True)

    assert dsp.dispatch(Op("set_input", pin=24, value=0)).ok
    assert wait_for(lambda: state_by_bcm(dsp)[24].value is False)


def test_set_input_pir_active_high(dsp):
    """MotionSensor has pull_up=False: pressed = wire HIGH."""
    assert dsp.dispatch(Op("set_input", pin=25, value=1)).ok
    assert wait_for(lambda: state_by_bcm(dsp)[25].value is True)

    assert dsp.dispatch(Op("set_input", pin=25, value=0)).ok
    assert wait_for(lambda: state_by_bcm(dsp)[25].value is False)


def test_set_input_rejected_outside_dev_mode():
    real = pi.Dispatcher(dev=False)  # no device is ever constructed here
    res = real.dispatch(Op("set_input", pin=24, value=1))
    assert not res.ok and "--dev" in res.error


# ------------------------------------------------------------------ pwm -----

def test_pwm_write_and_state(dsp):
    res = dsp.dispatch(Op("pwm_write", pin=18, value=60, req_id=3))
    assert res.ok and res.value == 60.0
    assert state_by_bcm(dsp)[18].value == 60.0
    assert state_by_bcm(dsp)[18].unit == "%"


def test_pwm_rejects_out_of_range(dsp):
    res = dsp.dispatch(Op("pwm_write", pin=18, value=150))
    assert not res.ok and "0..100" in res.error
    res = dsp.dispatch(Op("pwm_write", pin=18, value=-5))
    assert not res.ok


def test_pwm_rejected_on_output_pin(dsp):
    res = dsp.dispatch(Op("pwm_write", pin=17, value=60))
    assert not res.ok and "not allowed" in res.error


# ----------------------------------------------------------------- motor ----

def test_motor_write_signed_speed(dsp):
    res = dsp.dispatch(Op("motor_write", pin=13, value=60, req_id=4))
    assert res.ok and res.value == 60.0
    assert state_by_bcm(dsp)[13].value == 60.0

    res = dsp.dispatch(Op("motor_write", pin=13, value=-40))
    assert res.ok and res.value == -40.0
    assert state_by_bcm(dsp)[13].value == -40.0

    assert dsp.dispatch(Op("motor_write", pin=13, value=0)).ok
    assert state_by_bcm(dsp)[13].value == 0.0


def test_motor_write_rejects_bad_values(dsp):
    assert "motor value" in dsp.dispatch(Op("motor_write", pin=13, value=150)).error
    assert not dsp.dispatch(Op("motor_write", pin=13, value=-150)).ok
    assert not dsp.dispatch(Op("motor_write", pin=13, value=True)).ok
    res = dsp.dispatch(Op("motor_write", pin=18, value=60))  # dimmer, not a motor
    assert not res.ok and "not a motor" in res.error


# -------------------------------------------------------------- rgb led -----

def test_rgb_write_packed_colour(dsp):
    res = dsp.dispatch(Op("rgb_write", pin=19, value=0xFF8800, req_id=6))
    assert res.ok and res.value == 0xFF8800
    assert state_by_bcm(dsp)[19].value == 0xFF8800

    assert dsp.dispatch(Op("rgb_write", pin=19, value=0)).ok
    assert state_by_bcm(dsp)[19].value == 0  # off


def test_rgb_write_rejects_bad_values(dsp):
    assert not dsp.dispatch(Op("rgb_write", pin=19, value=0x1000000)).ok
    assert not dsp.dispatch(Op("rgb_write", pin=19, value=-1)).ok
    assert not dsp.dispatch(Op("rgb_write", pin=19, value=True)).ok
    res = dsp.dispatch(Op("rgb_write", pin=17, value=0xFF0000))  # plain LED
    assert not res.ok and "not an RGB LED" in res.error


# --------------------------------------------------------------- sensors ----

def test_temperature_read_is_synthetic(dsp):
    res = dsp.dispatch(Op("read", pin=4, req_id=7))
    assert res.ok
    assert isinstance(res.value, float)
    assert 18.0 <= res.value <= 24.0  # sine walk around 21 °C


def test_distance_read_is_synthetic(dsp):
    res = dsp.dispatch(Op("read", pin=5))
    assert res.ok and isinstance(res.value, float)
    assert 0.1 <= res.value <= 1.1


def test_light_sensor_is_synthetic(dsp):
    """MCP3008-backed LDR: in --dev mode it serves a plausible % reading."""
    res = dsp.dispatch(Op("read", pin=-1))
    assert res.ok and isinstance(res.value, float)
    assert 0.0 <= res.value <= 100.0


def test_system_sensors_are_synthetic(dsp):
    """CPU temp / system load have no GPIO — synthetic values in --dev."""
    cpu = dsp.dispatch(Op("read", pin=-2))
    assert cpu.ok and 30.0 <= cpu.value <= 70.0
    load = dsp.dispatch(Op("read", pin=-3))
    assert load.ok and 0.0 <= load.value <= 2.0


def test_state_rows_for_system_sensors(dsp):
    rows = state_by_bcm(dsp)
    assert rows[-2].unit == "°C" and rows[-2].value is not None
    assert rows[-1].value is not None  # load has no unit
    # no GPIO: they never appear on the header
    assert all(c.bcm not in (-1, -2, -3) for c in pins.HEADER)


def test_dht22_without_a_driver_is_a_clear_fault(capsys):
    """gpiozero has no DHT22 class (issue #254): a read must come back as
    advice the card can show, never an AttributeError + traceback."""
    real = pi.Dispatcher(dev=False)  # dev mode serves synthetic values instead
    res = real.dispatch(Op("read", pin=4))
    assert not res.ok
    assert "DHT22" in res.error and "adafruit-circuitpython-dht" in res.error

    # second read: the failure is cached, and still never a traceback
    assert not real.dispatch(Op("read", pin=4)).ok
    row = {s.bcm: s for s in real.state()}[4]
    assert row.value is None and "DHT22" in row.fault
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err

    # release the mock pins this dispatcher claimed (gpiozero reserves the
    # HC-SR04's pins until the device is closed — the next test needs them)
    for device in real._devices.values():
        device.close()


def test_distance_read_never_blocks(dsp):
    """partial=True on DistanceSensor: an unwired HC-SR04 (echo never fires)
    must not hang GET /state — gpiozero's default waits for a full sample
    queue that would never fill."""
    dev = dsp._device(pins.PROFILE[5])
    box: dict = {}

    def read():
        box["value"] = dev.distance

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(2.0)
    assert not thread.is_alive(), "DistanceSensor.distance blocked — partial=True missing?"
    assert isinstance(box.get("value"), float)
    dev.close()  # release the mock pins and stop the sample thread


# -------------------------------------------------- validation boundaries ----

def test_unknown_pin_rejected(dsp):
    res = dsp.dispatch(Op("digital_write", pin=99, value=1))
    assert not res.ok and "not in the profile" in res.error


def test_write_rejected_on_input(dsp):
    res = dsp.dispatch(Op("digital_write", pin=24, value=1))
    assert not res.ok and "not allowed" in res.error


def test_read_rejected_on_output(dsp):
    res = dsp.dispatch(Op("read", pin=17))
    assert not res.ok and "not allowed" in res.error


def test_noop_and_all_off(dsp):
    assert dsp.dispatch(Op("noop")).ok
    dsp.dispatch(Op("digital_write", pin=17, value=1))
    dsp.dispatch(Op("pwm_write", pin=18, value=50))
    res = dsp.dispatch(Op("all_off"))
    # 5 outputs (LED, 2 relays, RGB LED, buzzer) + 2 PWM (dimmer, motor)
    assert res.ok and res.value == 7
    states = state_by_bcm(dsp)
    assert states[17].value is False
    assert states[18].value == 0.0
    assert states[13].value == 0.0  # motor stopped


def test_state_includes_all_wired_rows(dsp):
    """Every profile entry shows up in GET /state — new devices included."""
    rows = state_by_bcm(dsp)
    for bcm in (17, 22, 27, 19, 23, 24, 25, 4, 5, -1, -2, -3, 18, 13):
        assert bcm in rows, f"missing state row for BCM {bcm}"


def test_dispatch_never_raises(dsp):
    """Even a nonsense op comes back as a structured result."""
    for op in (Op("digital_read", pin=-1), Op("read", pin=24), Op("noop")):
        res = dsp.dispatch(op)
        assert isinstance(res, protocol.OpResult)


# ------------------------------------------------------- HTTP integration ----

@pytest.fixture()
def http_bridge():
    """The real pi.py server on an ephemeral port, driven end-to-end."""
    dispatcher = pi.Dispatcher(dev=True)
    handler = type("H", (pi._Handler,), {"dispatcher": dispatcher})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, dispatcher
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    dispatcher.release_all()


def _post(base: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(base + "/op", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def test_http_op_roundtrip(http_bridge):
    base, _ = http_bridge
    status, body = _post(base, protocol.encode_op(
        Op("digital_write", pin=17, value=1, req_id=42)))
    assert status == 200
    result = parse_result(body)
    assert result.ok and result.req_id == 42


def test_http_bad_op_is_400(http_bridge):
    base, _ = http_bridge
    status, body = _post(base, b'{"op": "eval"}')
    assert status == 400
    assert not parse_result(body).ok


def test_http_garbage_is_400(http_bridge):
    base, _ = http_bridge
    status, body = _post(base, b"not json at all")
    assert status == 400
    assert not parse_result(body).ok


def test_http_unknown_endpoint_is_404(http_bridge):
    base, _ = http_bridge
    status, _ = _post(base, b'{"op":"noop"}')  # hits /op, fine
    assert status == 200
    req = urllib.request.Request(base + "/nope", data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 404
    except urllib.error.HTTPError as ex:
        assert ex.code == 404


def test_http_state_and_cors(http_bridge):
    base, _ = http_bridge
    with urllib.request.urlopen(base + "/state", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        body = resp.read()
        states = parse_state(body)
    by_bcm = {s.bcm: s for s in states}
    assert by_bcm[17].direction == "out"
    assert by_bcm[-1].value is not None  # light sensor serves a synthetic value
    # the script block is present (disabled: this fixture didn't pass --allow-code)
    assert protocol.parse_script_status(body).enabled is False


def test_http_cors_preflight(http_bridge):
    base, _ = http_bridge
    req = urllib.request.Request(base + "/op", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")


# ---------------------------------------------------------- script runner ----

def _finish(runner, timeout=10.0):
    """Wait for the current job to end; return its status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status()
        if status.job and not status.running:
            return status
        time.sleep(0.02)
    return runner.status()


def test_script_disabled_by_default(dsp):
    r = pi.ScriptRunner(dsp, enabled=False)
    res = r.start("print('nope')")
    assert not res.ok and "--allow-code" in res.error


def test_script_runs_and_captures_output(dsp):
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    res = r.start("print('hello from the script')", req_id=7)
    assert res.ok and res.value == 1  # job number
    status = _finish(r)
    assert status.exit == 0
    assert "hello from the script" in status.output
    assert dsp.halted is False  # GPIO released again after the job


def test_script_failure_reports_exit_code(dsp):
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    assert r.start("import sys; sys.exit(3)").ok
    assert _finish(r).exit == 3


def test_script_source_echoed_in_dev_mode(dsp, capsys):
    """--dev + --allow-code: print the received source to the console."""
    r = pi.ScriptRunner(dsp, enabled=True, echo_code=True)
    dsp.script = r
    code = "import time\nfor i in range(2):\n    print(i)\n"
    assert r.start(code, req_id=4).ok
    out = capsys.readouterr().out
    assert "script: job 1 source:" in out
    assert "for i in range(2):" in out   # indentation survives to the console
    assert out.index("job 1 source:") < out.index("job 1 source end")
    _finish(r)


def test_script_source_not_echoed_without_dev_flag(dsp, capsys):
    """Live mode (--allow-code only): console keeps the two status lines only."""
    r = pi.ScriptRunner(dsp, enabled=True, echo_code=False)
    dsp.script = r
    assert r.start("print('quiet')").ok
    _finish(r)
    out = capsys.readouterr().out
    assert "job 1 started" in out
    assert "source" not in out


def test_script_halts_pin_ops_while_running(dsp):
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    assert r.start("import time; time.sleep(30)").ok
    assert wait_for(lambda: r.status().running)

    res = dsp.dispatch(Op("digital_write", pin=17, value=1))
    assert not res.ok and "stop it first" in res.error
    # every state row reports the release
    assert all("script running" in s.fault for s in dsp.state())
    # noop and all_off still work (all_off stops the script)
    assert dsp.dispatch(Op("noop")).ok

    assert r.stop().ok
    status = _finish(r)
    assert status.running is False
    assert dsp.halted is False
    assert dsp.dispatch(Op("digital_write", pin=17, value=1)).ok


def test_stop_without_running_script_is_structured(dsp):
    r = pi.ScriptRunner(dsp, enabled=False)
    res = r.stop()
    assert not res.ok and "no script" in res.error


def test_second_job_refused_while_running(dsp):
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    assert r.start("import time; time.sleep(30)").ok
    assert wait_for(lambda: r.status().running)
    res = r.start("print('too late')")
    assert not res.ok and "still running" in res.error
    r.stop()
    _finish(r)


def test_start_releases_cached_devices(dsp):
    """gpiozero can't share pins across processes — the bridge lets go first."""
    dsp.dispatch(Op("digital_write", pin=17, value=1))
    assert 17 in dsp._devices
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    assert r.start("print('hi')").ok
    assert 17 not in dsp._devices
    _finish(r)


def test_all_off_stops_running_script(dsp):
    r = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = r
    assert r.start("import time; time.sleep(30)").ok
    assert wait_for(lambda: r.status().running)
    res = dsp.dispatch(Op("all_off"))
    assert res.ok
    assert _finish(r).running is False


# -------------------------------------------------- script HTTP endpoints ----

def _post_path(base: str, path: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def test_http_script_disabled_is_400(http_bridge):
    """The fixture runs without --allow-code: /script must refuse clearly."""
    base, _ = http_bridge
    status, body = _post_path(base, "/script", protocol.encode_script("print(1)"))
    assert status == 400
    assert "--allow-code" in parse_result(body).error


def test_http_script_roundtrip_with_runner(dsp):
    """Full path with an enabled runner: submit, poll /state, see output."""
    runner = pi.ScriptRunner(dsp, enabled=True)
    dsp.script = runner
    handler = type("H", (pi._Handler,),
                   {"dispatcher": dsp, "runner": runner})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _post_path(
            base, "/script", protocol.encode_script("print('via http')", 5))
        assert status == 200
        result = parse_result(body)
        assert result.ok and result.req_id == 5

        deadline = time.monotonic() + 10
        info = runner.status()
        while time.monotonic() < deadline and (not info.job or info.running):
            time.sleep(0.02)
            info = runner.status()
        assert info.exit == 0 and "via http" in info.output

        with urllib.request.urlopen(base + "/state", timeout=5) as resp:
            state = protocol.parse_script_status(resp.read())
        assert state.job == 1 and state.exit == 0

        status, body = _post_path(base, "/script/stop", b"{}")
        assert status == 400  # nothing running — structured refusal
        assert not parse_result(body).ok
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_script_garbage_is_400(http_bridge):
    base, _ = http_bridge
    status, body = _post_path(base, "/script", b'{"script": ""}')
    assert status == 400
    assert not parse_result(body).ok


# ------------------------------------------------- startup failure modes ----

def test_help_prints_all_flags_and_exits_zero(capsys):
    """`pi.py --help` lists every flag without touching the pin factory."""
    with pytest.raises(SystemExit) as exc:
        pi.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--host", "--port", "--dev", "--allow-code"):
        assert flag in out


def test_live_mode_fails_fast_without_pin_factory(monkeypatch, capsys):
    """A PC started without --dev must exit with a hint, not serve and
    explode later on the first device construction."""

    def _boom():
        raise RuntimeError("Unable to load any default pin factory!")

    monkeypatch.setattr(gpiozero.Device, "ensure_pin_factory",
                        staticmethod(_boom))
    assert pi.main(["--port", "0"]) == 1
    err = capsys.readouterr().err
    assert "no usable GPIO pin factory" in err
    assert "--dev" in err
    assert "Raspberry Pi" in err


def test_all_off_reports_one_line_per_failure(dsp, monkeypatch, capsys):
    """all_off must never spam tracebacks — one line per failed device."""

    def _boom(profile):
        raise RuntimeError("no hardware")

    monkeypatch.setattr(dsp, "_create", _boom)
    result = dsp.dispatch(Op("all_off"))
    assert result.ok and result.value == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "all_off: Status LED failed: DeviceError: RuntimeError: no hardware" in err


def test_failed_construction_is_structured_and_cached(dsp, monkeypatch, capsys):
    """A device that won't construct is a DeviceError: structured on the wire,
    no traceback, and retried only after _DEVICE_RETRY_SECONDS."""
    attempts: list[int] = []

    def _boom(profile):
        attempts.append(profile.bcm)
        raise RuntimeError("no hardware")

    monkeypatch.setattr(dsp, "_create", _boom)

    res = dsp.dispatch(Op("digital_read", pin=17))
    assert not res.ok and res.error == "RuntimeError: no hardware"
    res = dsp.dispatch(Op("digital_read", pin=17))
    assert not res.ok and res.error == "RuntimeError: no hardware"

    assert attempts == [17], "the failure must be cached, not retried"
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "internal error" not in captured.out
