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
from protocol import Op, OpResult, PinState

NARROW_BREAKPOINT = 600          # px: below this we render portrait layout
POLL_OK_SECONDS = 1.5            # healthy poll cadence
POLL_FAIL_SECONDS = 3.0          # slower retry while unreachable
REQUEST_TIMEOUT = 3.0            # per-request urllib timeout
LOG_LINES = 16                   # activity log depth
PULSE_MS = 300                   # click-to-pulse duration for the buzzer

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

    def post_op(self, op: Op) -> OpResult:
        request = urllib.request.Request(
            f"{self.base}/op",
            data=protocol.encode_op(op),
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
                return OpResult(ok=False, req_id=op.req_id,
                                error=f"HTTP {ex.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            reason = getattr(ex, "reason", ex)
            return OpResult(ok=False, req_id=op.req_id,
                            error=f"unreachable: {reason}")
        except Exception as ex:  # client boundary: never crash the GUI loop
            return OpResult(ok=False, req_id=op.req_id,
                            error=f"{type(ex).__name__}: {ex}")

    def fetch_state(self) -> tuple[list[PinState] | None, str | None]:
        """Returns (states, None) on success or (None, reason)."""
        try:
            with urllib.request.urlopen(f"{self.base}/state",
                                        timeout=REQUEST_TIMEOUT) as resp:
                return protocol.parse_state(resp.read()), None
        except urllib.error.HTTPError as ex:
            return None, f"HTTP {ex.code}"
        except protocol.ProtocolError as ex:
            return None, f"bad response: {ex}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            return None, f"unreachable: {getattr(ex, 'reason', ex)}"
        except Exception as ex:  # client boundary: never crash the poll loop
            return None, f"{type(ex).__name__}: {ex}"


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
        self.pending: set[int] = set()      # BCMs with an op in flight
        self.board_open = True
        self.log: list[tuple[str, bool]] = []
        self.shell: ft.Column | None = None
        # Widget refs rebuilt on every _render():
        self.dot: ft.Container | None = None
        self.conn_text: ft.Text | None = None
        self.host_field: ft.TextField | None = None
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
        connect = ft.Button(content="Connect", key="connect",
                            on_click=self._on_connect)
        self.dot = ft.Container(width=12, height=12, border_radius=6,
                                bgcolor="#BDBDBD")
        self.conn_text = ft.Text("waiting for the Pi…", size=12,
                                 color="#546E7A")
        return ft.Row(
            controls=[self.host_field, connect, self.dot, self.conn_text],
            wrap=True,
            spacing=10,
            run_spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _on_connect(self, e: ft.ControlEvent) -> None:
        raw = (self.host_field.value if self.host_field else "") or ""
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
        self.host_field.value = f"{self.client.host}:{self.client.port}"
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
        states, error = await asyncio.to_thread(self.client.fetch_state)
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
        self.connected = True
        self.error = None
        self.latency_ms = elapsed_ms
        if not was_connected:
            self._push_log(
                f"connected to {self.client.base} "
                f"({elapsed_ms:.0f} ms)", True)
        self._apply_states()
        self._apply_connection()
        self.page.update()
        return POLL_OK_SECONDS

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
                        pins_ = f"GPIO{bcm}"
                        if profile.echo is not None:
                            pins_ += f" + GPIO{profile.echo}"
                        note.value = pins_
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
            cards.append(self._card(
                ft.Text(f"GPIO{profile.bcm} · {profile.driver}",
                        size=11, color=_CARD_SUB),
                switch,
            ))
        off_button = ft.Button(
            content="ALL OFF", key="all-off", bgcolor="#E53935",
            color="#FFFFFF",
            on_click=lambda e: self.page.run_task(self._all_off),
        )
        self.toggles.append(off_button)
        return self._section("Outputs", "switches · relay is active-low",
                             cards, off_button)

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
            cards.append(self._card(
                ft.Text(profile.name, size=13, weight=ft.FontWeight.BOLD,
                        color=_CARD_TEXT),
                ft.Text(f"GPIO{profile.bcm} · tap to pulse",
                        size=11, color=_CARD_SUB),
                button,
            ))
        return self._section("Momentary", "tap = one pulse", cards)

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
            if profile.needs_adc:
                note = ft.Text("needs an ADC (not fitted)", size=11,
                               color="#E53935")
            else:
                pins_ = f"GPIO{profile.bcm}"
                if profile.echo is not None:
                    pins_ += f" + GPIO{profile.echo}"
                note = ft.Text(pins_, size=11, color=_CARD_SUB)
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
            label = ft.Text(f"{pct:.0f}%", size=13,
                            weight=ft.FontWeight.BOLD, color=_ACCENT)
            slider = ft.Slider(
                value=pct, min=0, max=100, divisions=20,
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
            cards.append(self._card(
                ft.Row(controls=[
                    ft.Text(profile.name, size=13,
                            weight=ft.FontWeight.BOLD, color=_CARD_TEXT),
                    label,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Text(f"GPIO{profile.bcm} · {profile.driver}",
                        size=11, color=_CARD_SUB),
                slider,
            ))
        return self._section("PWM", "duty cycle · send on release", cards)

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
        odd = [pins.HEADER[i] for i in range(0, 40, 2)]   # physical 1,3..39
        even = [pins.HEADER[i] for i in range(1, 40, 2)]  # physical 2,4..40
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
            result = await self._send(Op(
                "pwm_write", pin=profile.bcm, value=pct,
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
