"""Agent use case: one user turn of model <-> tool calls <-> Pi.

Layers (ports and adapters):

    Agent          use case: the loop, history, cancellation
    Model          port:     complete(messages, tools) -> AssistantMessage
    OpenAIChat     adapter:  /chat/completions over stdlib urllib
    transport      port:     Op -> OpResult   (PiClient.post_op in the app)
    ask_user       port:     question, options, timeout -> answer | None

No flet import: the GUI runs `turn()` in a worker thread and drains events;
tests drive the same code with a fake `Model` and a recording transport.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from capabilities import (
    Call,
    Capabilities,
    Pause,
    ToolCompiler,
    ToolError,
    build_capabilities,
)
from protocol import Op, OpResult

__all__ = [
    "Agent", "AgentEvent", "AgentTurn", "AssistantMessage", "ModelError",
    "OpenAIChat", "Settings", "ToolCall", "make_agent",
    "normalise_base_url",
]

MAX_STEPS = 8            # model round-trips per user turn
MAX_TURNS = 6            # user turns kept in history
MODEL_TIMEOUT = 60.0     # seconds per model request
MAX_OUTPUT_TOKENS = 3000 # reasoning models spend a lot before the tool call
DEFAULT_PULSE_MS = 300


# ---------------------------------------------------------------------------
# Model port
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    # Non-empty when the provider sent arguments that were not a JSON object
    # (some free models truncate them). Never resent to the model verbatim.
    parse_error: str = ""


@dataclass(frozen=True)
class AssistantMessage:
    """A model reply: prose, tool calls, or both."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    def normalised_calls(self) -> tuple[ToolCall, ...]:
        """Calls with a non-empty id; some providers omit it.

        OpenRouter rejects a history where a `tool` message has an empty
        `tool_call_id`, so both the assistant message and the tool replies
        must use the same synthesised id.
        """
        return tuple(
            call if call.id else ToolCall(
                id=f"call_{index}", name=call.name,
                arguments=call.arguments, parse_error=call.parse_error)
            for index, call in enumerate(self.tool_calls)
        )

    def to_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation.

        Raw provider fields (e.g. reasoning) are kept, but `tool_calls` are
        rebuilt from the parsed arguments: echoing a truncated/invalid
        arguments string makes the next request fail with HTTP 400.
        """
        calls = self.normalised_calls()
        message: dict[str, Any] = (dict(self.raw) if self.raw
                                   else {"role": "assistant"})
        message["role"] = "assistant"
        message["content"] = self.content or None
        if calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.arguments)}}
                for c in calls
            ]
        else:
            message.pop("tool_calls", None)
        return message


class ModelError(Exception):
    """The model endpoint failed (HTTP, unreachable, malformed reply)."""


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> AssistantMessage:
        ...


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


class OpenAIChat:
    """`/chat/completions` with `tools` — OpenRouter, Ollama, LM Studio, …

    Only the fields every OpenAI-compatible server understands are sent, so
    the same adapter works locally and in the cloud.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = MODEL_TIMEOUT,
                 opener: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        # Ignore shell proxies: a LAN model or a Pi address is unreachable
        # through them, and the address would leak to the proxy.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> AssistantMessage:
        body = {"model": self.model, "messages": messages,
                "tools": tools, "max_tokens": MAX_OUTPUT_TOKENS}
        payload = self._post(body)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("the model returned no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ModelError("the model returned no message")
        calls = []
        for call in message.get("tool_calls") or []:
            arguments, parse_error = _arguments(call)
            calls.append(ToolCall(
                id=str(call.get("id") or ""),
                name=str((call.get("function") or {}).get("name") or ""),
                arguments=arguments,
                parse_error=parse_error,
            ))
        return AssistantMessage(
            content=str(message.get("content") or ""),
            tool_calls=tuple(calls),
            raw={"role": "assistant",
                 "content": message.get("content"),
                 **({"tool_calls": message["tool_calls"]}
                    if message.get("tool_calls") else {})},
            finish_reason=str(choices[0].get("finish_reason") or ""),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST", headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as ex:
            try:
                detail = ex.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise ModelError(
                f"HTTP {ex.code} from {request.full_url}: {detail[:400]}") from ex
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise ModelError(
                f"model unreachable at {request.full_url}: "
                f"{getattr(ex, 'reason', ex)}") from ex
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise ModelError(f"the model returned invalid JSON: {ex}") from ex
        if not isinstance(payload, dict):
            raise ModelError("the model returned a non-object response")
        return payload


def _arguments(call: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Parse a tool call's arguments; also report why parsing failed."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw, ""
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as ex:
        return {}, str(ex)
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object"
    return parsed, ""


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEvent:
    """One thing that happened during a turn, in order.

    kind: thinking | text | action | question | answer | memory | script |
          error
    """

    kind: str
    text: str = ""
    result: OpResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTurn:
    events: list[AgentEvent] = field(default_factory=list)
    stopped: bool = False
    error: str = ""

    @property
    def reply(self) -> str:
        return "\n".join(e.text for e in self.events
                         if e.kind == "text" and e.text)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


# The digit 1 is routinely mistyped as i/l, and the glyphs are hard to tell
# apart in many fonts: "/api/vi" and "/api/vl" must become "/api/v1".
_VERSION_LOOKALIKES = ("/vi", "/vl", "/vI", "/vL")


def normalise_base_url(raw: str) -> str:
    """Strip a trailing slash and repair a lookalike `/v1` ending."""
    url = (raw or "").strip().rstrip("/")
    if url[-3:] in _VERSION_LOOKALIKES:
        url = url[:-3] + "/v1"
    return url


class Settings:
    """Model endpoint + key. Env vars provide defaults; the GUI overrides."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None,
                 facts_path: str | None = None,
                 stt_model: str | None = None) -> None:
        self.base_url = normalise_base_url(
            base_url or os.environ.get("PICONTROL_LLM_BASE",
                                       "http://localhost:11434/v1"))
        self.model = (model or os.environ.get("PICONTROL_LLM_MODEL", "")).strip()
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("PICONTROL_LLM_KEY", "")).strip()
        self.facts_path = facts_path or os.environ.get("PICONTROL_FACTS_PATH")
        self.stt_model = stt_model or os.environ.get("PICONTROL_STT_MODEL", "")

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def label(self) -> str:
        if not self.model:
            return "no model configured"
        # Keep the path: a mistyped "/api/vi" must be visible at a glance.
        tail = self.base_url.split("//", 1)[-1].rstrip("/")
        return f"{self.model} @ {tail}"

    def model_port(self) -> Model:
        return OpenAIChat(self.base_url, self.model, self.api_key)

    def to_dict(self) -> dict[str, Any]:
        return {"base_url": self.base_url, "model": self.model,
                "api_key": self.api_key, "facts_path": self.facts_path,
                "stt_model": self.stt_model}

    def save(self, path: str) -> None:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str) -> "Settings":
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        return cls(base_url=raw.get("base_url"), model=raw.get("model"),
                   api_key=raw.get("api_key"),
                   facts_path=raw.get("facts_path"),
                   stt_model=raw.get("stt_model"))


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """One conversation. `turn()` blocks; call it off the UI thread."""

    def __init__(self, model: Model, caps: Capabilities,
                 transport: Callable[[Op], OpResult],
                 ask_user: Callable[[str, list[str], float], str | None]
                 | None = None,
                 write_script: Callable[[str, str], OpResult] | None = None,
                 max_steps: int = MAX_STEPS) -> None:
        self.model = model
        self.caps = caps
        self.compiler = ToolCompiler(caps)
        self.transport = transport
        self.ask_user = ask_user
        self.write_script = write_script
        self.max_steps = max_steps
        self._turns: list[list[dict[str, Any]]] = []
        self._cancel = False
        self._req_id = 0

    # -- lifecycle ----------------------------------------------------------

    def cancel(self) -> None:
        """Stop between steps; the current request still completes."""
        self._cancel = True

    def reset(self) -> None:
        self._turns = []
        self._cancel = False

    @property
    def history(self) -> list[dict[str, Any]]:
        return [m for turn in self._turns for m in turn]

    # -- the turn -----------------------------------------------------------

    def turn(self, text: str,
             state: Iterable[Any] | None = None,
             script: Any = None,
             on_event: Callable[[AgentEvent], None] | None = None,
             ask_timeout: float = 300.0) -> AgentTurn:
        out = AgentTurn()
        emit = self._emitter(out, on_event)
        self._cancel = False
        emit(AgentEvent("thinking"))

        messages: list[dict[str, Any]] = [
            {"role": "system",
             "content": self.caps.system_prompt(state, script)}]
        messages.extend(self.history)
        user = {"role": "user", "content": text}
        messages.append(user)
        turn_messages: list[dict[str, Any]] = [user]

        for _ in range(self.max_steps):
            if self._cancel:
                self._fail(out, emit, "stopped")
                break
            try:
                reply = self.model.complete(messages, self.caps.tool_schema)
            except ModelError as ex:
                self._fail(out, emit, str(ex))
                break

            if reply.content:
                emit(AgentEvent("text", reply.content))
            if not reply.tool_calls:
                if not reply.content:
                    # A reasoning model can burn the whole output budget and
                    # return nothing. Silence is worse than a clear error.
                    self._fail(out, emit, _empty_reply_error(reply))
                else:
                    turn_messages.append(reply.to_message())
                break

            assistant = reply.to_message()
            messages.append(assistant)
            turn_messages.append(assistant)
            cancelled = False
            for call in reply.normalised_calls():
                if self._cancel:
                    cancelled = True
                    break
                tool_message = self._run_tool(call, emit, ask_timeout)
                messages.append(tool_message)
                turn_messages.append(tool_message)
            if cancelled:
                self._fail(out, emit, "stopped")
                break
        else:
            self._fail(out, emit,
                       "hit the tool-call budget for this turn")

        # Only a completed turn joins the history. A cancelled/failed turn can
        # end between an assistant `tool_calls` message and its tool replies;
        # committing that would orphan the call and every later request would
        # be rejected by the endpoint.
        if not out.stopped:
            self._turns.append(turn_messages)
            self._turns = self._turns[-MAX_TURNS:]
        return out

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _emitter(out: AgentTurn,
                 on_event: Callable[[AgentEvent], None] | None
                 ) -> Callable[[AgentEvent], None]:
        def emit(event: AgentEvent) -> None:
            out.events.append(event)
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # a broken UI callback must not stop the loop
                    pass
        return emit

    @staticmethod
    def _fail(out: AgentTurn, emit: Callable[[AgentEvent], None],
              error: str) -> None:
        out.stopped = True
        out.error = error
        emit(AgentEvent("error", error))

    def _req(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send(self, call: Call) -> OpResult:
        return self.transport(call.op)

    def _run_tool(self, call: ToolCall,
                  emit: Callable[[AgentEvent], None],
                  ask_timeout: float) -> dict[str, Any]:
        """Execute one tool call; return the `role: tool` reply message."""
        try:
            if call.parse_error:
                raise ToolError(
                    f"could not parse the arguments for {call.name} as a "
                    f"JSON object ({call.parse_error}); send valid JSON")
            text, payload = self._dispatch(call, emit, ask_timeout)
        except ToolError as ex:
            text, payload = str(ex), {"ok": False, "error": str(ex)}
            emit(AgentEvent("action", f"{call.name}: {ex}",
                            result=OpResult(ok=False, error=str(ex))))
        return {"role": "tool", "tool_call_id": call.id, "name": call.name,
                "content": json.dumps(payload)}

    def _dispatch(self, call: ToolCall,
                  emit: Callable[[AgentEvent], None],
                  ask_timeout: float) -> tuple[str, Any]:
        name, params = call.name, call.arguments

        if name == "all_off":
            return self._action(self.compiler.all_off(self._req()), emit)

        if name == "run_sequence":
            return self._sequence(params.get("steps"), emit)

        if name == "blink":
            device = params.get("device")
            if not isinstance(device, str) or not device:
                raise ToolError(
                    "blink needs a 'device' (tool name, e.g. status_led)")
            plan = self.compiler.compile_blink(
                device, params.get("times"), params.get("on_ms"),
                params.get("off_ms"), self._req())
            return self._plan(plan, emit)

        if name == "ask_user":
            question = params.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ToolError("ask_user needs a 'question'")
            options = [str(o) for o in (params.get("options") or [])][:6] \
                if isinstance(params.get("options"), list) else []
            if self.ask_user is None:
                raise ToolError("this build cannot ask questions")
            emit(AgentEvent("question", question,
                            extra={"options": options}))
            answer = self.ask_user(question, options, ask_timeout)
            if answer is None:
                raise ToolError("the operator did not answer")
            emit(AgentEvent("answer", answer))
            return f"operator answered: {answer}", {"answer": answer}

        if name == "remember":
            key = params.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ToolError("remember needs a 'key'")
            self.caps.facts.remember(key, params.get("value"))
            emit(AgentEvent("memory",
                            f"remembered {key} = {params.get('value')!r}"))
            return f"remembered {key}", {"ok": True, "key": key}

        if name == "write_script":
            code = params.get("code")
            title = params.get("title") or ""
            if not isinstance(code, str) or not code.strip():
                raise ToolError("write_script needs 'code'")
            if self.write_script is None:
                raise ToolError("script jobs are disabled "
                                "(start pi.py with --allow-code)")
            result = self.write_script(code, str(title))
            emit(AgentEvent("script", str(title) or "script ready",
                            result=result,
                            extra={"code": code, "title": str(title)}))
            return ("script offered in the transcript (not run)",
                    _payload(result))

        action = params.get("action")
        if not isinstance(action, str) or not action:
            raise ToolError(f"{name} needs an 'action'")
        args = {k: v for k, v in params.items() if k != "action"}
        return self._action(
            self.compiler.compile(name, action, args, self._req()), emit)

    def _action(self, call: Call,
                emit: Callable[[AgentEvent], None]) -> tuple[str, Any]:
        result = self._send(call)
        emit(AgentEvent("action", _describe(call.label, result),
                        result=result))
        return _describe(call.label, result), _payload(result)

    def _sequence(self, steps: Any,
                  emit: Callable[[AgentEvent], None]) -> tuple[str, Any]:
        return self._plan(self.compiler.compile_sequence(steps, self._req()),
                          emit)

    def _plan(self, plan: list[Step],
              emit: Callable[[AgentEvent], None]) -> tuple[str, Any]:
        done = 0
        for step in plan:
            if self._cancel:
                raise ToolError("stopped")
            if isinstance(step, Pause):
                time.sleep(step.ms / 1000.0)
                continue
            text, payload = self._action(step, emit)
            done += 1
            if not payload.get("ok"):
                return f"sequence failed: {payload.get('error', '')}", {
                    "ok": False, "error": payload.get("error", ""),
                    "steps": done}
        return f"sequence ok ({done} steps)", {"ok": True, "steps": done}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_reply_error(reply: AssistantMessage) -> str:
    if reply.finish_reason == "length":
        return ("the model ran out of output tokens before answering "
                "(finish_reason=length); try a shorter request, a larger "
                "output budget, or another model")
    return (f"the model returned an empty reply "
            f"(finish_reason={reply.finish_reason or 'unknown'})")


def _describe(label: str, result: OpResult) -> str:
    if result.ok:
        value = result.value
        if isinstance(value, bool):
            return f"{label} → {'on' if value else 'off'}"
        if value is None:
            return f"{label} ✓"
        return f"{label} → {value}"
    return f"{label} ✗ {result.error or 'failed'}"


def _payload(result: OpResult) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": result.ok}
    if result.value is not None:
        body["value"] = result.value
    if result.error:
        body["error"] = result.error
    return body


def make_agent(settings: Settings, transport: Callable[[Op], OpResult],
               ask_user: Callable[[str, list[str], float], str | None]
               | None = None,
               write_script: Callable[[str, str], OpResult] | None = None,
               model: Model | None = None) -> Agent:
    """Wire an Agent from settings; `model` is injectable for tests."""
    caps = build_capabilities(settings.facts_path)
    return Agent(model or settings.model_port(), caps, transport, ask_user,
                 write_script)
