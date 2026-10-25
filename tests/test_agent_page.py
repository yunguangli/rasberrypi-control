"""Headless tests for the Agent page: the controller, not the rendering.

`tests/test_main.py` drives the real Flutter host, which needs a desktop
toolchain. These tests build `App` against a stub `Page` instead, so the
wiring that only exists at runtime (worker thread, event queue, ask_user
handshake, settings dialog) is covered on any machine.

`src/main.py` calls `ft.run(main)` at import time; we stub it out for the
import and restore it immediately so the flet test plugin is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import types

import pytest

import flet as ft

_real_run = ft.run
ft.run = lambda *args, **kwargs: None  # no Flutter host for a headless import
try:
    import agent as agent_mod
    import main as gui
finally:
    ft.run = _real_run

from agent import AssistantMessage, ToolCall  # noqa: E402
from protocol import Op, OpResult, PinState  # noqa: E402


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class StubPage:
    """The slice of `ft.Page` the App touches, without a session."""

    def __init__(self, width: float = 1200) -> None:
        self.title = ""
        self.padding = 0
        self.bgcolor = None
        self.theme_mode = None
        self.theme = None
        self.on_resize = None
        self.on_disconnect = None
        self.on_close = None
        self.width = width
        self.window = types.SimpleNamespace(width=width)
        self.platform = ft.PagePlatform.LINUX
        self.controls: list = []
        self.dialogs: list = []
        self.updates = 0
        self.tasks: list = []

    def update(self) -> None:
        self.updates += 1

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def run_task(self, handler, *args, **kwargs):
        result = handler(*args, **kwargs) if callable(handler) else handler
        task = asyncio.ensure_future(result)
        self.tasks.append(task)
        return task

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None


class FakeModel:
    def __init__(self, replies: list[AssistantMessage]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


class RecordingTransport:
    def __init__(self) -> None:
        self.ops: list[Op] = []

    def __call__(self, op: Op) -> OpResult:
        self.ops.append(op)
        return OpResult(ok=True, req_id=op.req_id, value=op.value)


def tool_call(name: str, args: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


def keys_in(control, found: set[str] | None = None,
            seen: set[int] | None = None) -> set[str]:
    """Every `key=` in a control tree (depth-first, cycle-safe)."""
    found = found if found is not None else set()
    seen = seen if seen is not None else set()
    if id(control) in seen:
        return found
    seen.add(id(control))
    key = getattr(control, "key", None)
    if isinstance(key, str) and key:
        found.add(key)
    for attr in ("controls", "content", "actions", "tabs", "destinations",
                 "leading", "trailing"):
        child = getattr(control, attr, None)
        if isinstance(child, list):
            for item in child:
                if hasattr(item, "key") or hasattr(item, "controls"):
                    keys_in(item, found, seen)
        elif child is not None and hasattr(child, "key"):
            keys_in(child, found, seen)
    return found


def find_by_key(control, key: str):
    """First control in the tree whose `key` matches, or None."""
    if getattr(control, "key", None) == key:
        return control
    for attr in ("controls", "content", "actions", "tabs", "destinations",
                 "leading", "trailing"):
        child = getattr(control, attr, None)
        items = child if isinstance(child, list) else [child]
        for item in items:
            if item is None or not hasattr(item, "key"):
                continue
            found = find_by_key(item, key)
            if found is not None:
                return found
    return None


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def app(tmp_path, monkeypatch):
    # Never read or write the operator's real settings/facts.
    monkeypatch.setattr(gui, "AGENT_SETTINGS_PATH",
                        str(tmp_path / "agent.json"))
    for name in ("PICONTROL_LLM_BASE", "PICONTROL_LLM_MODEL",
                 "PICONTROL_LLM_KEY", "PICONTROL_FACTS_PATH",
                 "PICONTROL_STT_MODEL"):
        monkeypatch.delenv(name, raising=False)

    page = StubPage()
    application = gui.App(page)
    # The Pi is not reachable in tests: keep the poll loop "connected" so
    # the Agent page is usable, without any network call.
    application.client.fetch_state = lambda: (
        list(application.states.values()), None, None)
    application.start()
    await asyncio.sleep(0)
    try:
        yield application
    finally:
        application._session_lost = True
        for task in page.tasks:
            task.cancel()
        await asyncio.sleep(0)


def install_agent(app, model, transport=None) -> RecordingTransport:
    transport = transport or RecordingTransport()
    app.agent_settings = agent_mod.Settings(
        base_url="http://localhost:1/v1", model="fake/model",
        facts_path=app.agent_settings.facts_path)
    app.agent = agent_mod.Agent(
        model, app._capabilities(), transport,
        ask_user=app._ask_user,
        write_script=app._write_script_from_agent)
    app._apply_agent()
    return transport


async def send(app, text: str) -> None:
    app.agent_prompt.value = text
    await app._agent_send()
    await wait_for(lambda: not app.agent_busy)


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


async def test_agent_is_the_fourth_page(app):
    assert app.tabs is not None and app.tabs.length == 4
    assert set(app.page_controls) == {gui.App.PAGE_BOARD, gui.App.PAGE_CONTROL,
                                      gui.App.PAGE_SCRIPT, gui.App.PAGE_AGENT}
    for ref in (app.agent_list, app.agent_prompt, app.agent_send,
                app.agent_stop, app.agent_status, app.agent_state,
                app.agent_devices):
        assert ref is not None
    keys = keys_in(app.shell)
    for expected in ("tab-agent", "agent-prompt", "agent-send", "agent-stop",
                     "agent-settings", "agent-clear", "agent-transcript",
                     "agent-state"):
        assert expected in keys, expected


async def test_send_is_gated_on_a_configured_model_and_the_pi(app):
    assert not app.agent_settings.ready
    app.connected = True
    app._apply_agent()
    assert app.agent_send.disabled
    assert app.agent_status.value == "no model configured"

    app.agent_settings = agent_mod.Settings(
        base_url="http://localhost:1/v1", model="fake/model")
    app._apply_agent()
    assert not app.agent_send.disabled
    assert app.agent_status.value.startswith("ready")


async def test_narrow_layout_keeps_the_agent_page(app):
    app.narrow = True
    app.page_index = gui.App.PAGE_AGENT
    app._render()
    assert app.nav is not None and len(app.nav.destinations) == 4
    assert app.body.content is app.page_controls[gui.App.PAGE_AGENT]
    assert "agent-prompt" in keys_in(app.shell)


async def test_motor_slider_spans_reverse_to_forward(app):
    """`tests/test_main.py` cannot read widget properties (Finder has none)."""
    assert (app.sliders[13].min, app.sliders[13].max) == (-100, 100)
    assert (app.sliders[18].min, app.sliders[18].max) == (0, 100)


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------


async def test_turn_runs_one_action_and_renders_chips(app):
    app.connected = True
    app.states = {17: PinState(bcm=17, direction="out", value=False)}
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("status_led",
                                               {"action": "on"}),)),
        AssistantMessage(content="Status LED is on."),
    ])
    transport = install_agent(app, model)

    await send(app, "turn on the status LED")

    assert transport.ops == [Op(op="digital_write", pin=17, value=True,
                                req_id=1)]
    assert [row["kind"] for row in app.agent_rows] == [
        "user", "action", "assistant"]
    assert app.agent_rows[1]["ok"] is True
    assert len(app.agent_list.controls) == 3
    assert app.agent_prompt.value == ""
    # The action also reaches the activity log.
    assert any("agent: status_led.on" in line for line, _ in app.log)


async def test_a_tool_error_becomes_a_red_chip_and_still_replies(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call(
            "status_led", {"action": "tone", "hz": 440}),)),
        AssistantMessage(content="The LED cannot play tones."),
    ])
    transport = install_agent(app, model)

    await send(app, "beep the LED")

    assert transport.ops == []
    assert app.agent_rows[1]["ok"] is False
    assert "not available on status_led" in app.agent_rows[1]["text"]
    assert app.agent_rows[-1]["text"] == "The LED cannot play tones."


async def test_ask_user_question_can_be_answered_from_the_composer(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("ask_user", {
            "question": "Which LED?",
            "options": ["Status LED", "RGB LED"]}),)),
        AssistantMessage(content="Turning on the Status LED."),
    ])
    install_agent(app, model)

    app.agent_prompt.value = "turn on the LED"
    await app._agent_send()
    await wait_for(lambda: app.pending_question is not None
                   and any(r["kind"] == "question" for r in app.agent_rows))

    question = next(r for r in app.agent_rows if r["kind"] == "question")
    assert question["options"] == ["Status LED", "RGB LED"]
    assert question["answer"] is None
    # The composer is how you answer, so Send stays live during the turn.
    assert not app.agent_send.disabled
    assert app.agent_status.value == "waiting for your answer"

    # Typing in the composer while the turn is blocked answers the question.
    app.agent_prompt.value = "Status LED"
    await app._agent_send()
    await wait_for(lambda: not app.agent_busy)

    assert question["answer"] == "Status LED"
    assert app.agent_rows[-1]["text"] == "Turning on the Status LED."
    assert not app.agent_send.disabled
    assert app.agent_status.value.startswith("ready")


async def test_stop_cancels_a_turn_blocked_on_a_question(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("ask_user", {
            "question": "Which LED?"}),)),
        AssistantMessage(content="should not be reached"),
    ])
    install_agent(app, model)

    app.agent_prompt.value = "turn on the LED"
    await app._agent_send()
    await wait_for(lambda: app.pending_question is not None)

    app._agent_stop()
    await wait_for(lambda: not app.agent_busy)

    assert app.pending_question is None
    assert app.agent_rows[-1]["kind"] == "error"
    assert any(row["kind"] == "error" and row["text"] == "stopped"
               for row in app.agent_rows)
    question = next(r for r in app.agent_rows if r["kind"] == "question")
    assert question["answer"] == "(stopped)"


async def test_wire_panel_shows_the_exact_op_and_toggles(app):
    app.connected = True
    app.client.post_op = lambda op: OpResult(
        ok=True, req_id=op.req_id, value=op.value)
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("status_led",
                                               {"action": "on"}),)),
        AssistantMessage(content="on"),
    ])
    app.agent_settings = agent_mod.Settings(
        base_url="http://localhost:1/v1", model="fake/model",
        facts_path=app.agent_settings.facts_path)
    app.agent = agent_mod.Agent(
        model, app._capabilities(), app._agent_transport,
        ask_user=app._ask_user,
        write_script=app._write_script_from_agent)
    app._apply_agent()

    await send(app, "turn on the status LED")

    wire = "\n".join(app.agent_wire)
    assert '"op":"digital_write"' in wire
    assert '"pin":17' in wire
    assert '"value":true' in wire
    assert '"ok":true' in wire
    assert app.agent_wire_text.value == wire

    # Hidden by default; the cable icon in the toolbar toggles it.
    assert app.agent_wire_panel.visible is False
    toggle = find_by_key(app.shell, "agent-wire-toggle")
    assert toggle is not None
    toggle.on_click(None)
    assert app.agent_wire_panel.visible is True
    assert app.agent_wire_toggle.selected is True
    toggle.on_click(None)
    assert app.agent_wire_panel.visible is False

    app._clear_agent()
    assert app.agent_wire == []
    assert "no agent traffic" in app.agent_wire_text.value


async def test_clear_empties_the_transcript_and_history(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("status_led",
                                               {"action": "on"}),)),
        AssistantMessage(content="on"),
    ])
    install_agent(app, model)
    await send(app, "on")

    app._clear_agent()

    assert app.agent_rows == []
    assert len(app.agent_list.controls) == 1      # the empty state
    assert app.agent.history == []


async def test_settings_can_change_while_a_turn_is_blocked_on_a_question(app):
    """Saving settings sets `self.agent = None`; the running turn must finish."""
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("ask_user", {
            "question": "Which LED?"}),)),
        AssistantMessage(content="done"),
    ])
    install_agent(app, model)

    app.agent_prompt.value = "go"
    await app._agent_send()
    await wait_for(lambda: app.pending_question is not None)

    app._open_agent_settings()
    dialog = app.page.dialogs[-1]
    fields = {c.key: c for c in dialog.content.controls
              if getattr(c, "key", None)}
    fields["agent-model"].value = "other/model"
    dialog.actions[-1].on_click(None)
    assert app.agent is None

    app._answer_question("ok")
    await wait_for(lambda: not app.agent_busy)
    assert app.agent_rows[-1]["text"] == "done"
    assert app.agent_settings.model == "other/model"


async def test_clear_is_ignored_while_a_turn_runs(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("ask_user", {
            "question": "Which LED?"}),)),
        AssistantMessage(content="done"),
    ])
    install_agent(app, model)

    app.agent_prompt.value = "go"
    await app._agent_send()
    await wait_for(lambda: app.pending_question is not None)
    before = list(app.agent_rows)

    app._clear_agent()
    assert app.agent_rows == before

    app._answer_question("ok")
    await wait_for(lambda: not app.agent_busy)


async def test_learned_facts_persist_next_to_the_settings_by_default(app,
                                                                   tmp_path):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("remember", {
            "key": "lamp", "value": "status_led"}, "m1"),)),
        AssistantMessage(content="Got it."),
    ])
    install_agent(app, model)

    await send(app, "call the status LED the lamp")

    saved = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    assert saved == {"lamp": "status_led"}
    assert stat.S_IMODE(os.stat(tmp_path / "facts.json").st_mode) == 0o600
    assert app.agent_rows[-2]["kind"] == "memory"


# ---------------------------------------------------------------------------
# script escape hatch
# ---------------------------------------------------------------------------


async def test_write_script_offers_a_card_without_running_anything(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("write_script", {
            "code": "print('hi')", "title": "hello"}),)),
        AssistantMessage(content="Ready when you open it."),
    ])
    transport = install_agent(app, model)

    await send(app, "write a script")

    assert transport.ops == []                   # nothing was executed
    assert app.script_code == ""                 # the editor is untouched
    script_row = next(r for r in app.agent_rows if r["kind"] == "script")
    assert script_row["title"] == "hello"
    assert script_row["code"] == "print('hi')"
    assert any("script 'hello' ready" in line for line, _ in app.log)

    # The card's button is the only thing that loads the editor, on the UI thread.
    button = find_by_key(app.agent_list, "agent-open-script")
    assert button is not None
    button.on_click(None)
    assert app.script_code == "print('hi')"
    assert app.script_text_field.value == "print('hi')"
    assert app.page_index == gui.App.PAGE_SCRIPT


async def test_open_in_script_puts_the_code_back_in_the_editor(app):
    app.connected = True
    model = FakeModel([
        AssistantMessage(tool_calls=(tool_call("write_script", {
            "code": "x = 1", "title": "one"}),)),
        AssistantMessage(content="ok"),
    ])
    install_agent(app, model)
    await send(app, "write it")

    app._clear_agent()
    assert app.script_code == ""

    app._open_script("x = 1")
    assert app.script_code == "x = 1"
    assert app.script_text_field.value == "x = 1"
    assert app.page_index == gui.App.PAGE_SCRIPT


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


async def test_settings_dialog_saves_and_rebuilds_the_agent(app, tmp_path):
    app._open_agent_settings()
    dialog = app.page.dialogs[-1]
    fields = {c.key: c for c in dialog.content.controls
              if getattr(c, "key", None)}
    base_field = fields["agent-base-url"]
    # Typing a wrong path shows the exact endpoint that would be called.
    base_field.value = "https://openrouter.ai/api/vi"
    base_field.on_change(None)
    assert fields["agent-endpoint-preview"].value == (
        "POST https://openrouter.ai/api/vi/chat/completions")
    base_field.value = "https://openrouter.ai/api/v1"
    base_field.on_change(None)
    assert fields["agent-endpoint-preview"].value == (
        "POST https://openrouter.ai/api/v1/chat/completions")
    fields["agent-model"].value = "vendor/model"
    fields["agent-api-key"].value = "sk-test"

    dialog.actions[-1].on_click(None)              # the Save button

    saved = json.loads((tmp_path / "agent.json").read_text(encoding="utf-8"))
    assert saved["base_url"] == "https://openrouter.ai/api/v1"
    assert saved["model"] == "vendor/model"
    assert saved["api_key"] == "sk-test"
    mode = stat.S_IMODE(os.stat(tmp_path / "agent.json").st_mode)
    assert mode == 0o600
    assert app.agent is None                       # rebuilt on next send
    assert app.agent_status.value.startswith("ready")
    assert "openrouter.ai/api/v1" in app.agent_status.value


async def test_cancelling_the_settings_dialog_changes_nothing(app, tmp_path):
    before = app.agent_settings.to_dict()
    app._open_agent_settings()
    dialog = app.page.dialogs[-1]
    dialog.actions[0].on_click(None)               # the Cancel button

    assert app.agent_settings.to_dict() == before
    assert not (tmp_path / "agent.json").exists()


async def test_settings_presets_fill_the_exact_url(app):
    app._open_agent_settings()
    dialog = app.page.dialogs[-1]
    fields = {c.key: c for c in dialog.content.controls
              if getattr(c, "key", None)}

    ollama = find_by_key(dialog, "agent-preset-ollama")
    openrouter = find_by_key(dialog, "agent-preset-openrouter")
    assert ollama is not None and openrouter is not None

    ollama.on_click(None)
    assert fields["agent-base-url"].value == "http://localhost:11434/v1"
    assert fields["agent-endpoint-preview"].value == (
        "POST http://localhost:11434/v1/chat/completions")

    openrouter.on_click(None)
    assert fields["agent-base-url"].value == "https://openrouter.ai/api/v1"
    assert fields["agent-endpoint-preview"].value == (
        "POST https://openrouter.ai/api/v1/chat/completions")


async def test_saving_a_lookalike_url_stores_the_fixed_one(app, tmp_path):
    """The exact `/api/vl` mistake: the digit 1 looks like a letter l."""
    app.connected = True
    app._open_agent_settings()
    dialog = app.page.dialogs[-1]
    fields = {c.key: c for c in dialog.content.controls
              if getattr(c, "key", None)}
    fields["agent-base-url"].value = "https://openrouter.ai/api/vl"
    fields["agent-model"].value = "openrouter/free"

    dialog.actions[-1].on_click(None)              # the Save button

    saved = json.loads((tmp_path / "agent.json").read_text(encoding="utf-8"))
    assert saved["base_url"] == "https://openrouter.ai/api/v1"
    assert "openrouter.ai/api/v1" in app.agent_status.value
