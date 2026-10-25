"""Domain layer: pin profile -> tool schema -> validated wire ops.

Pure: no HTTP, no flet, no gpiozero. Three responsibilities, in order:

  1. `Capabilities`  — the tool list and the system prompt, derived from
                       `pins.PROFILE` + `pins.ROLE_OPS` (the same contract
                       `pi.py` validates against).
  2. `ToolCompiler`  — turn a tool call into a `protocol.Op`, or raise
                       `ToolError` with a message the model can act on.
  3. `Facts`         — learned aliases persisted as JSON.

Adapters (`agent.OpenAIChat`, `main.PiClient`) sit outside this module, so
the same schema and the same validation serve the Flet app today and an MCP
server later.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

import pins
from pins import PinProfile, Role
from protocol import Op, OpResult, PinState

__all__ = [
    "Call", "Capabilities", "DeviceTool", "Facts", "Pause", "Step",
    "ToolCompiler", "ToolError", "build_capabilities",
]


# ---------------------------------------------------------------------------
# Action space
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """One tool action and the wire op it compiles to.

    `level` is the fixed value for on/off; `param` names the argument that
    carries the value for everything else.
    """

    name: str
    op: str
    param: str | None = None
    level: bool | None = None


ACTIONS: dict[str, ActionSpec] = {
    "on": ActionSpec("on", "digital_write", level=True),
    "off": ActionSpec("off", "digital_write", level=False),
    "pulse": ActionSpec("pulse", "pulse", "ms"),
    "tone": ActionSpec("tone", "tone", "hz"),
    "color": ActionSpec("color", "rgb_write", "rgb"),
    "set": ActionSpec("set", "pwm_write", "percent"),
    "drive": ActionSpec("drive", "motor_write", "percent"),
    "read": ActionSpec("read", "digital_read"),
}

# Defaults per role; a driver entry below wins when present.
_ROLE_ACTIONS: dict[Role, tuple[str, ...]] = {
    Role.OUTPUT: ("on", "off", "pulse"),
    Role.PWM: ("set",),
    Role.INPUT: ("read",),
    Role.SENSOR: ("read",),
}

_DRIVER_ACTIONS: dict[str, tuple[str, ...]] = {
    "TonalBuzzer": ("on", "off", "pulse", "tone"),
    "RGBLED": ("on", "off", "color"),
    "Motor": ("drive",),
}

# JSON Schema fragments, one per parameter name.
_PARAM_SCHEMA: dict[str, dict[str, Any]] = {
    "ms": {"type": "integer", "minimum": 1, "maximum": 10000,
           "description": "duration in milliseconds (default 300)"},
    "hz": {"type": "integer", "minimum": 200, "maximum": 5000,
           "description": "tone frequency in Hz (each tone lasts 400 ms)"},
    "rgb": {"type": "integer", "minimum": 0, "maximum": 16777215,
            "description": "colour packed as 0xRRGGBB"},
    "percent": {"type": "integer", "minimum": 0, "maximum": 100,
                "description": "level in percent"},
}

_PARAM_ORDER: tuple[str, ...] = ("ms", "hz", "rgb", "percent")


def _wire_op(action: str, role: Role) -> str:
    """`read` has two wire ops: sensors use `read`, inputs `digital_read`."""
    if action == "read" and role is Role.SENSOR:
        return "read"
    return ACTIONS[action].op


def actions_for(profile: PinProfile) -> tuple[str, ...]:
    """Actions this device accepts, filtered by its role's allowed ops."""
    names = _DRIVER_ACTIONS.get(profile.driver) or _ROLE_ACTIONS.get(
        profile.role, ())
    allowed = pins.ops_for(profile.role)
    return tuple(n for n in names if _wire_op(n, profile.role) in allowed)


def params_for(profile: PinProfile) -> dict[str, Any]:
    """Only the parameters this device's actions actually use."""
    actions = actions_for(profile)
    wanted = {ACTIONS[a].param for a in actions} - {None}
    out: dict[str, Any] = {}
    for name in _PARAM_ORDER:
        if name not in wanted:
            continue
        schema = dict(_PARAM_SCHEMA[name])
        if name == "percent" and "drive" in actions:
            schema["minimum"] = -100
            schema["description"] = "speed in percent; negative = reverse"
        out[name] = schema
    return out


def tool_name(profile: PinProfile) -> str:
    """A safe function name: "Light (ADC)" -> "light_adc"."""
    return re.sub(r"[^a-z0-9]+", "_", profile.name.lower()).strip("_")


