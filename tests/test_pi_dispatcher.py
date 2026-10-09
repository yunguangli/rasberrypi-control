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
    return pi.Dispatcher(dev=True)


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


def test_light_sensor_reports_missing_adc(dsp):
    res = dsp.dispatch(Op("read", pin=-1))
    assert not res.ok and "ADC" in res.error


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
    assert res.ok and res.value == 6  # 4 outputs (LED, 2 relays, buzzer) + 2 PWM
    states = state_by_bcm(dsp)
    assert states[17].value is False
    assert states[18].value == 0.0


def test_state_includes_light_fault_row(dsp):
    light = state_by_bcm(dsp)[-1]
    assert "ADC" in light.fault
    assert light.value is None


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
        states = parse_state(resp.read())
    by_bcm = {s.bcm: s for s in states}
    assert by_bcm[17].direction == "out"
    assert by_bcm[-1].fault  # light sensor always carries its ADC fault


def test_http_cors_preflight(http_bridge):
    base, _ = http_bridge
    req = urllib.request.Request(base + "/op", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")


# ------------------------------------------------- startup failure modes ----

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
