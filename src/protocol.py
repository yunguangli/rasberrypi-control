"""Wire contract between the PC GUI (main.py) and the Pi bridge (pi.py).

Stdlib-only (json/dataclasses/typing) — no flet, no gpiozero. Import boundary
is enforced by tests/test_import_boundary.py.

Deployment note: part of the 3-file copy set for the Pi.

Wire shape (HTTP + JSON, see plan.md §4):
  POST /op    {"op":"digital_write","pin":17,"value":1,"req_id":42}
              -> 200 {"ok":true,"req_id":42,...}  or  400 {"ok":false,"error":...}
  GET /state  -> 200 {"ok":true,"pins":[PinState,...]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass

DEFAULT_PORT = 8642

# Ops that act globally (no pin needed); everything else is validated against
# the target pin's role in pins.ROLE_OPS.
SYSTEM_OPS = frozenset({"noop", "all_off"})

# Every op name that may appear on the wire. Keep in sync with ROLE_OPS —
# tests assert ROLE_OPS values are a subset of this.
KNOWN_OPS = SYSTEM_OPS | frozenset({
    "digital_write", "digital_read", "pwm_write",
    "pulse", "read", "set_input",
})

MAX_BODY = 4096        # request bodies are tiny; refuse anything larger
MAX_ABS_VALUE = 1_000_000


class ProtocolError(ValueError):
    """Malformed request — callers translate this into a 400 OpResult."""


@dataclass(frozen=True)
class Op:
    """One command from PC -> Pi. `req_id` echoes back for log correlation."""

    op: str
    pin: int = 0
    value: float | int | bool | None = None
    req_id: int = 0


@dataclass(frozen=True)
class OpResult:
    """Reply from Pi -> PC. Always structured — never a traceback."""

    ok: bool
    req_id: int = 0
    value: float | int | bool | None = None
    error: str = ""


@dataclass(frozen=True)
class PinState:
    """Live state of one wired pin, as reported by GET /state."""

    bcm: int
    direction: str                    # "out" | "in" | "pwm" | "sensor"
    value: float | int | bool | None = None   # level, duty %, or sensor reading
    unit: str = ""
    fault: str = ""                   # device error surfaced to the card


# ---------------------------------------------------------------- encode ----

def encode_op(op: Op) -> bytes:
    body: dict = {"op": op.op, "req_id": op.req_id}
    if op.op not in SYSTEM_OPS:
        body["pin"] = op.pin
    if op.value is not None:
        body["value"] = op.value
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def encode_result(res: OpResult) -> bytes:
    body: dict = {"ok": res.ok, "req_id": res.req_id}
    if res.value is not None:
        body["value"] = res.value
    if res.error:
        body["error"] = res.error
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def encode_state(states: list[PinState]) -> bytes:
    return json.dumps(
        {"ok": True, "pins": [s.__dict__ for s in states]},
        separators=(",", ":"),
    ).encode("utf-8")


# ---------------------------------------------------------------- decode ----

def _require_int(raw: dict, key: str, default: int) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError(f"'{key}' must be an integer")
    return v


def parse_op(body: bytes) -> Op:
    """Parse a request body into an Op; raise ProtocolError on anything odd."""
    if len(body) > MAX_BODY:
        raise ProtocolError(f"body exceeds {MAX_BODY} bytes")
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ProtocolError(f"malformed JSON: {ex}") from ex
    if not isinstance(raw, dict):
        raise ProtocolError("body must be a JSON object")

    name = raw.get("op")
    if not isinstance(name, str) or not name:
        raise ProtocolError("'op' must be a non-empty string")
    if name not in KNOWN_OPS:
        raise ProtocolError(f"unknown op '{name}'")

    pin = _require_int(raw, "pin", 0)
    req_id = _require_int(raw, "req_id", 0)

    value = raw.get("value")
    if value is not None and not isinstance(value, (int, float, bool)):
        raise ProtocolError("'value' must be a number, bool, or null")
    if isinstance(value, float) and value != value:  # NaN
        raise ProtocolError("'value' must not be NaN")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(value) > MAX_ABS_VALUE:
            raise ProtocolError("'value' out of range")

    return Op(op=name, pin=pin, value=value, req_id=req_id)


def parse_result(body: bytes) -> OpResult:
    """Parse a reply body into an OpResult (tolerant: a broken reply becomes
    ok=False rather than an exception, so the GUI can surface it)."""
    try:
        raw = json.loads(body.decode("utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("ok"), bool):
            raise ValueError("not an OpResult")
        return OpResult(
            ok=raw["ok"],
            req_id=int(raw.get("req_id", 0)),
            value=raw.get("value"),
            error=str(raw.get("error", "")),
        )
    except Exception as ex:  # noqa: BLE001 — deliberately total
        return OpResult(ok=False, error=f"unparseable reply: {ex}")


def parse_state(body: bytes) -> list[PinState]:
    """Parse GET /state reply into PinState rows; raise ProtocolError if the
    shape is wrong (caller decides how to surface it)."""
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ProtocolError(f"malformed JSON: {ex}") from ex
    if not isinstance(raw, dict) or not isinstance(raw.get("pins"), list):
        raise ProtocolError("state reply must be {'pins': [...]}")

    states: list[PinState] = []
    for row in raw["pins"]:
        if not isinstance(row, dict):
            raise ProtocolError("state row must be an object")
        bcm = row.get("bcm")
        direction = row.get("direction")
        if isinstance(bcm, bool) or not isinstance(bcm, int):
            raise ProtocolError("state row 'bcm' must be an integer")
        if not isinstance(direction, str):
            raise ProtocolError("state row 'direction' must be a string")
        states.append(PinState(
            bcm=bcm,
            direction=direction,
            value=row.get("value"),
            unit=str(row.get("unit", "")),
            fault=str(row.get("fault", "")),
        ))
    return states