# ---------------------------------------------------------------------------
# Device tool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceTool:
    """One wired device, as an OpenAI-style function tool."""

    name: str
    profile: PinProfile
    actions: tuple[str, ...]

    @property
    def bcm(self) -> int:
        return self.profile.bcm if self.profile.bcm is not None else 0

    @property
    def description(self) -> str:
        p = self.profile
        where = (f"on BCM {p.bcm}" if p.bcm >= 0
                 else "virtual — not on the header")
        parts = [p.name, f"{p.role.value} {where}"]
        if p.physical:
            parts.append(f"physical pin {p.physical}")
        if p.driver:
            parts.append(f"driver {p.driver}")
        if p.unit:
            parts.append(f"reads in {p.unit}")
        if p.active_low:
            parts.append("active-low")
        parts.append("actions: " + ", ".join(self.actions))
        return "; ".join(parts)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": list(self.actions),
                                   "description": "what to do"},
                        **params_for(self.profile),
                    },
                    "required": ["action"],
                },
            },
        }


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


class Facts:
    """Flat key -> value store (aliases, learned devices), saved as JSON."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError):
                self._data = {}  # corrupt file: start clean, never crash

    @property
    def data(self) -> dict[str, Any]:
        return dict(self._data)

    def resolve(self, key: str) -> Any | None:
        if key in self._data:
            return self._data[key]
        lowered = key.lower()
        return next((v for k, v in self._data.items()
                     if k.lower() == lowered), None)

    def remember(self, key: str, value: Any) -> None:
        self._data[key] = value
        if not self.path:
            return
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # Windows: best effort


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class Capabilities:
    """The device tools, the global tools and the system prompt."""

    def __init__(self, facts: Facts | None = None) -> None:
        self.facts = facts or Facts()
        self.tools: list[DeviceTool] = []
        self.by_name: dict[str, DeviceTool] = {}
        for profile in pins.PROFILE.values():
            actions = actions_for(profile)
            if not actions:
                continue
            name = tool_name(profile)
            if name in self.by_name:
                raise ValueError(f"duplicate tool name '{name}' "
                                 f"({profile.name})")
            tool = DeviceTool(name, profile, actions)
            self.tools.append(tool)
            self.by_name[name] = tool

    @property
    def tool_schema(self) -> list[dict[str, Any]]:
        """The `tools` array for /chat/completions."""
        return [t.schema() for t in self.tools] + list(_GLOBAL_TOOLS)

    def system_prompt(self, state: Iterable[PinState] | None = None,
                      script: Any = None,
                      facts: dict[str, Any] | None = None) -> str:
        profiles = {p.bcm: p for p in pins.PROFILE.values()}
        state_lines: list[str] = []
        for item in state or []:
            profile = profiles.get(item.bcm)
            label = profile.name if profile else f"BCM {item.bcm}"
            value = item.value
            if isinstance(value, bool):
                value = "on" if value else "off"
            elif isinstance(value, float):
                value = f"{value:.3g}"
            suffix = f" {item.unit}" if item.unit else ""
            fault = f"  FAULT: {item.fault}" if item.fault else ""
            state_lines.append(f"  {label} (BCM {item.bcm}): {value}{suffix}"
                               f"{fault}")
        if not state_lines:
            state_lines.append("  (none — the bridge may be offline)")

        learned = facts if facts is not None else self.facts.data
        fact_lines = [f"  {k} = {json.dumps(v)}"
                      for k, v in sorted(learned.items())] or ["  (none)"]

        tool_lines = [
            f"  {t.name}: {', '.join(t.actions)}  — {t.profile.name}, "
            + (f"BCM {t.profile.bcm}" if t.profile.bcm >= 0 else "virtual")
            for t in self.tools
        ]

        running = ""
        if script is not None and getattr(script, "running", False):
            running = ("\nA script job is running; its GPIO is released, so "
                       "device actions will fail until it stops.")

        return (
            "You control a Raspberry Pi's GPIO with the tools below. "
            "For any request about the board, call a tool; a prose answer "
            "alone is wrong. Answer in one or two short sentences.\n\n"
            "Devices:\n" + "\n".join(tool_lines) + "\n\n"
            "Global tools: all_off, blink, run_sequence, ask_user, remember, "
            "write_script.\n\n"
            "Live state:\n" + "\n".join(state_lines) + "\n\n"
            "Learned aliases:\n" + "\n".join(fact_lines) + "\n\n"
            "Rules:\n"
            "  1. One device action handles most requests; use blink for "
            "'N times', and run_sequence for beeps, sweeps and melodies "
            "(buzzer tone steps plus wait_ms; each tone lasts 400 ms; max "
            "64 steps).\n"
            "  2. Ask the operator only when two devices could match the "
            "request; otherwise pick the obvious one.\n"
            "  3. When the operator resolves an ambiguity, call remember so "
            "you never ask again.\n"
            "  4. write_script only for loops, events or sensor-driven "
            "logic — it offers a card the operator can open in the editor; "
            "it is never run automatically.\n"
            "  5. Never invent a device, pin or action; if it is not listed, "
            "ask."
            + running
        )


def build_capabilities(facts_path: str | None = None) -> Capabilities:
    return Capabilities(Facts(facts_path))


# ---------------------------------------------------------------------------
# Global tools
# ---------------------------------------------------------------------------

_GLOBAL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "all_off",
            "description": "Turn off every output, dimmer and motor.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blink",
            "description": "Blink one output device N times — the simple "
                           "on/off pattern. Use this instead of run_sequence "
                           "for 'blink N times'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string",
                               "description": "tool name, e.g. status_led"},
                    "times": {"type": "integer", "minimum": 1,
                              "maximum": 20,
                              "description": "blink count (default 3)"},
                    "on_ms": {"type": "integer", "minimum": 20,
                              "maximum": 10000,
                              "description": "on duration (default 300)"},
                    "off_ms": {"type": "integer", "minimum": 20,
                               "maximum": 10000,
                               "description": "off duration (default 300)"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sequence",
            "description": "Run several device actions in order with optional "
                           "pauses (up to 64 steps). A step may be just "
                           "{'wait_ms': 300}. For a melody use buzzer tone "
                           "steps, e.g. {\"device\": \"buzzer\", \"action\": "
                           "\"tone\", \"hz\": 262}. Example: {\"steps\": ["
                           "{\"device\": \"status_led\", \"action\": \"on\"}, "
                           "{\"wait_ms\": 300}, {\"device\": \"status_led\", "
                           "\"action\": \"off\"}]}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "device": {"type": "string",
                                           "description": "tool name"},
                                "action": {"type": "string"},
                                "wait_ms": {"type": "integer", "minimum": 0,
                                            "maximum": 60000},
                                "ms": {"type": "integer", "minimum": 1,
                                       "maximum": 10000},
                                "hz": {"type": "integer", "minimum": 200,
                                       "maximum": 5000},
                                "rgb": {"type": "integer", "minimum": 0,
                                        "maximum": 16777215},
                                "percent": {"type": "integer",
                                            "minimum": -100,
                                            "maximum": 100},
                            },
                            "required": ["action"],
                        },
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the operator a clarifying question and wait "
                           "for the answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "short choices to tap"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Persist an alias or fact for future turns, e.g. "
                           "key 'led', value 'status_led'.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {}},
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_script",
            "description": "Offer a Python script as a card in the transcript; "
                           "the operator can open it in the Script editor to "
                           "review and run. Never runs automatically.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"},
                               "title": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Compiler: tool call -> wire Op
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A call that failed locally (unknown device, bad action, out of range).

    No HTTP happened. The message is written for the model to correct itself
    and for the operator to read.
    """


