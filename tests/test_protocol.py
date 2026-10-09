"""Unit tests for src/protocol.py — encode/decode of the wire contract."""

from __future__ import annotations

import pytest

import pins
import protocol
from protocol import (
    KNOWN_OPS,
    MAX_ABS_VALUE,
    MAX_BODY,
    MAX_SCRIPT_BODY,
    Op,
    OpResult,
    PinState,
    ProtocolError,
    ScriptStatus,
    encode_op,
    encode_result,
    encode_script,
    encode_state,
    parse_op,
    parse_result,
    parse_script,
    parse_script_status,
    parse_state,
)


# ----------------------------------------------------------------- encode ---

def test_encode_op_roundtrip():
    op = Op(op="digital_write", pin=17, value=1, req_id=42)
    parsed = parse_op(encode_op(op))
    assert parsed == op


def test_encode_system_op_omits_pin():
    body = encode_op(Op(op="all_off", req_id=7)).decode()
    assert "pin" not in body
    assert parse_op(encode_op(Op(op="all_off", req_id=7))).req_id == 7


def test_encode_skips_none_value():
    body = encode_op(Op(op="digital_read", pin=24)).decode()
    assert "value" not in body


# ----------------------------------------------------------------- parse_op -

def test_parse_op_defaults():
    op = parse_op(b'{"op":"noop"}')
    assert op == Op(op="noop", pin=0, value=None, req_id=0)


@pytest.mark.parametrize("body", [
    b"",                                  # empty
    b"not json",                          # garbage
    b"[1, 2]",                            # not an object
    b'{"pin": 17}',                       # missing op
    b'{"op": 42}',                        # wrong type
    b'{"op": ""}',                        # empty
    b'{"op": "eval"}',                    # unknown op
    b'{"op":"digital_write","pin":"17"}', # pin wrong type
    b'{"op":"digital_write","pin":true}', # bool is not an int
    b'{"op":"digital_write","pin":1.5}',  # float pin
    b'{"op":"digital_write","req_id":"x"}',
    b'{"op":"pwm_write","value":"60"}',
    b'{"op":"pwm_write","value":NaN}',    # NaN not JSON-legal... but be safe
    b'{"op":"pwm_write","value":' + str(MAX_ABS_VALUE * 10).encode() + b"}",
    b"\xff\xfe",                          # undecodable
])
def test_parse_op_rejects(body):
    with pytest.raises(ProtocolError):
        parse_op(body)


def test_parse_op_rejects_oversized_body():
    with pytest.raises(ProtocolError):
        parse_op(b" " * (MAX_BODY + 1))


def test_parse_op_accepts_bool_value_for_level():
    op = parse_op(b'{"op":"digital_write","pin":17,"value":true}')
    assert op.value is True


def test_parse_op_accepts_float_pwm_value():
    op = parse_op(b'{"op":"pwm_write","pin":18,"value":62.5}')
    assert op.value == 62.5


def test_parse_op_accepts_packed_rgb_and_negative_motor():
    """rgb_write maxes out at 0xRRGGBB; motor_write goes negative."""
    op = parse_op(b'{"op":"rgb_write","pin":19,"value":16777215}')
    assert op.value == 0xFFFFFF
    op = parse_op(b'{"op":"motor_write","pin":13,"value":-75.5}')
    assert op.value == -75.5


# ------------------------------------------------------------------ results --

def test_result_roundtrip():
    res = OpResult(ok=True, req_id=9, value=21.3)
    assert parse_result(encode_result(res)) == res


def test_result_error_roundtrip():
    res = OpResult(ok=False, req_id=9, error="pin 17 is input-only")
    assert parse_result(encode_result(res)) == res


@pytest.mark.parametrize("body", [b"", b"nope", b"[]", b'{"ok":"yes"}', b"{}"])
def test_parse_result_is_total(body):
    """A broken reply never raises — it becomes ok=False for the GUI log."""
    res = parse_result(body)
    assert res.ok is False
    assert res.error


# ------------------------------------------------------------------- state --

def test_state_roundtrip():
    states = [
        PinState(bcm=17, direction="out", value=True),
        PinState(bcm=4, direction="sensor", value=21.3, unit="°C"),
        PinState(bcm=-1, direction="sensor", fault="requires ADC (not fitted)"),
    ]
    assert parse_state(encode_state(states)) == states


@pytest.mark.parametrize("body", [
    b"",
    b"[]",
    b'{"pins": {}}',
    b'{"pins": [42]}',
    b'{"pins": [{"direction": "out"}]}',           # missing bcm
    b'{"pins": [{"bcm": "17", "direction": "out"}]}',
    b'{"pins": [{"bcm": 17, "direction": 7}]}',
])
def test_parse_state_rejects_shapes(body):
    with pytest.raises(ProtocolError):
        parse_state(body)


# ------------------------------------------------------------------ script ---

def test_script_roundtrip():
    body = encode_script("print('hi')", req_id=5)
    code, req_id = parse_script(body)
    assert code == "print('hi')" and req_id == 5


@pytest.mark.parametrize("body", [
    b"",
    b"not json",
    b"[]",
    b"{}",                                   # missing script
    b'{"script": ""}',                       # empty
    b'{"script": "   "}',                    # whitespace only
    b'{"script": 42}',                       # wrong type
    # too big: explicit short id — the 64 KiB body must not end up in the
    # test ID (Windows caps env vars at 32767 chars: PYTEST_CURRENT_TEST).
    pytest.param(b'{"script": "' + b"x" * (MAX_SCRIPT_BODY + 1) + b'"}',
                 id="too-big"),
])
def test_parse_script_rejects(body):
    with pytest.raises(ProtocolError):
        parse_script(body)


def test_script_status_roundtrip_in_state():
    states = [PinState(bcm=17, direction="out", value=True)]
    script = ScriptStatus(enabled=True, running=True, job=3,
                          output="hello\nworld")
    body = encode_state(states, script=script)
    assert parse_state(body) == states
    assert parse_script_status(body) == script


def test_script_status_absent_means_disabled():
    """An old bridge (no 'script' key) degrades to 'not enabled'."""
    assert parse_script_status(encode_state([])) == ScriptStatus()


@pytest.mark.parametrize("body", [b"", b"nope", b'{"script": 7}',
                                  b'{"script": {"job": "x"}}'])
def test_parse_script_status_is_total(body):
    """A garbled script block never raises — it becomes the default."""
    status = parse_script_status(body)
    assert status.enabled is False and status.running is False


# ------------------------------------------------------------- cross-module --

def test_role_ops_are_known_wire_ops():
    """pins.ROLE_OPS may only reference ops the protocol actually accepts —
    catches drift between the two contract modules."""
    for role, ops in pins.ROLE_OPS.items():
        unknown = ops - KNOWN_OPS
        assert not unknown, f"ROLE_OPS[{role}] references unknown ops: {unknown}"


def test_system_ops_disjoint_from_role_ops():
    role_ops = set().union(*pins.ROLE_OPS.values())
    assert not (role_ops & protocol.SYSTEM_OPS)


def test_default_port_is_stable():
    """The GUI's default and pi.py's default must be the same number."""
    assert protocol.DEFAULT_PORT == 8642
