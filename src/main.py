"""picontrol GUI — the client app (Flet 1.0), phone-portrait-first.

MVC-ish split in a single file; this is the only module allowed to import
flet (boundary enforced by tests/test_import_boundary.py):

  PiClient — model/transport: blocking stdlib HTTP against pi.py. The
             controller wraps every call in asyncio.to_thread.
  App      — controller: poll loop, optimistic updates, activity log,
             responsive re-render at NARROW_BREAKPOINT, portrait lock.
  _render* — view: cards and the Board strip are generated from
             pins.HEADER / pins.PROFILE, so adding a device to pins.py
             adds its card automatically.

Layout (plan.md §6): wide screens keep the horizontal 2x20 Board strip and
multi-column cards; narrow screens (phone portrait) switch to a 20-row
vertical pinout and wrap to a single column. Portrait is locked on
Android/iOS via set_allowed_device_orientations.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request

import flet as ft

import pins
import protocol
from pins import FunctionColor, PinProfile, Role
from protocol import Op, OpResult, PinState, ScriptStatus

NARROW_BREAKPOINT = 600          # px: below this we render portrait layout
POLL_OK_SECONDS = 1.5            # healthy poll cadence
POLL_FAIL_SECONDS = 3.0          # slower retry while unreachable
REQUEST_TIMEOUT = 3.0            # per-request urllib timeout
LOG_LINES = 16                   # activity log depth
PULSE_MS = 300                   # click-to-pulse duration for the buzzer
SCRIPT_INDENT = 4                # spaces per Tab press in the script box

# Quick-pick targets for the dropdown under the host field: this machine
# (where `pi.py --dev` runs) and the Pi on the LAN.
TARGET_PRESETS: tuple[str, ...] = (
    f"127.0.0.1:{protocol.DEFAULT_PORT}",
    f"192.168.1.118:{protocol.DEFAULT_PORT}",
)

# RGB LED preset swatches (avoid the ColorPicker: it needs an extra package).
_RGB_SWATCHES: tuple[tuple[str, int], ...] = (
    ("off", 0x000000),
    ("red", 0xE53935),
    ("orange", 0xFF9800),
    ("yellow", 0xFBC02D),
    ("green", 0x43A047),
    ("cyan", 0x00BCD4),
    ("blue", 0x1E88E5),
    ("purple", 0x8E24AA),
    ("white", 0xFFFFFF),
)

# Musical notes for the TonalBuzzer card (all inside its default 220-880 Hz).
_TONES: tuple[tuple[str, int], ...] = (
    ("C4", 262), ("D4", 294), ("E4", 330),
    ("G4", 392), ("A4", 440), ("C5", 523),
)

# Board strip: intrinsic function-family ring colours (pinout-style palette).
_FUNCTION_COLORS: dict[FunctionColor, str] = {
    FunctionColor.GPIO: "#43A047",
    FunctionColor.POWER: "#E53935",
    FunctionColor.GND: "#607D8B",
    FunctionColor.UART: "#8E24AA",
    FunctionColor.SPI: "#1E88E5",
    FunctionColor.I2C: "#D81B60",
    FunctionColor.ID: "#FBC02D",
}

_CARD_BG = "#F5F7FA"
_CARD_TEXT = "#263238"
_CARD_SUB = "#78909C"
_ACCENT = "#1E88E5"


def _describe(op: Op) -> str:
    """Human-readable one-liner for the activity log."""
    if op.op in protocol.SYSTEM_OPS:
        return op.op
    text = f"{op.op} pin {op.pin}"
    if op.value is not None:
        text += f" = {op.value}"
    return text


class PiClient:
    """Blocking HTTP client (stdlib urllib only).

    Never raises: connection problems come back as structured errors so the
    GUI can render them and keep polling. App calls these from threads.
    """

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = protocol.DEFAULT_PORT
        self._req_id = 0

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, path: str, body: bytes, req_id: int = 0) -> OpResult:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request,
                                         timeout=REQUEST_TIMEOUT) as resp:
                return protocol.parse_result(resp.read())
        except urllib.error.HTTPError as ex:
            # pi.py answers 400/404 with a structured OpResult body.
            try:
                return protocol.parse_result(ex.read())
            except protocol.ProtocolError:
                return OpResult(ok=False, req_id=req_id,
                                error=f"HTTP {ex.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            reason = getattr(ex, "reason", ex)
            return OpResult(ok=False, req_id=req_id,
                            error=f"unreachable: {reason}")
        except Exception as ex:  # client boundary: never crash the GUI loop
            return OpResult(ok=False, req_id=req_id,
                            error=f"{type(ex).__name__}: {ex}")

    def post_op(self, op: Op) -> OpResult:
        return self._post("/op", protocol.encode_op(op), op.req_id)

    def post_script(self, code: str, req_id: int = 0) -> OpResult:
        return self._post("/script", protocol.encode_script(code, req_id),
                          req_id)

    def post_script_stop(self, req_id: int = 0) -> OpResult:
        return self._post("/script/stop", b"{}", req_id)

    def fetch_state(self) -> tuple[list[PinState] | None,
                                   ScriptStatus | None, str | None]:
        """Returns (states, script, None) on success or (None, None, reason)."""
        try:
            with urllib.request.urlopen(f"{self.base}/state",
                                        timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read()
            return (protocol.parse_state(body),
                    protocol.parse_script_status(body), None)
        except urllib.error.HTTPError as ex:
            return None, None, f"HTTP {ex.code}"
        except protocol.ProtocolError as ex:
            return None, None, f"bad response: {ex}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            return None, None, f"unreachable: {getattr(ex, 'reason', ex)}"
        except Exception as ex:  # client boundary: never crash the poll loop
            return None, None, f"{type(ex).__name__}: {ex}"


def _badge_for_state(state: PinState) -> tuple[str, str, str]:
    """(text, color, background) for a Board cell once state is known."""
    if state.fault:
        return "ERR", "#FFFFFF", "#E53935"
    if state.direction == "out":
        if state.value:
            return "ON", "#1B5E20", "#C8E6C9"
        return "OFF", "#0D47A1", "#E3F2FD"
    if state.direction == "in":
        if state.value:
            return "ACT", "#F57F17", "#FFF9C4"
        return "idle", "#546E7A", "#ECEFF1"
    if state.direction == "pwm":
        return f"{float(state.value):.0f}%", "#0D47A1", "#E3F2FD"
    return f"{state.value}{state.unit}", "#4A148C", "#F3E5F5"


class App:
    """Controller + view builder for the picontrol GUI."""

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.client = PiClient()
        self.narrow = False
        self.connected = False
        self.error: str | None = None
        self.latency_ms: float | None = None
        self.states: dict[int, PinState] = {}
        self.script: ScriptStatus | None = None
        self.script_code: str = ""            # survives re-renders
        self.script_caret: int = 0           # caret index for the Tab handler
        self.script_span: tuple[int, int] = (0, 0)  # selected range, if any
        self.pending: set[int] = set()      # BCMs with an op in flight
        self.board_open = True
        self.log: list[tuple[str, bool]] = []
        self.shell: ft.Column | None = None
        # Widget refs rebuilt on every _render():
        self.dot: ft.Container | None = None
        self.conn_text: ft.Text | None = None
        self.host_field: ft.TextField | None = None
        self.preset_dropdown: ft.Dropdown | None = None
        self.log_column: ft.Column | None = None
        self.toggles: list[ft.Control] = []
        self.switches: dict[int, ft.Switch] = {}
        self.sliders: dict[int, ft.Slider] = {}
        self.slider_labels: dict[int, ft.Text] = {}
        self.input_texts: dict[int, ft.Text] = {}
        self.input_dots: dict[int, ft.Container] = {}
        self.sensor_values: dict[int, ft.Text] = {}
        self.sensor_notes: dict[int, ft.Text] = {}
        self.cell_bgs: dict[int, ft.Container] = {}
        self.cell_badges: dict[int, ft.Text] = {}
        self.script_field: ft.TextField | None = None
        self.script_clear: ft.Button | None = None
        self.script_indent: ft.Button | None = None
        self.script_outdent: ft.Button | None = None
        self.script_run: ft.Button | None = None
        self.script_stop: ft.Button | None = None
        self.script_status: ft.Text | None = None
        self.script_output: ft.Text | None = None

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> None:
        self.page.title = "picontrol"
        self.page.padding = 8
        self.page.on_resize = self._on_resize
        self.narrow = self._initial_narrow()
        self._render()
        self.page.run_task(self._lock_portrait)
        self.page.run_task(self._poll_loop)

    def _initial_narrow(self) -> bool:
        if self.page.platform in (ft.PagePlatform.ANDROID,
                                  ft.PagePlatform.IOS):
            return True
        try:
            width = float(self.page.window.width or 0)
        except Exception:
            width = 0.0
        return 0 < width < NARROW_BREAKPOINT

    async def _lock_portrait(self) -> None:
        if self.page.platform in (ft.PagePlatform.ANDROID,
                                  ft.PagePlatform.IOS):
            try:
                await self.page.set_allowed_device_orientations(
                    [ft.DeviceOrientation.PORTRAIT_UP])
            except Exception:
                pass  # some devices refuse; the layout re-flows anyway

    def _on_resize(self, e: ft.PageResizeEvent) -> None:
        narrow = e.width < NARROW_BREAKPOINT
        if narrow != self.narrow:
            self.narrow = narrow
            self._render()

    def _render(self) -> None:
        """(Re)build every widget; on re-renders swap into the live shell."""
        # Widgets in flight are gone with the old tree — if a slider drag was
        # pending, its on_change_end will never arrive, so drop the marks
        # rather than leaving that BCM stuck (polls would skip it forever).
        self.pending.clear()
        script_box = self.script_text_field
        if script_box is not None and script_box.value is not None:
            self.script_code = script_box.value  # keep the typed script
        self.toggles.clear()
        self.switches.clear()
        self.sliders.clear()
        self.slider_labels.clear()
        self.input_texts.clear()
        self.input_dots.clear()
        self.sensor_values.clear()
        self.sensor_notes.clear()
        self.cell_bgs.clear()
        self.cell_badges.clear()

        column = ft.Column(
            controls=[
                self._connection_bar(),
                self._board_section(),
                self._outputs_section(),
                self._momentary_section(),
                self._inputs_section(),
                self._sensors_section(),
                self._pwm_section(),
                self._script_section(),
                self._log_section(),
            ],
            spacing=14,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )
        if self.shell is None:
            self.shell = column
            self.page.add(column)
        else:
            self.shell.controls = column.controls
        self._apply_states()
        self._apply_connection()
        self.page.update()

    # --------------------------------------------------------- connection ----

    def _connection_bar(self) -> ft.Row:
        self.host_field = ft.TextField(
            value=self.client.base.removeprefix("http://"),
            label="Pi host:port",
            width=190 if self.narrow else 230,
            key="host",
            on_submit=self._on_connect,
        )
        self.preset_dropdown = ft.Dropdown(
            options=[ft.DropdownOption(key=p, text=p) for p in TARGET_PRESETS],
            value=self.client.base.removeprefix("http://"),
            label="quick targets",
            width=190 if self.narrow else 230,
            key="preset",
            dense=True,
            on_select=self._on_preset,
        )
        connect = ft.Button(content="Connect", key="connect",
                            on_click=self._on_connect)
        self.dot = ft.Container(width=12, height=12, border_radius=6,
                                bgcolor="#BDBDBD")
        self.conn_text = ft.Text("waiting for the Pi…", size=12,
                                 color="#546E7A")
        return ft.Row(
            controls=[
                ft.Column(controls=[self.host_field, self.preset_dropdown],
                          spacing=6),
                connect, self.dot, self.conn_text,
            ],
            wrap=True,
            spacing=10,
            run_spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _on_connect(self, e: ft.ControlEvent) -> None:
        self._apply_target(
            (self.host_field.value if self.host_field else "") or "")

    def _on_preset(self, e: ft.ControlEvent) -> None:
        self._apply_target(self.preset_dropdown.value or "")

    def _apply_target(self, raw: str) -> None:
        """Point the client at `host[:port]` and mirror it in the widgets."""
        raw = raw.strip().removeprefix("http://").rstrip("/")
        if ":" in raw:
            host, _, port_text = raw.rpartition(":")
            try:
                self.client.port = int(port_text)
            except ValueError:
                pass
            if host:
                self.client.host = host
        elif raw:
            self.client.host = raw
        if self.host_field is not None:
            self.host_field.value = f"{self.client.host}:{self.client.port}"
        # Keep the quick-pick in sync: preselect an exact preset, clear the
        # selection when a custom address was typed.
        if self.preset_dropdown is not None:
            base = self.client.base.removeprefix("http://")
            self.preset_dropdown.value = base if base in TARGET_PRESETS else None
        self._push_log(f"target -> {self.client.base}", True)
        self.page.update()
        self.page.run_task(self._poll_once)

    def _apply_connection(self) -> None:
        if self.connected:
            color, text = "#43A047", (
                f"connected · {self.latency_ms or 0:.0f} ms")
            text_color = None
        elif self.error:
            color, text, text_color = "#E53935", self.error, "#E53935"
        else:
            color, text, text_color = "#BDBDBD", "waiting for the Pi…", None
        if self.dot is not None:
            self.dot.bgcolor = color
        if self.conn_text is not None:
            self.conn_text.value = text
            self.conn_text.color = text_color
        for control in self.toggles:
            control.disabled = not self.connected

    # ------------------------------------------------------------ polling ----

    async def _poll_loop(self) -> None:
        while True:
            try:
                delay = await self._poll_once()
            except Exception as ex:  # never let one bad poll kill the loop
                delay = POLL_FAIL_SECONDS
                try:
                    self._push_log(
                        f"poll error: {type(ex).__name__}: {ex}", False)
                except Exception:
                    pass  # if the log is broken too, just keep polling
            await asyncio.sleep(delay)

    async def _poll_once(self) -> float:
        was_connected = self.connected
        t0 = time.monotonic()
        states, script, error = await asyncio.to_thread(self.client.fetch_state)
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        if error is not None:
            self.connected = False
            self.error = error
            if was_connected:
                self._push_log(f"lost {self.client.base}: {error}", False)
            self._apply_connection()
            self.page.update()
            return POLL_FAIL_SECONDS

        self.states = {s.bcm: s for s in states}
        self._note_script_transition(script)
        self.script = script
        self.connected = True
        self.error = None
        self.latency_ms = elapsed_ms
        if not was_connected:
            self._push_log(
                f"connected to {self.client.base} "
                f"({elapsed_ms:.0f} ms)", True)
        self._apply_states()
        self._apply_connection()
        self._apply_script()
        self.page.update()
        return POLL_OK_SECONDS

    def _note_script_transition(self, script: ScriptStatus | None) -> None:
        """Log script start/exit edges once, as they arrive via polling."""
        old, self.script = self.script, script
        if script is None or old is None:
            return
        if script.job != old.job and script.job:
            self._push_log(f"script job {script.job} started", True)
        elif script.running != old.running:
            if script.running:
                self._push_log(f"script job {script.job} started", True)
            elif script.exit is not None:
                self._push_log(
                    f"script job {script.job} exited with {script.exit}",
                    script.exit == 0)

    # ------------------------------------------------------------ logging ----

    def _push_log(self, text: str, ok: bool) -> None:
        self.log.insert(0, (text, ok))
        del self.log[LOG_LINES:]
        if self.log_column is not None:
            self.log_column.controls = self._log_texts()

    def _log_texts(self) -> list[ft.Control]:
        if not self.log:
            return [ft.Text("no operations yet", size=11, color="#90A4AE")]
        return [
            ft.Text(text, size=11, max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    color="#43A047" if ok else "#E53935")
            for text, ok in self.log
        ]

    async def _send(self, op: Op) -> OpResult:
        result = await asyncio.to_thread(self.client.post_op, op)
        self._push_log(
            f"#{result.req_id} {_describe(op)} — "
            + ("ok" if result.ok else f"ERR {result.error}"),
            result.ok,
        )
        self.page.update()
        return result

    # ------------------------------------------------------------- states ----

    @staticmethod
    def _sensor_pin_note(profile: PinProfile) -> str:
        """Where a sensor lives: GPIO pins, the ADC bus, or 'on the Pi'."""
        if profile.physical is None:
            return "MCP3008 · SPI0" if profile.driver == "LDR" else "on the Pi"
        text = f"GPIO{profile.bcm}"
        if profile.echo is not None:
            text += f" + GPIO{profile.echo}"
        return text

    def _apply_states(self) -> None:
        for bcm, state in self.states.items():
            profile = pins.PROFILE.get(bcm)
            if profile is None:
                continue
            busy = bcm in self.pending

            sw = self.switches.get(bcm)
            if sw is not None and not busy and state.value is not None:
                sw.value = bool(state.value)

            dot = self.input_dots.get(bcm)
            if dot is not None and state.value is not None:
                active = bool(state.value)
                dot.bgcolor = "#FBC02D" if active else "#BDBDBD"
                label = self.input_texts.get(bcm)
                if label is not None:
                    label.value = "active" if active else "idle"
                    label.color = "#F57F17" if active else "#78909C"

            value_text = self.sensor_values.get(bcm)
            if value_text is not None:
                note = self.sensor_notes.get(bcm)
                if state.fault:
                    value_text.value = "—"
                    if note is not None:
                        note.value = state.fault
                        note.color = "#E53935"
                else:
                    value_text.value = f"{state.value}{state.unit}"
                    if note is not None:
                        note.value = self._sensor_pin_note(profile)
                        note.color = "#78909C"

            slider = self.sliders.get(bcm)
            if slider is not None and not busy and state.value is not None:
                pct = float(state.value)
                slider.value = pct
                label = self.slider_labels.get(bcm)
                if label is not None:
                    label.value = f"{pct:.0f}%"

            self._apply_cell(bcm, state)

    def _apply_cell(self, bcm: int, state: PinState) -> None:
        badge = self.cell_badges.get(bcm)
        bg = self.cell_bgs.get(bcm)
        if badge is None or bg is None:
            return
        text, color, bgcolor = _badge_for_state(state)
        badge.value = text
        badge.color = color
        bg.bgcolor = bgcolor

    # ----------------------------------------------------------- sections ----

    def _section(self, title: str, subtitle: str,
                 cards: list[ft.Control],
                 *trailing: ft.Control) -> ft.Column:
        if not cards:
            return ft.Column(controls=[])
        heading = ft.Row(
            controls=[
                ft.Text(title, size=15, weight=ft.FontWeight.BOLD),
                ft.Text(subtitle, size=11, color="#90A4AE"),
                *trailing,
            ],
            wrap=True,
            spacing=8,
            run_spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        return ft.Column(
            controls=[
                heading,
                ft.Row(controls=cards, wrap=True, spacing=10,
                       run_spacing=10),
            ],
            spacing=8,
        )

    def _card(self, *rows: ft.Control) -> ft.Container:
        return ft.Container(
            content=ft.Column(controls=list(rows), spacing=6),
            padding=12,
            border_radius=12,
            bgcolor=_CARD_BG,
            width=210 if not self.narrow else None,
        )

    def _outputs_section(self) -> ft.Column:
        cards = []
        for profile in pins.PROFILE.values():
            if profile.role is not Role.OUTPUT or profile.momentary:
                continue
            state = self.states.get(profile.bcm)
            switch = ft.Switch(
                label=profile.name,
                value=bool(state.value) if state and state.value else False,
                disabled=not self.connected,
                key=f"switch-{profile.bcm}",
            )
            switch.on_change = (
                lambda e, p=profile: self._on_switch(p, e))
            self.switches[profile.bcm] = switch
            self.toggles.append(switch)
            rows = [
                ft.Text(f"GPIO{profile.bcm} · {profile.driver}",
                        size=11, color=_CARD_SUB),
                switch,
            ]
            if profile.driver == "RGBLED":
                rows.append(self._rgb_swatches(profile))
            cards.append(self._card(*rows))
        off_button = ft.Button(
            content="ALL OFF", key="all-off", bgcolor="#E53935",
            color="#FFFFFF",
            on_click=lambda e: self.page.run_task(self._all_off),
        )
        self.toggles.append(off_button)
        return self._section("Outputs", "switches · relay is active-low",
                             cards, off_button)

    def _rgb_swatches(self, profile: PinProfile) -> ft.Row:
        """Preset colour buttons — set the LED in one tap (0xRRGGBB)."""
        swatches = ft.Row(
            controls=[
                ft.Button(
                    content=ft.Container(
                        width=26, height=26, border_radius=6,
                        bgcolor=f"#{packed:06X}",
                        border=ft.Border.all(1, "#B0BEC5")),
                    tooltip=name,
                    key=f"rgb-{profile.bcm}-{name}",
                    disabled=not self.connected,
                    on_click=lambda e, p=profile, c=packed: (
                        self.page.run_task(self._write_rgb, p, c)),
                )
                for name, packed in _RGB_SWATCHES
            ],
            wrap=True, spacing=6, run_spacing=6,
        )
        self.toggles.extend(swatches.controls)
        return swatches

    async def _write_rgb(self, profile: PinProfile, packed: int) -> None:
        bcm = profile.bcm
        if bcm in self.pending:
            return
        self.pending.add(bcm)
        try:
            result = await self._send(Op(
                "rgb_write", pin=bcm, value=packed,
                req_id=self.client.next_id()))
            sw = self.switches.get(bcm)
            if sw is not None:
                if result.ok:
                    sw.value = packed != 0
                self.page.update()
        finally:
            self.pending.discard(bcm)

    def _momentary_section(self) -> ft.Column:
        cards = []
        for profile in pins.PROFILE.values():
            if profile.role is not Role.OUTPUT or not profile.momentary:
                continue
            button = ft.Button(
                content=f"Buzz {PULSE_MS} ms", key=f"pulse-{profile.bcm}",
                disabled=not self.connected,
                on_click=lambda e, p=profile: self.page.run_task(
                    self._pulse, p),
            )
            self.toggles.append(button)
            rows = [
                ft.Text(profile.name, size=13, weight=ft.FontWeight.BOLD,
                        color=_CARD_TEXT),
                ft.Text(f"GPIO{profile.bcm} · tap to pulse",
                        size=11, color=_CARD_SUB),
                button,
            ]
            if profile.driver == "TonalBuzzer":
                rows.append(ft.Text("notes", size=11, color=_CARD_SUB))
                notes = ft.Row(
                    controls=[
                        ft.Button(
                            content=f"{note} {hz}",
                            key=f"tone-{profile.bcm}-{hz}",
                            disabled=not self.connected,
                            on_click=lambda e, p=profile, f=hz: (
                                self.page.run_task(self._tone, p, f)),
                        )
                        for note, hz in _TONES
                    ],
                    wrap=True, spacing=6, run_spacing=6,
                )
                self.toggles.extend(notes.controls)
                rows.append(notes)
            cards.append(self._card(*rows))
        return self._section("Momentary", "tap = one pulse", cards)

    async def _tone(self, profile: PinProfile, hz: int) -> None:
        bcm = profile.bcm
        if bcm in self.pending:
            return
        self.pending.add(bcm)
        try:
            await self._send(Op("tone", pin=bcm, value=hz,
                                req_id=self.client.next_id()))
        finally:
            self.pending.discard(bcm)

    def _inputs_section(self) -> ft.Column:
        cards = []
        for profile in pins.PROFILE.values():
            if profile.role is not Role.INPUT:
                continue
            state = self.states.get(profile.bcm)
            active = bool(state.value) if state and state.value else False
            dot = ft.Container(width=14, height=14, border_radius=7,
                               bgcolor="#FBC02D" if active else "#BDBDBD")
            label = ft.Text("active" if active else "idle", size=12,
                            color="#F57F17" if active else "#78909C")
            self.input_dots[profile.bcm] = dot
            self.input_texts[profile.bcm] = label
            cards.append(self._card(
                ft.Row(controls=[
                    dot,
                    ft.Text(profile.name, size=13,
                            weight=ft.FontWeight.BOLD, color=_CARD_TEXT),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(f"GPIO{profile.bcm} · {profile.driver}",
                        size=11, color=_CARD_SUB),
                label,
            ))
        return self._section("Inputs", "live from the Pi", cards)

    def _sensors_section(self) -> ft.Column:
        cards = []
        for profile in pins.PROFILE.values():
            if profile.role is not Role.SENSOR:
                continue
            state = self.states.get(profile.bcm)
            value = ft.Text("—", size=22, weight=ft.FontWeight.BOLD,
                            color=_CARD_TEXT)
            note = ft.Text(self._sensor_pin_note(profile), size=11,
                           color="#E53935" if profile.needs_adc else _CARD_SUB)
            if profile.needs_adc:
                note.value = "needs an ADC (not fitted)"
            self.sensor_values[profile.bcm] = value
            self.sensor_notes[profile.bcm] = note
            cards.append(self._card(
                ft.Text(profile.name, size=13, weight=ft.FontWeight.BOLD,
                        color=_CARD_TEXT),
                value,
                note,
                ft.Text(profile.driver, size=11, color=_CARD_SUB),
            ))
        return self._section("Sensors", "polled on demand", cards)

    def _pwm_section(self) -> ft.Column:
        cards = []
        for profile in pins.PROFILE.values():
            if profile.role is not Role.PWM:
                continue
            state = self.states.get(profile.bcm)
            pct = float(state.value) if state and state.value else 0.0
            # A motor slider spans reverse..stop..forward; a dimmer 0..100.
            is_motor = profile.driver == "Motor"
            low, high = (-100.0, 100.0) if is_motor else (0.0, 100.0)
            label = ft.Text(f"{pct:.0f}%", size=13,
                            weight=ft.FontWeight.BOLD, color=_ACCENT)
            slider = ft.Slider(
                value=pct, min=low, max=high, divisions=40 if is_motor else 20,
                label="{value}%", key=f"slider-{profile.bcm}",
                disabled=not self.connected,
                on_change_start=lambda e, p=profile: self.pending.add(p.bcm),
                on_change=lambda e, p=profile: self._on_slider_live(p, e),
                on_change_end=lambda e, p=profile: self.page.run_task(
                    self._commit_pwm, p, e),
            )
            self.sliders[profile.bcm] = slider
            self.slider_labels[profile.bcm] = label
            self.toggles.append(slider)
            if is_motor:
                hint = (f"GPIO{profile.bcm} fwd · GPIO{profile.aux[0]} back"
                        if profile.aux else f"GPIO{profile.bcm}")
            else:
                hint = f"GPIO{profile.bcm} · {profile.driver}"
            cards.append(self._card(
                ft.Row(controls=[
                    ft.Text(profile.name, size=13,
                            weight=ft.FontWeight.BOLD, color=_CARD_TEXT),
                    label,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Text(hint, size=11, color=_CARD_SUB),
                slider,
            ))
        return self._section("PWM", "duty cycle · send on release", cards)

    def _script_section(self) -> ft.Column:
        """Run a Python script on the Pi (opt-in: pi.py --allow-code)."""
        script = self.script
        enabled = bool(script and script.enabled)
        running = bool(script and script.running)
        if script is None or not script.enabled:
            status_text, status_color = "requires pi.py --allow-code", "#E53935"
        elif running:
            status_text, status_color = f"running (job {script.job})", "#F57F17"
        elif script.job:
            status_text, status_color = (
                f"job {script.job} finished · exit {script.exit}",
                "#43A047" if script.exit == 0 else "#E53935")
        else:
            status_text, status_color = "idle", "#78909C"

        self.script_field = ft.TextField(
            value=self.script_code,
            multiline=True, min_lines=4, max_lines=10,
            label="Python script (runs on the Pi)",
            hint_text="import time; print('hello from the Pi')",
            text_style=ft.TextStyle(font_family="monospace", size=12),
            key="script-code",
            # Always editable — you can compose while offline or while a job
            # runs; only Run/Stop are gated by connection + --allow-code.
        )
        self.script_field.on_change = self._on_script_edit
        self.script_field.on_selection_change = self._on_script_selection
        self.script_field = ft.KeyboardListener(
            content=self.script_field,
            key="script-keys",
            on_key_down=self._on_script_key,
        )
        self.script_clear = ft.Button(
            content="Clear", key="script-clear",
            disabled=not (self.script_code or "").strip(),
            on_click=lambda e: self._clear_script(),
        )
        # Indent/outdent buttons: Tab does the same thing on a desktop
        # keyboard, but phones have no Tab key at all.
        self.script_indent = ft.Button(
            content="⇥ Indent", key="script-indent",
            on_click=lambda e: self._indent_script(),
        )
        self.script_outdent = ft.Button(
            content="⇤ Outdent", key="script-outdent",
            on_click=lambda e: self._indent_script(outdent=True),
        )
        self.script_run = ft.Button(
            content="Run on Pi", key="script-run", bgcolor="#43A047",
            color="#FFFFFF",
            disabled=not (self.connected and enabled and not running),
            on_click=lambda e: self.page.run_task(self._run_script),
        )
        self.script_stop = ft.Button(
            content="Stop", key="script-stop", bgcolor="#E53935",
            color="#FFFFFF",
            disabled=not (self.connected and running),
            on_click=lambda e: self.page.run_task(self._stop_script),
        )
        self.script_status = ft.Text(status_text, size=12,
                                     color=status_color)
        self.script_output = ft.Text(
            script.output if script and script.output else "",
            size=11, selectable=True,
            font_family="monospace", color=_CARD_TEXT,
            max_lines=12, overflow=ft.TextOverflow.VISIBLE,
        )
        return ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Text("Script", size=15,
                                weight=ft.FontWeight.BOLD),
                        ft.Text("subprocess on the Pi · stdout+stderr",
                                size=11, color="#90A4AE"),
                        self.script_status,
                    ],
                    wrap=True, spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.script_field,
                ft.Row(controls=[self.script_run, self.script_stop,
                                self.script_clear, self.script_indent,
                                self.script_outdent],
                       spacing=8, wrap=True),
                self.script_output,
            ],
            spacing=8,
        )

    def _on_script_edit(self, e: ft.ControlEvent) -> None:
        self.script_code = e.control.value or ""
        if self.script_clear is not None:
            self.script_clear.disabled = not self.script_code.strip()

    @property
    def script_text_field(self) -> ft.TextField | None:
        """The script box itself — script_field is its KeyboardListener shell."""
        listener = self.script_field
        if isinstance(listener, ft.KeyboardListener):
            content = listener.content
            return content if isinstance(content, ft.TextField) else None
        return listener if isinstance(listener, ft.TextField) else None

    def _on_script_selection(self, e: ft.TextSelectionChangeEvent) -> None:
        """Remember the caret (and any selected span) for the Tab handler."""
        selection = getattr(e, "selection", None)
        if selection is not None and selection.is_valid:
            self.script_caret = selection.end
            self.script_span = (selection.start, selection.end)

    def _on_script_key(self, e: ft.KeyDownEvent) -> None:
        """Tab indents instead of leaving the box — Flutter's focus traversal
        acts on Tab too and flet 1.0 cannot consume the key, so indent first
        and pull focus back afterwards."""
        if e.key not in ("Tab", "Backtab"):
            return
        self._indent_script(outdent=e.key == "Backtab")
        self._refocus_script_box()

    def _indent_script(self, *, outdent: bool = False) -> None:
        """Indent (or outdent) the caret's line / every selected line.

        Shared by the Tab key and the toolbar buttons — buttons matter on
        phones, whose on-screen keyboard has no Tab key at all.
        """
        field = self.script_text_field
        if field is None:
            return
        text = field.value or ""
        start, end = self.script_span
        if not (0 <= start <= end <= len(text)):
            start = end = min(self.script_caret, len(text))
        indent = " " * SCRIPT_INDENT

        if end > start:  # selection: indent each non-blank line it touches
            lines = text[start:end].split("\n")
            if outdent:
                lines = [(ln[SCRIPT_INDENT:] if ln.startswith(indent)
                          else ln.lstrip(" ")) if ln.strip() else ln
                         for ln in lines]
            else:
                lines = [f"{indent}{ln}" if ln.strip() else ln for ln in lines]
            block = "\n".join(lines)
            value = text[:start] + block + text[end:]
            caret = start + len(block)
        else:  # caret only: indent (or outdent) at the start of its line
            line_start = text.rfind("\n", 0, start) + 1
            body = text[line_start:]
            if outdent:
                spaces = len(body) - len(body.lstrip(" "))
                remove = min(spaces, SCRIPT_INDENT)
                if not remove:
                    return  # already flush left — leave the text alone
                value = text[:line_start] + body[remove:]
                caret = start - remove
            else:
                value = text[:line_start] + indent + body
                caret = start + len(indent)
        field.value = value
        field.selection = ft.TextSelection(base_offset=caret,
                                           extent_offset=caret)
        self.script_code = value
        self.script_caret = caret
        self.script_span = (caret, caret)
        if self.script_clear is not None:
            self.script_clear.disabled = not value.strip()
        self.page.update()

    def _refocus_script_box(self) -> None:
        """Put the caret back in the script box after a focus-stealing key."""
        field = self.script_text_field
        if field is not None:
            self.page.run_task(field.focus)

    def _clear_script(self) -> None:
        """Stop a running job first, then empty the script box.

        Clearing while a job runs would leave the Pi executing code that is
        no longer visible in the GUI — so stop it, and log that we did.
        """
        running = bool(self.script and self.script.running)
        if running:
            self._push_log("script: stopped (cleared while running)", True)
            self.page.run_task(self._stop_script)
        field = self.script_text_field
        if field is not None:
            field.value = ""
            field.selection = ft.TextSelection(base_offset=0, extent_offset=0)
        self.script_code = ""
        self.script_caret = 0
        self.script_span = (0, 0)
        if self.script_clear is not None:
            self.script_clear.disabled = True
        self._push_log("script: cleared", True)
        self.page.update()

    async def _run_script(self) -> None:
        code = self.script_code
        if not code.strip():
            self._push_log("script: nothing to run", False)
            self.page.update()
            return
        result = await asyncio.to_thread(
            self.client.post_script, code, self.client.next_id())
        self._push_log(
            f"script: " + ("started" if result.ok else f"ERR {result.error}"),
            result.ok)
        self.page.update()
        self.page.run_task(self._poll_once)

    async def _stop_script(self) -> None:
        result = await asyncio.to_thread(
            self.client.post_script_stop, self.client.next_id())
        self._push_log(
            "script stop: " + ("ok" if result.ok else f"ERR {result.error}"),
            result.ok)
        self.page.update()
        self.page.run_task(self._poll_once)

    def _apply_script(self) -> None:
        """Refresh Run/Stop/status/output from the latest polled status."""
        script = self.script
        enabled = bool(script and script.enabled)
        running = bool(script and script.running)
        if self.script_run is not None:
            self.script_run.disabled = not (
                self.connected and enabled and not running)
        if self.script_stop is not None:
            self.script_stop.disabled = not (self.connected and running)
        if self.script_status is not None and script is not None:
            if not script.enabled:
                self.script_status.value = "requires pi.py --allow-code"
                self.script_status.color = "#E53935"
            elif running:
                self.script_status.value = f"running (job {script.job})"
                self.script_status.color = "#F57F17"
            elif script.job:
                self.script_status.value = (
                    f"job {script.job} finished · exit {script.exit}")
                self.script_status.color = (
                    "#43A047" if script.exit == 0 else "#E53935")
            else:
                self.script_status.value = "idle"
                self.script_status.color = "#78909C"
        if self.script_output is not None and script is not None:
            self.script_output.value = script.output or ""

    def _log_section(self) -> ft.Column:
        self.log_column = ft.Column(controls=self._log_texts(), spacing=2)
        return ft.Column(
            controls=[
                ft.Text("Activity", size=15, weight=ft.FontWeight.BOLD),
                self.log_column,
            ],
            spacing=6,
        )

    # -------------------------------------------------------------- board ----

    def _board_section(self) -> ft.Column:
        toggle = ft.IconButton(
            ft.Icons.EXPAND_LESS if self.board_open else ft.Icons.EXPAND_MORE,
            tooltip="Show/hide the header",
            key="board-toggle",
            on_click=self._toggle_board,
        )
        subtitle = ("tap an output cell to toggle"
                    if self.narrow else
                    "cell = #physical · BCM · live state")
        header = ft.Row(
            controls=[
                ft.Column(
                    controls=[
                        ft.Text("Board — 40-pin header", size=15,
                                weight=ft.FontWeight.BOLD),
                        ft.Text(subtitle, size=11, color="#90A4AE"),
                    ],
                    spacing=0,
                ),
                toggle,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        board = ft.Column(spacing=6)
        if self.board_open:
            board.controls.append(self._board_vertical()
                                  if self.narrow
                                  else self._board_horizontal())
        return ft.Column(controls=[header, board], spacing=4)

    def _toggle_board(self, e: ft.ControlEvent) -> None:
        self.board_open = not self.board_open
        self._render()

    def _board_horizontal(self) -> ft.Column:
        # The reference diagram rotated 90° clockwise: its left column
        # (1,3..39) becomes the top row and its right column (2,4..40) the
        # second row, with each column's first pins (1 and 2) at the RIGHT
        # end. Wide layout only; narrow keeps the untouched vertical pinout.
        odd = [pins.HEADER[i] for i in range(38, -1, -2)]   # 39,37..1
        even = [pins.HEADER[i] for i in range(39, -1, -2)]  # 40,38..2
        return ft.Column(
            controls=[
                ft.Row(controls=[self._board_cell(c) for c in odd],
                       spacing=3, scroll=ft.ScrollMode.AUTO),
                ft.Row(controls=[self._board_cell(c) for c in even],
                       spacing=3, scroll=ft.ScrollMode.AUTO),
            ],
            spacing=4,
        )

    def _board_vertical(self) -> ft.Column:
        rows = []
        for i in range(20):
            left = pins.HEADER[i * 2]      # odd physical  2i+1
            right = pins.HEADER[i * 2 + 1]  # even physical 2i+2
            rows.append(ft.Row(
                controls=[
                    self._side_label(left, end=False),
                    self._board_cell(left),
                    self._board_cell(right),
                    self._side_label(right, end=True),
                ],
                spacing=6,
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
        return ft.Column(controls=rows, spacing=4)

    def _side_label(self, cell: PinProfile, *, end: bool) -> ft.Container:
        if self._is_wired(cell):
            label, color, weight = cell.name, _ACCENT, ft.FontWeight.BOLD
        elif cell.role in (Role.POWER, Role.GND, Role.ID):
            label, color, weight = cell.name, "#90A4AE", None
        else:
            label, color, weight = cell.alt_function, "#90A4AE", None
        return ft.Container(
            content=ft.Text(
                label, size=11, color=color, weight=weight, width=86,
                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                text_align=ft.TextAlign.END if end else ft.TextAlign.START,
            ),
            width=90,
        )

    @staticmethod
    def _is_wired(cell: PinProfile) -> bool:
        if cell.bcm is not None and pins.PROFILE.get(cell.bcm) is cell:
            return True
        return cell.name.endswith("(echo)")

    def _cell_tooltip(self, cell: PinProfile) -> str:
        parts = [cell.name, f"physical #{cell.physical}"]
        if cell.bcm is not None:
            parts.append(f"BCM {cell.bcm}")
        if cell.alt_function:
            parts.append(cell.alt_function)
        return " · ".join(parts)

    def _cell_initial_badge(self, cell: PinProfile) -> tuple[str, str, str]:
        """(text, color, bgcolor) before any state arrives."""
        if not self._is_wired(cell):
            return "", _CARD_TEXT, "#FFFFFF"
        if cell.name.endswith("(echo)"):
            return "echo", "#546E7A", "#ECEFF1"
        return cell.name, _CARD_TEXT, "#FFFFFF"

    def _board_cell(self, cell: PinProfile) -> ft.Container:
        ring = _FUNCTION_COLORS[cell.function]
        badge_text, badge_color, badge_bg = self._cell_initial_badge(cell)
        badge = ft.Text(badge_text, size=8, weight=ft.FontWeight.BOLD,
                        color=badge_color, text_align=ft.TextAlign.CENTER,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        width = 58 if self.narrow else 50
        container = ft.Container(
            width=width,
            height=50,
            border=ft.Border.all(2, ring),
            border_radius=8,
            bgcolor=badge_bg,
            alignment=ft.Alignment.CENTER,
            padding=2,
            tooltip=self._cell_tooltip(cell),
            key=f"cell-{cell.physical}",
            content=ft.Column(
                controls=[
                    ft.Text(f"#{cell.physical}", size=7, color="#90A4AE",
                            text_align=ft.TextAlign.CENTER, max_lines=1),
                    ft.Text(pins.header_label(cell), size=8, color=_CARD_TEXT,
                            weight=ft.FontWeight.BOLD,
                            text_align=ft.TextAlign.CENTER, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    badge,
                ],
                spacing=0,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        if cell.bcm is not None:
            if self._is_wired(cell) and pins.PROFILE.get(cell.bcm) is cell:
                if pins.PROFILE[cell.bcm].role is Role.OUTPUT:
                    container.on_click = (
                        lambda e, c=cell: self._on_cell_tap(c))
            if cell.bcm in self.states:
                self._apply_cell_in(container, cell, self.states[cell.bcm])
        if cell.bcm is not None:
            # keep refs so later polls can update this cell
            self.cell_bgs[cell.bcm] = container
            self.cell_badges[cell.bcm] = badge
        return container

    def _apply_cell_in(self, container: ft.Container, cell: PinProfile,
                       state: PinState) -> None:
        text, color, bgcolor = _badge_for_state(state)
        container.bgcolor = bgcolor
        # badge Text lives as the 3rd child of the cell's inner Column
        badge = container.content.controls[2]
        badge.value = text
        badge.color = color

    def _on_cell_tap(self, cell: PinProfile) -> None:
        profile = pins.PROFILE.get(cell.bcm) if cell.bcm is not None else None
        if profile is None or profile.role is not Role.OUTPUT:
            return
        if not self.connected or profile.bcm in self.pending:
            return
        state = self.states.get(profile.bcm)
        if state is None or state.fault:
            return
        self.page.run_task(self._write_output, profile,
                           not bool(state.value))

    # ------------------------------------------------------------ actions ----

    def _on_switch(self, profile: PinProfile, e: ft.ControlEvent) -> None:
        if profile.bcm in self.pending:
            return
        self.page.run_task(self._write_output, profile,
                           bool(e.control.value))

    async def _write_output(self, profile: PinProfile, want: bool) -> None:
        bcm = profile.bcm
        self.pending.add(bcm)
        try:
            if profile.momentary:
                result = await self._send(Op(
                    "pulse", pin=bcm, value=PULSE_MS,
                    req_id=self.client.next_id()))
            else:
                result = await self._send(Op(
                    "digital_write", pin=bcm, value=int(want),
                    req_id=self.client.next_id()))
            sw = self.switches.get(bcm)
            if sw is not None:
                if result.ok and not profile.momentary:
                    sw.value = want
                elif not result.ok:
                    sw.value = not want  # revert the optimistic flip
                self.page.update()
        finally:
            self.pending.discard(bcm)

    async def _pulse(self, profile: PinProfile) -> None:
        bcm = profile.bcm
        if bcm in self.pending:      # ignore taps while a pulse is in flight
            return
        self.pending.add(bcm)
        try:
            await self._send(Op("pulse", pin=bcm, value=PULSE_MS,
                                req_id=self.client.next_id()))
        finally:
            self.pending.discard(bcm)

    async def _all_off(self) -> None:
        result = await self._send(
            Op("all_off", req_id=self.client.next_id()))
        if not result.ok:
            return
        for bcm, sw in self.switches.items():
            sw.value = False
            self.pending.discard(bcm)
        for bcm, slider in self.sliders.items():
            slider.value = 0
            self.pending.discard(bcm)
            label = self.slider_labels.get(bcm)
            if label is not None:
                label.value = "0%"
        self.page.update()

    def _on_slider_live(self, profile: PinProfile,
                        e: ft.ControlEvent) -> None:
        label = self.slider_labels.get(profile.bcm)
        if label is not None:
            label.value = f"{float(e.control.value):.0f}%"
            self.page.update()

    async def _commit_pwm(self, profile: PinProfile,
                          e: ft.ControlEvent) -> None:
        try:
            pct = float(e.control.value)
            op = ("motor_write" if profile.driver == "Motor"
                  else "pwm_write")
            result = await self._send(Op(
                op, pin=profile.bcm, value=pct,
                req_id=self.client.next_id()))
            if not result.ok:
                # revert to the last state the Pi reported
                state = self.states.get(profile.bcm)
                slider = self.sliders.get(profile.bcm)
                if state is not None and slider is not None \
                        and state.value is not None:
                    slider.value = float(state.value)
                    label = self.slider_labels.get(profile.bcm)
                    if label is not None:
                        label.value = f"{float(state.value):.0f}%"
                self.page.update()
        finally:
            self.pending.discard(profile.bcm)


def main(page: ft.Page) -> None:
    App(page).start()


ft.run(main)