@dataclass(frozen=True)
class Call:
    """One validated device action, ready to send."""

    label: str
    op: Op


@dataclass(frozen=True)
class Pause:
    """A bare wait inside a sequence."""

    ms: int


Step = Call | Pause


class ToolCompiler:
    """Validate tool calls against the profile and compile them to `Op`s.

    Pure: nothing is sent. `compile_sequence` validates every step before
    returning, so a typo in step 5 cannot leave the hardware half-way
    through a pattern.
    """

    def __init__(self, caps: Capabilities) -> None:
        self.caps = caps

    # -- public -------------------------------------------------------------

    def compile(self, device: str, action: str,
                params: dict[str, Any] | None = None,
                req_id: int = 0) -> Call:
        tool = self.resolve(device)
        if action not in tool.actions:
            raise ToolError(
                f"'{action}' is not available on {tool.name} "
                f"(allowed: {', '.join(tool.actions)})")
        spec = ACTIONS[action]
        value = spec.level if spec.level is not None else (
            None if spec.param is None
            else self._value(action, spec.param, params or {}))
        op = Op(op=_wire_op(action, tool.profile.role),
                pin=tool.bcm, value=value, req_id=req_id)
        return Call(label=f"{tool.name}.{action}", op=op)

    def compile_sequence(self, steps: Any, req_id: int = 0,
                         max_steps: int = 64) -> list[Step]:
        if not isinstance(steps, list) or not steps:
            raise ToolError("run_sequence needs a non-empty 'steps' list")
        if len(steps) > max_steps:
            raise ToolError(f"run_sequence: at most {max_steps} steps")
        plan: list[Step] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ToolError(f"step {index} must be an object")
            wait = self._wait(step.get("wait_ms", 0), index)
            device, action = step.get("device"), step.get("action")
            if device is None and action is None:
                if not wait:
                    raise ToolError(f"step {index} is empty")
                plan.append(Pause(wait))
                continue
            if not isinstance(device, str) or not device:
                raise ToolError(f"step {index} needs 'device'")
            if not isinstance(action, str) or not action:
                raise ToolError(f"step {index} needs 'action'")
            try:
                call = self.compile(device, action, step, req_id)
            except ToolError as ex:
                raise ToolError(f"step {index}: {ex}") from None
            plan.append(call)
            if wait:
                plan.append(Pause(wait))
        return plan

    def compile_blink(self, device: str, times: Any = None,
                      on_ms: Any = None, off_ms: Any = None,
                      req_id: int = 0) -> list[Step]:
        """Expand a blink into on/pause/off/pause steps.

        Simpler and far more reliable for small models than asking them to
        hand-write a `run_sequence` with repeats.
        """
        tool = self.resolve(device)
        if "on" not in tool.actions or "off" not in tool.actions:
            raise ToolError(f"{tool.name} cannot blink (needs on and off)")
        count = self._int(times, "times", 1, 20, 3)
        on = self._int(on_ms, "on_ms", 20, 10000, 300)
        off = self._int(off_ms, "off_ms", 20, 10000, 300)
        on_call = self.compile(tool.name, "on", req_id=req_id)
        off_call = self.compile(tool.name, "off", req_id=req_id)
        plan: list[Step] = []
        for _ in range(count):
            plan.extend((on_call, Pause(on), off_call, Pause(off)))
        return plan

    def all_off(self, req_id: int = 0) -> Call:
        return Call(label="all_off", op=Op(op="all_off", req_id=req_id))

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _int(raw: Any, name: str, low: int, high: int,
             default: int) -> int:
        if raw is None:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolError(f"{name} must be an integer {low}..{high}")
        if not low <= raw <= high:
            raise ToolError(f"{name} {raw} is out of range {low}..{high}")
        return raw

    def resolve(self, device: str) -> DeviceTool:
        tool = self.caps.by_name.get(device)
        if tool is not None:
            return tool
        alias = self.caps.facts.resolve(device)
        if isinstance(alias, str) and alias in self.caps.by_name:
            return self.caps.by_name[alias]
        known = ", ".join(sorted(self.caps.by_name))
        raise ToolError(f"unknown device '{device}' (known: {known})")

    @staticmethod
    def _wait(raw: Any, index: int) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int) \
                or not 0 <= raw <= 60000:
            raise ToolError(f"step {index}: wait_ms must be 0..60000")
        return raw

    @staticmethod
    def _value(action: str, key: str, params: dict[str, Any]) -> Any:
        raw = params.get(key)
        if key == "percent":
            low = -100 if action == "drive" else 0
            if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
                raise ToolError(f"{action} needs '{key}' as an integer "
                                f"({low}..100)")
            if not low <= raw <= 100:
                raise ToolError(f"{key} {raw} is out of range {low}..100")
            return raw
        if key == "ms":
            if raw is None:
                return 300  # same default as the GUI pulse button
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ToolError("pulse needs 'ms' as an integer 1..10000")
            if not 1 <= raw <= 10000:
                raise ToolError(f"pulse ms {raw} is out of range 1..10000")
            return raw
        if key == "hz":
            if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
                raise ToolError("tone needs 'hz' as an integer 200..5000")
            if not 200 <= raw <= 5000:
                raise ToolError(f"hz {raw} is out of range 200..5000")
            return raw
        if key == "rgb":
            if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
                raise ToolError("color needs 'rgb' as an integer 0..16777215 "
                                "(0xRRGGBB)")
            if not 0 <= raw <= 0xFFFFFF:
                raise ToolError(f"rgb {raw} is out of range 0..16777215")
            return raw
        raise ToolError(f"internal: unmapped parameter '{key}'")
