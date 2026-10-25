"""Agent tests: tool schema, validation, the loop, and the HTTP adapter.

No network: the loop is driven by a fake `Model` port and a recording
transport. One contract test spins a localhost server to pin the wire format
the real adapter must produce.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import pins
from agent import (
    Agent,
    AssistantMessage,
    ModelError,
    OpenAIChat,
    Settings,
    ToolCall,
    make_agent,
    normalise_base_url,
)
from capabilities import (
    Capabilities,
    Facts,
    Pause,
    ToolCompiler,
    ToolError,
    actions_for,
    build_capabilities,
    tool_name,
)
from protocol import Op, OpResult, PinState

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class RecordingTransport:
    """Stands in for PiClient.post_op: records Ops, replies from a script."""

    def __init__(self, replies: list[OpResult] | None = None) -> None:
        self.ops: list[Op] = []
        self._replies = list(replies or [])

    def __call__(self, op: Op) -> OpResult:
        self.ops.append(op)
        if self._replies:
            reply = self._replies.pop(0)
            return OpResult(ok=reply.ok, req_id=op.req_id,
                            value=reply.value, error=reply.error)
        return OpResult(ok=True, req_id=op.req_id, value=op.value)


class FakeModel:
    """Model port: returns queued replies, records the messages it saw."""

    def __init__(self, replies: list[AssistantMessage]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise ModelError("FakeModel ran out of replies")
        return self.replies.pop(0)


def call(name: str, args: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


@pytest.fixture()
def caps(tmp_path) -> Capabilities:
    return build_capabilities(str(tmp_path / "facts.json"))


@pytest.fixture()
def compiler(caps) -> ToolCompiler:
    return ToolCompiler(caps)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_every_wired_device_with_actions_has_a_tool(caps):
    names = {t.name for t in caps.tools}
    for profile in pins.PROFILE.values():
        if actions_for(profile):
            assert tool_name(profile) in names
    assert "light_adc" in names          # "Light (ADC)" sanitised
    assert "cpu_temp" in names


def test_tool_actions_match_the_device(caps):
    assert set(caps.by_name["status_led"].actions) == {"on", "off", "pulse"}
    assert set(caps.by_name["buzzer"].actions) == {"on", "off", "pulse", "tone"}
    assert set(caps.by_name["rgb_led"].actions) == {"on", "off", "color"}
    assert set(caps.by_name["desk_dimmer"].actions) == {"set"}
    assert set(caps.by_name["motor"].actions) == {"drive"}
    assert set(caps.by_name["door_button"].actions) == {"read"}
    assert set(caps.by_name["room_temp"].actions) == {"read"}
    assert "relay_1" in caps.by_name


def test_schema_only_advertises_relevant_params(caps):
    led = caps.by_name["status_led"].schema()["function"]["parameters"]
    assert set(led["properties"]) == {"action", "ms"}
    dimmer = caps.by_name["desk_dimmer"].schema()["function"]["parameters"]
    assert set(dimmer["properties"]) == {"action", "percent"}
    buzzer = caps.by_name["buzzer"].schema()["function"]["parameters"]
    assert set(buzzer["properties"]) == {"action", "ms", "hz"}
    motor = caps.by_name["motor"].schema()["function"]["parameters"]
    assert motor["properties"]["percent"]["minimum"] == -100


def test_global_tools_are_present(caps):
    names = [t["function"]["name"] for t in caps.tool_schema]
    for expected in ("all_off", "blink", "run_sequence", "ask_user", "remember",
                     "write_script"):
        assert expected in names


def test_system_prompt_lists_devices_state_and_facts(caps):
    state = [PinState(bcm=17, direction="out", value=True),
             PinState(bcm=4, direction="sensor", value=22.3, unit="°C")]
    caps.facts.remember("led", "status_led")
    prompt = caps.system_prompt(state)
    assert "status_led" in prompt
    assert "Status LED (BCM 17): on" in prompt
    assert "Room temp (BCM 4): 22.3 °C" in prompt
    assert "led = \"status_led\"" in prompt


# ---------------------------------------------------------------------------
# compiler
# ---------------------------------------------------------------------------


def test_compile_on_off(compiler):
    assert compiler.compile("status_led", "on").op == Op(
        op="digital_write", pin=17, value=True, req_id=0)
    assert compiler.compile("status_led", "off").op == Op(
        op="digital_write", pin=17, value=False, req_id=0)


def test_compile_pulse_default_and_explicit(compiler):
    assert compiler.compile("status_led", "pulse").op.value == 300
    assert compiler.compile("status_led", "pulse", {"ms": 1200}).op.value == 1200


def test_compile_rejects_action_the_device_lacks(compiler):
    with pytest.raises(ToolError) as err:
        compiler.compile("status_led", "tone", {"hz": 440})
    assert "not available on status_led" in str(err.value)
    assert "on, off, pulse" in str(err.value)


def test_compile_rejects_out_of_range(compiler):
    with pytest.raises(ToolError, match="out of range 1..10000"):
        compiler.compile("status_led", "pulse", {"ms": 0})
    with pytest.raises(ToolError, match="200..5000"):
        compiler.compile("buzzer", "tone", {"hz": 9000})
    with pytest.raises(ToolError, match="0..100"):
        compiler.compile("desk_dimmer", "set", {"percent": 140})
    with pytest.raises(ToolError, match="-100..100"):
        compiler.compile("motor", "drive", {"percent": 140})


def test_compile_keeps_sentinel_pins_for_sensors(compiler):
    # CPU temp / load / ADC light are not on the header: negative sentinels.
    assert compiler.compile("cpu_temp", "read").op.pin == -2
    assert compiler.compile("system_load", "read").op.pin == -3
    assert compiler.compile("light_adc", "read").op.pin == -1


def test_compile_sensor_read_uses_read_op(compiler):
    assert compiler.compile("room_temp", "read").op.op == "read"
    assert compiler.compile("door_button", "read").op.op == "digital_read"


def test_compile_rgb_colour(compiler):
    op = compiler.compile("rgb_led", "color", {"rgb": 0x1E88E5}).op
    assert op.op == "rgb_write" and op.value == 0x1E88E5


def test_compile_resolves_learned_alias(caps, compiler):
    caps.facts.remember("led", "status_led")
    assert compiler.compile("led", "on").op.pin == 17


def test_compile_unknown_device_lists_known_ones(compiler):
    with pytest.raises(ToolError, match="unknown device 'fan'"):
        compiler.compile("fan", "on")


def test_sequence_validates_every_step_before_returning(compiler):
    steps = [
        {"device": "status_led", "action": "on"},
        {"device": "status_led", "action": "tone"},  # invalid
    ]
    with pytest.raises(ToolError, match="step 1"):
        compiler.compile_sequence(steps)


def test_sequence_expands_pauses(compiler):
    plan = compiler.compile_sequence([
        {"device": "status_led", "action": "on", "wait_ms": 200},
        {"wait_ms": 50},
        {"device": "status_led", "action": "off"},
    ])
    labels = [getattr(s, "label", "pause") for s in plan]
    assert labels == ["status_led.on", "pause", "pause", "status_led.off"]


def test_sequence_rejects_empty_and_oversized(compiler):
    with pytest.raises(ToolError, match="non-empty"):
        compiler.compile_sequence([])
    with pytest.raises(ToolError, match="at most 64"):
        compiler.compile_sequence([{"wait_ms": 1}] * 65)


def test_compile_blink_expands_on_off_pairs(compiler):
    plan = compiler.compile_blink("status_led", times=2, on_ms=20, off_ms=30)
    labels = [getattr(step, "label", "pause") for step in plan]
    assert labels == ["status_led.on", "pause", "status_led.off", "pause"] * 2
    assert [s.ms for s in plan if isinstance(s, Pause)] == [20, 30, 20, 30]


def test_compile_blink_defaults_and_validation(compiler):
    plan = compiler.compile_blink("status_led")
    assert len(plan) == 12                      # 3 × (on, wait, off, wait)
    assert [s.ms for s in plan if isinstance(s, Pause)][:2] == [300, 300]
    with pytest.raises(ToolError, match="times 0"):
        compiler.compile_blink("status_led", times=0)
    with pytest.raises(ToolError, match="times 21"):
        compiler.compile_blink("status_led", times=21)
    with pytest.raises(ToolError, match="on_ms"):
        compiler.compile_blink("status_led", on_ms=5)
    with pytest.raises(ToolError, match="cannot blink"):
        compiler.compile_blink("door_button")


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------


def test_facts_persist_and_reload(tmp_path):
    path = str(tmp_path / "nested" / "facts.json")
    facts = Facts(path)
    facts.remember("led", "status_led")
    facts.remember("fan", {"bcm": 26, "role": "output"})
    reloaded = Facts(path)
    assert reloaded.resolve("LED") == "status_led"       # case-insensitive
    assert reloaded.resolve("fan") == {"bcm": 26, "role": "output"}


def test_facts_survive_a_corrupt_file(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text("{not json", encoding="utf-8")
    assert Facts(str(path)).data == {}


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def _agent(caps, model, transport, **kwargs) -> Agent:
    return Agent(model, caps, transport, **kwargs)


def test_turn_executes_one_action_then_replies(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("status_led", {"action": "on"}),)),
        AssistantMessage(content="Status LED is on."),
    ])
    transport = RecordingTransport()
    events = []
    turn = _agent(caps, model, transport).turn(
        "turn on the LED", on_event=events.append)

    assert transport.ops == [Op(op="digital_write", pin=17, value=True,
                                req_id=1)]
    assert [e.kind for e in events] == ["thinking", "action", "text"]
    assert turn.reply == "Status LED is on."
    assert not turn.stopped


def test_turn_feeds_tool_errors_back_to_the_model(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("status_led",
                                          {"action": "tone", "hz": 440}),)),
        AssistantMessage(content="The LED cannot play tones."),
    ])
    transport = RecordingTransport()
    turn = _agent(caps, model, transport).turn("beep the LED")

    assert transport.ops == []                       # nothing was sent
    tool_reply = json.loads(model.calls[1][-1]["content"])
    assert tool_reply["ok"] is False
    assert "not available on status_led" in tool_reply["error"]
    assert turn.reply == "The LED cannot play tones."


def test_tool_replies_carry_the_matching_call_id(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(
            call("status_led", {"action": "on"}, "abc123"),)),
        AssistantMessage(content="done"),
    ])
    _agent(caps, model, RecordingTransport()).turn("on")
    tool_message = model.calls[1][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "abc123"
    assistant = model.calls[1][-2]
    assert assistant["tool_calls"][0]["id"] == "abc123"


def test_history_never_orphans_a_tool_message(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("status_led", {"action": "on"},
                                          "first"),)),
        AssistantMessage(content="on"),
        AssistantMessage(tool_calls=(call("status_led", {"action": "off"},
                                          "second"),)),
        AssistantMessage(content="off"),
    ])
    agent = _agent(caps, model, RecordingTransport())
    agent.turn("on")
    agent.turn("off")

    messages = model.calls[2]                    # third request = turn two
    announced = {m["tool_calls"][0]["id"] for m in messages
                 if m.get("tool_calls")}
    replies = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
    assert replies == {"first"}                  # turn one is complete
    assert announced == {"first"}


def test_ask_user_roundtrip(caps):
    asked = {}

    def ask(question, options, timeout):
        asked.update(question=question, options=options, timeout=timeout)
        return "status"

    model = FakeModel([
        AssistantMessage(tool_calls=(call("ask_user", {
            "question": "Which LED?",
            "options": ["Status LED", "RGB LED"]}),)),
        AssistantMessage(tool_calls=(call("remember", {
            "key": "led", "value": "status_led"}, "c2"),)),
        AssistantMessage(tool_calls=(call("status_led", {"action": "on"},
                                          "c3"),)),
        AssistantMessage(content="Status LED on."),
    ])
    transport = RecordingTransport()
    turn = _agent(caps, model, transport, ask_user=ask).turn(
        "turn on the LED")

    assert asked["question"] == "Which LED?"
    assert asked["options"] == ["Status LED", "RGB LED"]
    assert caps.facts.resolve("led") == "status_led"
    assert transport.ops[0].pin == 17
    kinds = [e.kind for e in turn.events]
    assert kinds == ["thinking", "question", "answer", "memory", "action",
                     "text"]


def test_sequence_runs_steps_and_pauses(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("run_sequence", {"steps": [
            {"device": "status_led", "action": "on", "wait_ms": 1},
            {"device": "status_led", "action": "off"},
        ]}),)),
        AssistantMessage(content="blinked"),
    ])
    transport = RecordingTransport()
    turn = _agent(caps, model, transport).turn("blink once")
    assert [op.value for op in transport.ops] == [True, False]
    assert sum(e.kind == "action" for e in turn.events) == 2
    assert turn.reply == "blinked"


def test_sequence_stops_at_the_first_failure(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("run_sequence", {"steps": [
            {"device": "status_led", "action": "on"},
            {"device": "status_led", "action": "off"},
        ]}),)),
        AssistantMessage(content="one step failed"),
    ])
    transport = RecordingTransport([OpResult(ok=False, error="pin busy")])
    turn = _agent(caps, model, transport).turn("blink")
    assert len(transport.ops) == 1
    assert "pin busy" in turn.events[-1].text or "failed" in turn.reply


def test_agent_runs_blink(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("blink", {
            "device": "status_led", "times": 2,
            "on_ms": 20, "off_ms": 20}),)),
        AssistantMessage(content="blinked twice"),
    ])
    transport = RecordingTransport()
    turn = _agent(caps, model, transport).turn("blink twice")

    assert [op.value for op in transport.ops] == [True, False, True, False]
    assert sum(e.kind == "action" for e in turn.events) == 4
    assert turn.reply == "blinked twice"


def test_blink_rejects_an_unknown_device(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("blink", {"device": "fan"}),)),
        AssistantMessage(content="no fan"),
    ])
    transport = RecordingTransport()
    _agent(caps, model, transport).turn("blink the fan")
    assert transport.ops == []
    payload = json.loads(model.calls[1][-1]["content"])
    assert "unknown device 'fan'" in payload["error"]


def test_missing_tool_call_ids_are_synthesised(caps):
    """Some free providers omit the id; OpenRouter rejects that in history."""
    reply = AssistantMessage(
        content="",
        tool_calls=(ToolCall(id="", name="status_led",
                             arguments={"action": "on"}),),
        raw={"role": "assistant", "content": None,
             "tool_calls": [{"type": "function", "id": "",
                             "function": {"name": "status_led",
                                          "arguments": '{"action": "on"}'}}]},
    )
    model = FakeModel([reply, AssistantMessage(content="done")])
    _agent(caps, model, RecordingTransport()).turn("on")

    assistant = model.calls[1][-2]
    tool_message = model.calls[1][-1]
    assert assistant["tool_calls"][0]["id"] == "call_0"
    assert tool_message["tool_call_id"] == "call_0"


def test_malformed_arguments_are_sanitised_and_fed_back(caps):
    """A truncated arguments string must not be echoed to the provider."""
    reply = AssistantMessage(
        content="",
        tool_calls=(ToolCall(id="c1", name="run_sequence", arguments={},
                             parse_error="Expecting value"),),
        raw={"role": "assistant", "content": None,
             "tool_calls": [{"type": "function", "id": "c1",
                             "function": {"name": "run_sequence",
                                          "arguments": '{"steps": ['}}]},
    )
    model = FakeModel([reply, AssistantMessage(content="retry")])
    transport = RecordingTransport()
    turn = _agent(caps, model, transport).turn("blink")

    assert transport.ops == []
    assistant = model.calls[1][-2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    payload = json.loads(model.calls[1][-1]["content"])
    assert payload["ok"] is False
    assert "could not parse" in payload["error"]
    assert turn.reply == "retry"


def test_arguments_reports_invalid_json_and_non_objects():
    from agent import _arguments

    args, error = _arguments({"function": {"arguments": '{"a": 1}'}})
    assert args == {"a": 1} and error == ""
    args, error = _arguments({"function": {"arguments": "{oops"}})
    assert args == {} and error
    args, error = _arguments({"function": {"arguments": "[1, 2]"}})
    assert args == {} and "JSON object" in error


def test_cancel_stops_between_tool_calls(caps):
    agent: Agent = None

    def ask(question, options, timeout):
        agent.cancel()
        return "ok"

    model = FakeModel([
        AssistantMessage(tool_calls=(call("ask_user", {"question": "?"}),)),
        AssistantMessage(content="should not be reached"),
    ])
    agent = _agent(caps, model, RecordingTransport(), ask_user=ask)
    turn = agent.turn("go")
    assert turn.stopped
    assert turn.error == "stopped"


def test_a_cancelled_turn_never_enters_history(caps):
    """The cut can fall between a tool_calls message and its replies."""
    agent: Agent = None

    def ask(question, options, timeout):
        agent.cancel()
        return "ok"

    model = FakeModel([
        AssistantMessage(tool_calls=(call("ask_user", {"question": "?"}),
                                     call("status_led", {"action": "on"},
                                          "c2"))),
        AssistantMessage(content="unreached"),
    ])
    agent = _agent(caps, model, RecordingTransport(), ask_user=ask)
    assert agent.turn("go").stopped
    assert agent.history == []          # half a turn is not remembered

    model.replies = [AssistantMessage(content="fresh")]
    assert agent.turn("again").reply == "fresh"
    messages = model.calls[-1]
    assert not any(m["role"] == "tool" for m in messages)
    assert not any(m.get("tool_calls") for m in messages)


def test_max_steps_is_enforced(caps):
    replies = [AssistantMessage(tool_calls=(
        call("status_led", {"action": "on"}),))] * 20
    model = FakeModel(replies)
    transport = RecordingTransport()
    turn = _agent(caps, model, transport, max_steps=3).turn("on forever")
    assert turn.stopped
    assert "budget" in turn.error
    assert len(transport.ops) == 3


def test_write_script_without_a_runner_is_a_tool_error(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("write_script", {
            "code": "print(1)"}),)),
        AssistantMessage(content="cannot"),
    ])
    turn = _agent(caps, model, RecordingTransport()).turn("write a script")
    payload = json.loads(model.calls[1][-1]["content"])
    assert payload["ok"] is False
    assert "disabled" in payload["error"]


def test_write_script_emits_the_code_for_the_editor(caps):
    model = FakeModel([
        AssistantMessage(tool_calls=(call("write_script", {
            "code": "print('hi')", "title": "hello"}),)),
        AssistantMessage(content="ready"),
    ])
    seen = []
    agent = _agent(caps, model, RecordingTransport(),
                   write_script=lambda code, title: (
                       seen.append((code, title)) or OpResult(ok=True)))
    turn = agent.turn("write it")
    assert seen == [("print('hi')", "hello")]
    script_events = [e for e in turn.events if e.kind == "script"]
    assert script_events[0].extra["code"] == "print('hi')"


def test_model_failure_stops_the_turn(caps):
    model = FakeModel([])                      # raises ModelError immediately
    turn = _agent(caps, model, RecordingTransport()).turn("hi")
    assert turn.stopped
    assert "ran out of replies" in turn.error


def test_model_length_exhaustion_is_reported(caps):
    """A reasoning model can spend the whole budget and return nothing."""
    model = FakeModel([AssistantMessage(content="", finish_reason="length")])
    turn = _agent(caps, model, RecordingTransport()).turn("play a song")
    assert turn.stopped
    assert "output tokens" in turn.error


def test_empty_reply_is_reported(caps):
    model = FakeModel([AssistantMessage(content="", finish_reason="stop")])
    turn = _agent(caps, model, RecordingTransport()).turn("hi")
    assert turn.stopped
    assert "empty reply" in turn.error


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_roundtrip(tmp_path):
    path = str(tmp_path / "agent.json")
    settings = Settings(base_url="https://openrouter.ai/api/v1",
                        model="vendor/model", api_key="sk-test")
    settings.save(path)
    loaded = Settings.load(path)
    assert loaded.base_url == "https://openrouter.ai/api/v1"
    assert loaded.model == "vendor/model"
    assert loaded.api_key == "sk-test"
    assert loaded.ready
    # The status label keeps the path, so /api/vi typos are visible.
    assert loaded.label == "vendor/model @ openrouter.ai/api/v1"


def test_settings_without_a_model_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.delenv("PICONTROL_LLM_MODEL", raising=False)
    settings = Settings(base_url="http://localhost:11434/v1", model="")
    assert not settings.ready
    assert "no model" in settings.label


@pytest.mark.parametrize("raw", [
    "https://openrouter.ai/api/vi",   # letter i
    "https://openrouter.ai/api/vl",   # letter l
    "https://openrouter.ai/api/vI",
    "https://openrouter.ai/api/vL",
    "https://openrouter.ai/api/v1/",
])
def test_base_url_lookalikes_are_repaired(raw):
    assert normalise_base_url(raw) == "https://openrouter.ai/api/v1"


def test_base_url_keeps_other_paths_and_strips_whitespace():
    assert normalise_base_url("  http://localhost:11434/v1/  ") == (
        "http://localhost:11434/v1")
    assert normalise_base_url("https://example.com/openai") == (
        "https://example.com/openai")


def test_settings_applies_the_repair():
    settings = Settings(base_url="https://openrouter.ai/api/vl", model="m")
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.label.endswith("openrouter.ai/api/v1")


def test_settings_strip_pasted_whitespace():
    settings = Settings(base_url=" http://localhost:11434/v1 ",
                        model="  openrouter/free \n",
                        api_key="  sk-test  ")
    assert settings.base_url == "http://localhost:11434/v1"
    assert settings.model == "openrouter/free"
    assert settings.api_key == "sk-test"


def test_turn_uses_the_injected_model_port(caps):
    """Layering check: the Agent talks to the Model port, not to Settings.

    "no model configured" is a UI state (the page disables Send), so the
    loop itself has no knowledge of base URLs or keys.
    """
    settings = Settings(base_url="http://localhost:11434/v1", model="")
    model = FakeModel([AssistantMessage(content="hi")])
    agent = make_agent(settings, RecordingTransport(), model=model)
    assert agent.turn("hello").reply == "hi"


# ---------------------------------------------------------------------------
# HTTP adapter contract
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen["path"] = self.path
        type(self).seen["body"] = body
        type(self).seen["auth"] = self.headers.get("Authorization")
        reply = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "status_led",
                                     "arguments": '{"action": "on"}'},
                    }],
                },
            }]
        }
        data = json.dumps(reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence the test output
        pass


def test_openai_adapter_sends_tools_and_parses_tool_calls():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        chat = OpenAIChat(f"http://127.0.0.1:{server.server_address[1]}/v1",
                          "test/model", "sk-abc")
        reply = chat.complete([{"role": "user", "content": "on"}],
                              [{"type": "function",
                                "function": {"name": "status_led"}}])
    finally:
        server.shutdown()
        server.server_close()

    assert _Handler.seen["path"] == "/v1/chat/completions"
    assert _Handler.seen["auth"] == "Bearer sk-abc"
    assert _Handler.seen["body"]["model"] == "test/model"
    assert _Handler.seen["body"]["tools"][0]["function"]["name"] == "status_led"
    assert reply.content == ""
    assert reply.tool_calls == (ToolCall("call_1", "status_led",
                                         {"action": "on"}),)
    assert reply.finish_reason == "tool_calls"


def test_openai_adapter_reports_http_errors():
    class Failing(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Failing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        chat = OpenAIChat(f"http://127.0.0.1:{server.server_address[1]}/v1",
                          "m", "")
        with pytest.raises(ModelError) as err:
            chat.complete([], [])
        # The endpoint is in the message: a wrong base URL is diagnosable.
        assert "HTTP 401" in str(err.value)
        assert "/v1/chat/completions" in str(err.value)
    finally:
        server.shutdown()
        server.server_close()
