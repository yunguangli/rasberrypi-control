"""picontrol GUI — the client app (Flet 1.0), phone-portrait-first.

MVC-ish split in a single file; this is the only module allowed to import
flet (boundary enforced by tests/test_import_boundary.py):

  PiClient — model/transport: blocking stdlib HTTP against pi.py. The
             controller wraps every call in asyncio.to_thread.
  App      — controller: poll loop, optimistic updates, activity log,
             responsive navigation, portrait lock.
  _render* — view: three pages generated from pins.HEADER / pins.PROFILE
             (so adding a device to pins.py adds its card automatically).

Layout (app shell):

  [ fixed header: target + connection status ]
  [ Board | Control | Script ]   wide: Tabs  ·  narrow: NavigationBar
  [ the selected page scrolls; the shell itself never does ]

Wide screens pin a compact 2x20 Board strip above the tabs; the Board page
carries the full detail (number, BCM, alt function, family, wired device,
live state) on every screen size.
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

# ---------------------------------------------------------------- type scale --
# Sizes are deliberately generous: the GUI is read at arm's length, often on a
# phone, and the old 7-8 px pin labels were unreadable.
FS_TINY = 12                     # pin number, faint meta
FS_SMALL = 13                    # subtitles, pin notes, log
FS_BODY = 15                     # card body, switch labels
FS_CARD_TITLE = 17               # card titles
FS_SECTION = 20                  # section headings
FS_VALUE = 28                    # sensor readings
FS_STATUS = 14                   # connection / job status
FS_CODE = 14                     # script editor
FS_CONSOLE = 13                  # script console

# ------------------------------------------------------------------- palette --
CARD_BG = "#FFFFFF"
PAGE_BG = "#F4F6F9"
CARD_BORDER = "#DDE3EA"
TEXT = "#1F2933"
TEXT_SUB = "#52606D"             # 4.9:1 on white — was #78909C (2.9:1)
TEXT_FAINT = "#66727D"
ACCENT = "#1565C0"
OK = "#2E7D32"
OK_BG = "#E8F5E9"
WARN = "#E65100"
WARN_BG = "#FFF3E0"
DANGER = "#C62828"
DANGER_BG = "#FFEBEE"
IDLE_BG = "#ECEFF1"
CONSOLE_BG = "#101820"
CONSOLE_FG = "#D6E2EC"

# Quick-pick targets for the popup under the host field: this machine
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

_FUNCTION_LABELS: dict[FunctionColor, str] = {
    FunctionColor.GPIO: "GPIO",
    FunctionColor.POWER: "Power",
    FunctionColor.GND: "Ground",
    FunctionColor.UART: "UART",
    FunctionColor.SPI: "SPI",
    FunctionColor.I2C: "I2C",
    FunctionColor.ID: "ID EEPROM",
}

_DRIVER_ICONS: dict[str, str] = {
    "LED": ft.Icons.LIGHTBULB,
    "RGBLED": ft.Icons.PALETTE,
    "Buzzer": ft.Icons.VOLUME_UP,
    "TonalBuzzer": ft.Icons.MUSIC_NOTE,
    "Button": ft.Icons.RADIO_BUTTON_CHECKED,
    "MotionSensor": ft.Icons.SENSORS,
    "DHT22": ft.Icons.THERMOSTAT,
    "HCSR04": ft.Icons.STRAIGHTEN,
    "LDR": ft.Icons.WB_SUNNY,
    "CPUTemperature": ft.Icons.MEMORY,
    "LoadAverage": ft.Icons.SPEED,
    "PWMLED": ft.Icons.BRIGHTNESS_6,
    "Motor": ft.Icons.SETTINGS,
}

_DRIVER_ICON_FALLBACK = ft.Icons.ELECTRIC_BOLT

# Snippets offered by the Script tab's "Examples" menu.
SCRIPT_EXAMPLES: tuple[tuple[str, str], ...] = (
    (
        "Blink the Status LED",
        "import time\n"
        "from gpiozero import LED\n"
        "\n"
        "led = LED(17)\n"
        "for _ in range(6):\n"
        "    led.toggle()\n"
        "    time.sleep(0.5)\n"
        "led.off()\n",
    ),
    (
        "Sweep the dimmer",
        "import time\n"
        "from gpiozero import PWMLED\n"
        "\n"
        "dim = PWMLED(18)\n"
        "for level in range(0, 101, 10):\n"
        "    print(f'{level}%')\n"
        "    dim.value = level / 100\n"
        "    time.sleep(0.2)\n"
        "dim.off()\n",
    ),
    (
        "Read the Pi's CPU temperature",
        "from gpiozero import CPUTemperature\n"
        "\n"
        "for _ in range(10):\n"
        "    print(f\"{CPUTemperature().temperature:.1f} °C\")\n"
        "    time.sleep(0.5)\n",
    ),
    (
        "Watch the door button",
        "import time\n"
        "from gpiozero import Button\n"
        "\n"
        "button = Button(24, pull_up=True)\n"
        "print('waiting for the button…')\n"
        "while True:\n"
        "    button.wait_for_press()\n"
        "    print('pressed')\n"
        "    time.sleep(0.3)\n",
    ),
)


def _describe(op: Op) -> str:
    """Human-readable one-liner for the activity log."""
    if op.op in protocol.SYSTEM_OPS:
        return op.op
    text = f"{op.op} pin {op.pin}"
    if op.value is not None:
        text += f" = {op.value}"
    return text


def _badge_for_state(state: PinState) -> tuple[str, str, str]:
    """(text, color, background) for a live-state chip."""
    if state.fault:
        if state.fault.startswith("script running"):
            # GPIO deliberately released for a script job — not a failure.
            return "halted", WARN, WARN_BG
        return "ERR", DANGER, DANGER_BG
    if state.direction == "out":
        if state.value:
            return "ON", OK, OK_BG
        return "OFF", TEXT_SUB, IDLE_BG
    if state.direction == "in":
        if state.value:
            return "ACT", WARN, WARN_BG
        return "idle", TEXT_SUB, IDLE_BG
    if state.direction == "pwm":
        if state.value is None:
            return "—", TEXT_SUB, IDLE_BG
        return f"{float(state.value):.0f}%", ACCENT, "#E3F2FD"
    return _format_reading(state.value, state.unit), "#4A148C", "#F3E5F5"


def _format_reading(value: float | int | None, unit: str) -> str:
    """'58.1%' (tight) but '22.2 °C' / '0.83 m' (SI spacing)."""
    if value is None:
        return "—"
    return f"{value}%" if unit == "%" else f"{value} {unit}".strip()


def _driver_icon(profile: PinProfile) -> str:
    return _DRIVER_ICONS.get(profile.driver, _DRIVER_ICON_FALLBACK)


class PiClient:
    """Blocking HTTP client (stdlib urllib only).

    Never raises: connection problems come back as structured errors so the
    GUI can render them and keep polling. App calls these from threads.
    """

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = protocol.DEFAULT_PORT
        self._req_id = 0
        # Ignore http_proxy/https_proxy from the shell: a proxy cannot reach
        # the Pi's LAN address, so every poll would fail (and the address
        # would leak to whatever proxy is configured).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

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
            with self._opener.open(request,
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
            with self._opener.open(f"{self.base}/state",
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


class App:
    """Controller + view builder for the picontrol GUI."""

    # page indices (both the Tabs and the NavigationBar use them)
    PAGE_BOARD = 0
    PAGE_CONTROL = 1
    PAGE_SCRIPT = 2

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
        # Poll generations: a response from an older target, or one that a
        # newer poll already superseded, must never overwrite the view.
        self._target_gen = 0
        self._poll_seq = 0
        self._poll_applied = 0
        self.page_index = self.PAGE_CONTROL
        self.board_open = True
        self.wired_only = False
        self.log_open = True
        self.log: list[tuple[str, bool]] = []
        # Set when the window/tab goes away: stop polling, ignore UI calls.
        self._session_lost = False

        # app shell (rebuilt on every _render)
        self.shell: ft.Column | None = None
        self.body: ft.Container | None = None
        self.tabs: ft.Tabs | None = None
        self.nav: ft.NavigationBar | None = None
        self.page_controls: dict[int, ft.Control] = {}
        # Widget refs rebuilt on every _render():
        self.dot: ft.Container | None = None
        self.conn_chip: ft.Container | None = None
        self.conn_text: ft.Text | None = None
        self.host_field: ft.TextField | None = None
        self.preset_menu: ft.PopupMenuButton | None = None
        self.log_column: ft.Column | None = None
        self.log_body: ft.Container | None = None
        self.log_toggle: ft.IconButton | None = None
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
        self.pin_chips: dict[int, tuple[ft.Text, ft.Container]] = {}
        self.pin_notes: dict[int, ft.Text] = {}
        self.script_field: ft.KeyboardListener | None = None
        self.script_clear: ft.Button | None = None
        self.script_indent: ft.Button | None = None
        self.script_outdent: ft.Button | None = None
        self.script_run: ft.Button | None = None
        self.script_stop: ft.Button | None = None
        self.script_status: ft.Text | None = None
        self.console_text: ft.Text | None = None
        self.console: ft.Column | None = None

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> None:
        self.page.title = "picontrol"
        self.page.padding = 10
        self.page.bgcolor = PAGE_BG
        # The cards carry their own light palette; forcing the light theme
        # keeps headings/labels readable when the OS is in dark mode.
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.theme = ft.Theme(color_scheme_seed=ACCENT)
        self.page.on_resize = self._on_resize
        # The window/tab can go away while polls and scrolls are in flight.
        self.page.on_disconnect = self._on_session_end
        self.page.on_close = self._on_session_end
        self.narrow = self._initial_narrow()
        self._render()
        self.page.run_task(self._lock_portrait)
        self.page.run_task(self._poll_loop)

    def _initial_narrow(self) -> bool:
        if self.page.platform in (ft.PagePlatform.ANDROID,
                                  ft.PagePlatform.IOS):
            return True
        # page.width is what the UI actually got (web + desktop); the window
        # size is 0/None until the native window is created.
        for raw in (self.page.width, self.page.window.width):
            try:
                width = float(raw or 0)
            except Exception:
                width = 0.0
            if width:
                return width < NARROW_BREAKPOINT
        return False

    async def _lock_portrait(self) -> None:
        if self.page.platform in (ft.PagePlatform.ANDROID,
                                  ft.PagePlatform.IOS):
            try:
                await self.page.set_allowed_device_orientations(
                    [ft.DeviceOrientation.PORTRAIT_UP])
            except Exception:
                pass  # some devices refuse; the layout re-flows anyway

    def _on_session_end(self, e: ft.ControlEvent | None = None) -> None:
        """Window/tab gone: stop polling and ignore late UI calls."""
        self._session_lost = True

    def _on_resize(self, e: ft.PageResizeEvent) -> None:
        narrow = e.width < NARROW_BREAKPOINT
        if narrow == self.narrow:
            return
        self.narrow = narrow
        # Both form factors have the same three pages — keep the user on the
        # page they were looking at (only the navigation widget changes).
        self.page_index = max(0, min(self.page_index, self.PAGE_SCRIPT))
        self._render()

    # -------------------------------------------------------------- shell ----

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
        self.pin_chips.clear()
        self.pin_notes.clear()

        self.page_controls = {
            self.PAGE_BOARD: self._board_page(),
            self.PAGE_CONTROL: self._control_page(),
            self.PAGE_SCRIPT: self._script_page(),
        }

        header = self._header()
        if self.narrow:
            self.body = ft.Container(
                expand=True,
                content=self.page_controls[self.page_index],
            )
            self.nav = ft.NavigationBar(
                selected_index=self.page_index,
                on_change=self._on_nav_change,
                bgcolor="#FFFFFF",
                indicator_color="#DCE9F8",
                destinations=[
                    ft.NavigationBarDestination(
                        icon=ft.Icons.DASHBOARD, label="Board",
                        key="nav-board"),
                    ft.NavigationBarDestination(
                        icon=ft.Icons.TUNE, label="Control",
                        key="nav-control"),
                    ft.NavigationBarDestination(
                        icon=ft.Icons.TERMINAL, label="Script",
                        key="nav-script"),
                ],
            )
            controls = [header, self.body, self.nav]
        else:
            self.tabs = ft.Tabs(
                length=3,
                selected_index=self.page_index,
                expand=True,
                on_change=self._on_tab_change,
                content=ft.Column(
                    expand=True,
                    controls=[
                        ft.TabBar(
                            tabs=[
                                ft.Tab(label="Board", icon=ft.Icons.DASHBOARD,
                                       key="tab-board"),
                                ft.Tab(label="Control", icon=ft.Icons.TUNE,
                                       key="tab-control"),
                                ft.Tab(label="Script", icon=ft.Icons.TERMINAL,
                                       key="tab-script"),
                            ],
                        ),
                        ft.TabBarView(
                            expand=True,
                            controls=[
                                self.page_controls[self.PAGE_BOARD],
                                self.page_controls[self.PAGE_CONTROL],
                                self.page_controls[self.PAGE_SCRIPT],
                            ],
                        ),
                    ],
                ),
            )
            controls = [header, self._board_strip(), self.tabs]

        if self.shell is None:
            self.shell = ft.Column(controls=controls, spacing=10, expand=True)
            self.page.add(self.shell)
        else:
            self.shell.controls = controls
        self._apply_states()
        self._apply_connection()
        self.page.update()

    def _on_tab_change(self, e: ft.ControlEvent) -> None:
        try:
            self.page_index = int(e.control.selected_index)
        except Exception:
            try:
                self.page_index = int(e.data or 0)
            except Exception:
                return

    def _on_nav_change(self, e: ft.ControlEvent) -> None:
        try:
            index = int(e.control.selected_index)
        except Exception:
            try:
                index = int(e.data or 0)
            except Exception:
                return
        self.page_index = index
        if self.body is not None:
            self.body.content = self.page_controls.get(
                index, self.page_controls[self.PAGE_CONTROL])
        self.page.update()

    def _go_to_page(self, index: int) -> None:
        """Switch pages from code (e.g. the Board strip's Details button)."""
        self.page_index = index
        if self.narrow:
            if self.nav is not None:
                self.nav.selected_index = index
            if self.body is not None:
                self.body.content = self.page_controls.get(
                    index, self.page_controls[self.PAGE_CONTROL])
        elif self.tabs is not None:
            self.tabs.selected_index = index
        self.page.update()

    # --------------------------------------------------------- connection ----

    def _header(self) -> ft.Control:
        self.host_field = ft.TextField(
            value=self.client.base.removeprefix("http://"),
            label="Pi host:port",
            width=210 if self.narrow else 250,
            text_style=ft.TextStyle(size=FS_BODY),
            key="host",
            on_submit=self._on_connect,
        )
        connect = ft.Button(
            content="Connect", key="connect", icon=ft.Icons.LINK,
            height=48, on_click=self._on_connect,
        )
        self.preset_menu = ft.PopupMenuButton(
            key="preset",
            icon=ft.Icons.LAN,
            tooltip="Quick targets",
            items=[
                ft.PopupMenuItem(
                    content=ft.Text(target, size=FS_BODY),
                    on_click=lambda e, t=target: self._apply_target(t),
                )
                for target in TARGET_PRESETS
            ],
        )
        self.dot = ft.Container(width=12, height=12, border_radius=6,
                                bgcolor="#BDBDBD")
        self.conn_text = ft.Text(
            "waiting for the Pi…", size=FS_STATUS, color=TEXT_SUB,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self.conn_chip = ft.Container(
            content=ft.Row(controls=[self.dot, self.conn_text],
                           spacing=8, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=10,
            bgcolor=IDLE_BG,
        )
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.DEVELOPER_BOARD, size=24,
                                    color=ACCENT),
                            ft.Text("picontrol", size=FS_SECTION,
                                    weight=ft.FontWeight.BOLD, color=TEXT),
                        ],
                        spacing=8, tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.host_field,
                    self.preset_menu,
                    connect,
                    self.conn_chip,
                ],
                wrap=True,
                spacing=10,
                run_spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border_radius=14,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, CARD_BORDER),
        )

    def _on_connect(self, e: ft.ControlEvent) -> None:
        self._apply_target(
            (self.host_field.value if self.host_field else "") or "")

    def _apply_target(self, raw: str) -> None:
        """Point the client at `host[:port]` and mirror it in the widget."""
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
        self._push_log(f"target -> {self.client.base}", True)
        self._target_gen += 1  # in-flight polls for the old target are stale
        self.page.update()
        self.page.run_task(self._poll_once)

    def _apply_connection(self) -> None:
        if self.connected:
            color, text = OK, (f"connected · {self.latency_ms or 0:.0f} ms")
            text_color, chip_bg = OK, OK_BG
            tooltip = self.client.base
        elif self.error:
            color, text = DANGER, "can't reach the Pi — is pi.py running?"
            text_color, chip_bg = DANGER, DANGER_BG
            tooltip = self.error
        else:
            color, text, text_color, chip_bg = (
                "#BDBDBD", "waiting for the Pi…", TEXT_SUB, IDLE_BG)
            tooltip = None
        if self.dot is not None:
            self.dot.bgcolor = color
        if self.conn_text is not None:
            self.conn_text.value = text
            self.conn_text.color = text_color
        if self.conn_chip is not None:
            self.conn_chip.bgcolor = chip_bg
            self.conn_chip.tooltip = tooltip
        for control in self.toggles:
            control.disabled = not self.connected
        # Run/Stop are not in toggles (their gating also depends on --allow-code
        # and on whether a job is running), so refresh them here too — else
        # they stay enabled after the Pi becomes unreachable.
        self._apply_script()

    # ------------------------------------------------------------ polling ----

    async def _poll_loop(self) -> None:
        while not self._session_lost:
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
        if self._session_lost:
            return POLL_FAIL_SECONDS
        self._poll_seq += 1
        seq, gen = self._poll_seq, self._target_gen
        was_connected = self.connected
        t0 = time.monotonic()
        states, script, error = await asyncio.to_thread(self.client.fetch_state)
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        # Drop a response that belongs to a previous target, or that a newer
        # poll already superseded: applying it would flap the whole view.
        if gen != self._target_gen or seq <= self._poll_applied:
            return 0.0
        self._poll_applied = seq

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
        self._scroll_console()
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
            return [ft.Text("no operations yet", size=FS_SMALL,
                            color=TEXT_FAINT)]
        return [
            ft.Text(text, size=FS_SMALL, max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    color=OK if ok else DANGER)
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
    def _pin_label(profile: PinProfile) -> str:
        """The small hardware line under a device name."""
        parts: list[str] = []
        if profile.physical is not None:
            parts.append(f"GPIO{profile.bcm} · pin #{profile.physical}")
        elif profile.driver == "LDR":
            parts.append("MCP3008 · SPI0")
        else:
            parts.append("on the Pi")
        parts.append(profile.driver)
        if profile.echo is not None:
            parts.append(f"echo GPIO{profile.echo}")
        if profile.aux:
            labels = {"Motor": ("back",), "RGBLED": ("G", "B")}.get(
                profile.driver, ())
            for pin, label in zip(profile.aux, labels):
                parts.append(f"{label} GPIO{pin}")
        return " · ".join(parts)

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
                    label.color = WARN if active else TEXT_SUB

            value_text = self.sensor_values.get(bcm)
            if value_text is not None:
                note = self.sensor_notes.get(bcm)
                if state.fault:
                    value_text.value = "—"
                    value_text.color = DANGER
                    if note is not None:
                        note.value = state.fault
                        note.color = DANGER
                else:
                    value_text.value = _format_reading(state.value,
                                                       state.unit)
                    value_text.color = TEXT
                    if note is not None:
                        note.value = self._pin_label(profile)
                        note.color = TEXT_SUB

            slider = self.sliders.get(bcm)
            if slider is not None and not busy and state.value is not None:
                pct = float(state.value)
                slider.value = pct
                label = self.slider_labels.get(bcm)
                if label is not None:
                    label.value = f"{pct:.0f}%"

            self._apply_cell(bcm, state)
            self._apply_pin_card(bcm, state)

    def _apply_cell(self, bcm: int, state: PinState) -> None:
        badge = self.cell_badges.get(bcm)
        bg = self.cell_bgs.get(bcm)
        if badge is None or bg is None:
            return
        text, color, bgcolor = _badge_for_state(state)
        badge.value = text
        badge.color = color
        bg.bgcolor = bgcolor

    def _apply_pin_card(self, bcm: int, state: PinState) -> None:
        """Update the detailed Board-page chip/notes for one wired pin."""
        chip = self.pin_chips.get(bcm)
        if chip is None:
            return
        text, box = chip
        label, color, bgcolor = _badge_for_state(state)
        text.value = label
        text.color = color
        box.bgcolor = bgcolor
        note = self.pin_notes.get(bcm)
        profile = pins.PROFILE.get(bcm)
        if note is not None and profile is not None:
            if state.fault:
                note.value = state.fault
                note.color = DANGER
            else:
                note.value = f"{profile.driver} · {state.direction}"
                note.color = TEXT_SUB

    # ----------------------------------------------------------- sections ----

    def _section_head(self, icon: str, title: str, subtitle: str,
                      *trailing: ft.Control) -> ft.Control:
        return ft.Row(
            controls=[
                ft.Icon(icon, size=22, color=ACCENT),
                ft.Text(title, size=FS_SECTION, weight=ft.FontWeight.BOLD,
                        color=TEXT),
                ft.Text(subtitle, size=FS_SMALL, color=TEXT_SUB),
                *trailing,
            ],
            wrap=True,
            spacing=10,
            run_spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _section(self, icon: str, title: str, subtitle: str,
                 cards: list[ft.Control], *trailing: ft.Control) -> ft.Control:
        if not cards:
            return ft.Column(controls=[])
        return ft.Column(
            controls=[
                self._section_head(icon, title, subtitle, *trailing),
                ft.ResponsiveRow(
                    controls=cards,
                    spacing=12,
                    run_spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            spacing=10,
        )

    def _card(self, *rows: ft.Control, col: dict | None = None) -> ft.Container:
        return ft.Container(
            content=ft.Column(controls=list(rows), spacing=8),
            padding=14,
            border_radius=14,
            bgcolor=CARD_BG,
            border=ft.Border.all(1, CARD_BORDER),
            col=col or {"xs": 12, "sm": 6, "md": 4, "lg": 4, "xl": 3},
        )

    def _card_title(self, profile: PinProfile,
                    *trailing: ft.Control) -> ft.Row:
        return ft.Row(
            controls=[
                ft.Icon(_driver_icon(profile), size=20, color=ACCENT),
                ft.Text(profile.name, size=FS_CARD_TITLE,
                        weight=ft.FontWeight.BOLD, color=TEXT,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                        expand=True),
                *trailing,
            ],
            spacing=8,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # ------------------------------------------------------------- pages ----

    def _control_page(self) -> ft.Control:
        body = ft.Column(
            controls=[
                self._controls_section(),
                self._monitoring_section(),
                self._log_section(),
            ],
            spacing=18,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        return ft.Container(
            content=body, expand=True,
            padding=ft.Padding.only(right=6, bottom=8),
        )

    def _controls_section(self) -> ft.Control:
        cards = [
            self._device_card(profile)
            for profile in pins.PROFILE.values()
            if profile.role in (Role.OUTPUT, Role.PWM)
        ]
        all_off = ft.Button(
            content="ALL OFF", key="all-off", icon=ft.Icons.POWER_SETTINGS_NEW,
            bgcolor=DANGER, color="#FFFFFF", height=42,
            on_click=lambda e: self.page.run_task(self._all_off),
        )
        self.toggles.append(all_off)
        return self._section(
            ft.Icons.TUNE, "Controls",
            "switches · relays are active-low · sliders send on release",
            cards, all_off)

    def _device_card(self, profile: PinProfile) -> ft.Container:
        bcm = profile.bcm
        state = self.states.get(bcm) if bcm is not None else None
        rows: list[ft.Control] = [
            self._card_title(profile),
            ft.Text(self._pin_label(profile), size=FS_SMALL, color=TEXT_SUB),
        ]

        if profile.role is Role.OUTPUT and not profile.momentary:
            switch = ft.Switch(
                value=bool(state.value) if state and state.value else False,
                disabled=not self.connected,
                key=f"switch-{bcm}",
            )
            switch.on_change = (
                lambda e, p=profile: self._on_switch(p, e))
            self.switches[bcm] = switch
            self.toggles.append(switch)
            rows.append(ft.Row(
                controls=[
                    ft.Text("Power", size=FS_BODY, color=TEXT_SUB),
                    switch,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
            if profile.driver == "RGBLED":
                rows.append(self._rgb_swatches(profile))

        elif profile.role is Role.OUTPUT and profile.momentary:
            button = ft.Button(
                content=f"Pulse {PULSE_MS} ms", key=f"pulse-{bcm}",
                icon=ft.Icons.PLAY_ARROW,
                disabled=not self.connected,
                on_click=lambda e, p=profile: self.page.run_task(
                    self._pulse, p),
            )
            self.toggles.append(button)
            rows.append(button)
            if profile.driver == "TonalBuzzer":
                rows.append(self._tone_row(profile))

        else:  # PWM: dimmer or motor
            pct = float(state.value) if state and state.value else 0.0
            is_motor = profile.driver == "Motor"
            low, high = (-100.0, 100.0) if is_motor else (0.0, 100.0)
            label = ft.Text(f"{pct:.0f}%", size=FS_CARD_TITLE,
                            weight=ft.FontWeight.BOLD, color=ACCENT)
            slider = ft.Slider(
                value=pct, min=low, max=high,
                divisions=40 if is_motor else 20,
                label="{value}%", key=f"slider-{bcm}",
                disabled=not self.connected,
                on_change_start=lambda e, p=profile: self.pending.add(p.bcm),
                on_change=lambda e, p=profile: self._on_slider_live(p, e),
                on_change_end=lambda e, p=profile: self.page.run_task(
                    self._commit_pwm, p, e),
            )
            self.sliders[bcm] = slider
            self.slider_labels[bcm] = label
            self.toggles.append(slider)
            rows.append(ft.Row(
                controls=[
                    ft.Text("Duty" if not is_motor else "Speed",
                            size=FS_BODY, color=TEXT_SUB),
                    label,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ))
            rows.append(slider)

        return self._card(*rows)

    def _rgb_swatches(self, profile: PinProfile) -> ft.Row:
        """Preset colour tiles — set the LED in one tap (0xRRGGBB)."""
        tiles = []
        for name, packed in _RGB_SWATCHES:
            tile = ft.Container(
                width=28, height=28, border_radius=8,
                bgcolor=f"#{packed:06X}",
                border=ft.Border.all(2, "#B0BEC5"),
                tooltip=name,
                key=f"rgb-{profile.bcm}-{name}",
                disabled=not self.connected,
                on_click=lambda e, p=profile, c=packed: (
                    self.page.run_task(self._write_rgb, p, c)),
            )
            self.toggles.append(tile)
            tiles.append(tile)
        return ft.Row(controls=tiles, wrap=True, spacing=6, run_spacing=6)

    def _tone_row(self, profile: PinProfile) -> ft.Row:
        notes = []
        for note, hz in _TONES:
            button = ft.Button(
                content=note, key=f"tone-{profile.bcm}-{hz}",
                height=36,
                tooltip=f"{note} · {hz} Hz",
                disabled=not self.connected,
                on_click=lambda e, p=profile, f=hz: self.page.run_task(
                    self._tone, p, f),
            )
            self.toggles.append(button)
            notes.append(button)
        return ft.Row(controls=notes, wrap=True, spacing=6, run_spacing=6)

    def _monitoring_section(self) -> ft.Control:
        inputs = [
            self._input_card(profile) for profile in pins.PROFILE.values()
            if profile.role is Role.INPUT
        ]
        sensors = [
            self._sensor_card(profile) for profile in pins.PROFILE.values()
            if profile.role is Role.SENSOR
        ]
        if not inputs and not sensors:
            return ft.Column(controls=[])
        blocks: list[ft.Control] = [
            self._section_head(ft.Icons.MONITOR_HEART, "Monitoring",
                               "inputs are live · sensors polled on demand"),
        ]
        if inputs:
            blocks += [
                ft.Text("Inputs", size=FS_BODY,
                        weight=ft.FontWeight.BOLD, color=TEXT_SUB),
                ft.ResponsiveRow(
                    controls=inputs, spacing=12, run_spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START),
            ]
        if sensors:
            blocks += [
                ft.Text("Sensors", size=FS_BODY,
                        weight=ft.FontWeight.BOLD, color=TEXT_SUB),
                ft.ResponsiveRow(
                    controls=sensors, spacing=12, run_spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START),
            ]
        return ft.Column(controls=blocks, spacing=10)

    def _input_card(self, profile: PinProfile) -> ft.Container:
        state = self.states.get(profile.bcm)
        active = bool(state.value) if state and state.value else False
        dot = ft.Container(width=16, height=16, border_radius=8,
                           bgcolor="#FBC02D" if active else "#BDBDBD")
        label = ft.Text("active" if active else "idle", size=FS_BODY,
                        weight=ft.FontWeight.BOLD,
                        color=WARN if active else TEXT_SUB)
        self.input_dots[profile.bcm] = dot
        self.input_texts[profile.bcm] = label
        return self._card(
            self._card_title(profile),
            ft.Row(controls=[dot, label], spacing=8, tight=True),
            ft.Text(self._pin_label(profile), size=FS_SMALL,
                    color=TEXT_SUB),
            col={"xs": 6, "sm": 6, "md": 3, "lg": 3},
        )

    def _sensor_card(self, profile: PinProfile) -> ft.Container:
        state = self.states.get(profile.bcm)
        value = ft.Text("—", size=FS_VALUE, weight=ft.FontWeight.BOLD,
                        color=TEXT)
        note = ft.Text(self._pin_label(profile), size=FS_SMALL,
                       color=TEXT_SUB)
        if profile.needs_adc:
            note.value = "needs an ADC (not fitted)"
            note.color = DANGER
        self.sensor_values[profile.bcm] = value
        self.sensor_notes[profile.bcm] = note
        if state is not None and state.value is not None and not state.fault:
            value.value = _format_reading(state.value, state.unit)
        return self._card(
            self._card_title(profile),
            value,
            note,
            col={"xs": 6, "sm": 6, "md": 3, "lg": 3},
        )

    def _log_section(self) -> ft.Control:
        self.log_column = ft.Column(controls=self._log_texts(), spacing=4)
        self.log_body = ft.Container(
            content=self.log_column,
            visible=self.log_open,
            padding=12,
            border_radius=14,
            bgcolor=CARD_BG,
            border=ft.Border.all(1, CARD_BORDER),
        )
        self.log_toggle = ft.IconButton(
            icon=(ft.Icons.EXPAND_LESS if self.log_open
                  else ft.Icons.EXPAND_MORE),
            key="log-toggle",
            tooltip="Show/hide the activity log",
            on_click=self._toggle_log,
        )
        clear = ft.TextButton("Clear", key="log-clear", on_click=self._clear_log)
        return ft.Column(
            controls=[
                self._section_head(ft.Icons.RECEIPT_LONG, "Activity",
                                   "last 16 operations", clear,
                                   self.log_toggle),
                self.log_body,
            ],
            spacing=8,
        )

    def _toggle_log(self, e: ft.ControlEvent | None = None) -> None:
        self.log_open = not self.log_open
        if self.log_body is not None:
            self.log_body.visible = self.log_open
        if self.log_toggle is not None:
            self.log_toggle.icon = (
                ft.Icons.EXPAND_LESS if self.log_open
                else ft.Icons.EXPAND_MORE)
        self.page.update()

    def _clear_log(self, e: ft.ControlEvent | None = None) -> None:
        self.log.clear()
        if self.log_column is not None:
            self.log_column.controls = self._log_texts()
        self.page.update()

    # -------------------------------------------------------------- board ----

    def _legend(self) -> ft.Control:
        chips = [self._legend_chip(_FUNCTION_COLORS[f], _FUNCTION_LABELS[f])
                 for f in FunctionColor]
        chips += [
            self._legend_chip(OK, "ON / active"),
            self._legend_chip(TEXT_SUB, "OFF / idle"),
            self._legend_chip(ACCENT, "PWM %"),
            self._legend_chip("#4A148C", "sensor value"),
            self._legend_chip(DANGER, "fault"),
        ]
        return ft.Row(controls=chips, wrap=True, spacing=10, run_spacing=6)

    @staticmethod
    def _legend_chip(color: str, label: str) -> ft.Row:
        return ft.Row(
            controls=[
                ft.Container(width=10, height=10, border_radius=5,
                             bgcolor=color),
                ft.Text(label, size=FS_TINY, color=TEXT_SUB),
            ],
            spacing=4, tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _board_page(self) -> ft.Control:
        cells = [
            cell for cell in pins.HEADER
            if not self.wired_only or self._is_wired(cell)
        ]
        wired_switch = ft.Switch(
            label="Wired only", value=self.wired_only,
            key="board-wired-only", on_change=self._on_wired_only,
        )
        heading = ft.Row(
            controls=[
                ft.Icon(ft.Icons.DASHBOARD, size=22, color=ACCENT),
                ft.Text("Board", size=FS_SECTION, weight=ft.FontWeight.BOLD,
                        color=TEXT),
                ft.Text("40-pin header · tap a wired output to toggle",
                        size=FS_SMALL, color=TEXT_SUB),
            ],
            wrap=True, spacing=10, run_spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        body = ft.Column(
            controls=[
                heading,
                self._legend(),
                ft.Row(
                    controls=[
                        wired_switch,
                        ft.Text(f"{len(cells)} pins shown", size=FS_SMALL,
                                color=TEXT_SUB),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.ResponsiveRow(
                    controls=[self._pin_card(cell) for cell in cells],
                    spacing=10,
                    run_spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            spacing=12,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        return ft.Container(content=body, expand=True,
                            padding=ft.Padding.only(right=6, bottom=8))

    def _on_wired_only(self, e: ft.ControlEvent) -> None:
        self.wired_only = bool(e.control.value)
        self.page_index = self.PAGE_BOARD
        self._render()

    def _pin_card(self, cell: PinProfile) -> ft.Container:
        """One detailed header-pin card for the Board page."""
        profile = pins.PROFILE.get(cell.bcm) if cell.bcm is not None else None
        wired = self._is_wired(cell)
        ring = _FUNCTION_COLORS[cell.function]

        # #11  GPIO17        [family]
        head = ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text(f"#{cell.physical}", size=FS_TINY,
                                    weight=ft.FontWeight.BOLD, color=TEXT),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    border_radius=6, bgcolor=IDLE_BG,
                ),
                ft.Text(pins.header_label(cell), size=FS_BODY,
                        weight=ft.FontWeight.BOLD, color=TEXT,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                        expand=True),
                ft.Container(width=10, height=10, border_radius=5,
                             bgcolor=ring),
            ],
            spacing=6, tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        meta: list[str] = [_FUNCTION_LABELS[cell.function]]
        if cell.bcm is not None:
            meta.append(f"BCM {cell.bcm}")
        if cell.alt_function:
            meta.append(cell.alt_function)

        rows: list[ft.Control] = [
            head,
            ft.Text(" · ".join(meta), size=FS_TINY, color=TEXT_SUB,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
        ]

        if wired and profile is not None:
            rows.append(ft.Text(cell.name, size=FS_SMALL,
                                weight=ft.FontWeight.BOLD, color=TEXT,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS))
            state = self.states.get(profile.bcm)
            label, color, bgcolor = (
                _badge_for_state(state) if state is not None
                else ("—", TEXT_SUB, IDLE_BG))
            chip_text = ft.Text(label, size=FS_TINY,
                                weight=ft.FontWeight.BOLD, color=color)
            chip = ft.Container(
                content=chip_text,
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                border_radius=8, bgcolor=bgcolor,
            )
            self.pin_chips[profile.bcm] = (chip_text, chip)
            note = ft.Text(
                f"{profile.driver} · {state.direction}" if state is not None
                else profile.driver,
                size=FS_TINY, color=TEXT_SUB,
                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
            )
            self.pin_notes[profile.bcm] = note
            rows.append(ft.Row(controls=[chip, note], spacing=8, wrap=True,
                               run_spacing=4,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER))
        elif cell.claimed_by:
            rows.append(ft.Text(f"used by {cell.claimed_by}", size=FS_TINY,
                                color=TEXT_FAINT, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS))

        container = ft.Container(
            content=ft.Column(controls=rows, spacing=6),
            padding=12,
            border_radius=12,
            bgcolor=CARD_BG,
            border=ft.Border.all(1, CARD_BORDER),
            tooltip=self._cell_tooltip(cell),
            key=f"pin-{cell.physical}",
            col={"xs": 6, "sm": 4, "md": 4, "lg": 3},
        )
        if (wired and profile is not None and profile.role is Role.OUTPUT):
            container.ink = True
            container.on_click = lambda e, c=cell: self._on_cell_tap(c)
        return container

    def _board_strip(self) -> ft.Control:
        """Wide layout: the compact 2x20 pinout pinned above the tabs."""
        toggle = ft.IconButton(
            ft.Icons.EXPAND_LESS if self.board_open else ft.Icons.EXPAND_MORE,
            tooltip="Show/hide the board strip",
            key="board-toggle",
            on_click=self._toggle_board,
        )
        details = ft.TextButton(
            "Details", key="board-details",
            icon=ft.Icons.OPEN_IN_FULL,
            on_click=lambda e: self._go_to_page(self.PAGE_BOARD),
        )
        header = ft.Row(
            controls=[
                ft.Icon(ft.Icons.DEVELOPER_BOARD, size=22, color=ACCENT),
                ft.Column(
                    controls=[
                        ft.Text("Board — 40-pin header", size=FS_SECTION,
                                weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text("cell = #physical · BCM · live state",
                                size=FS_SMALL, color=TEXT_SUB),
                    ],
                    spacing=0, tight=True,
                ),
                details,
                toggle,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            spacing=8,
            run_spacing=4,
        )
        body = ft.Column(spacing=6)
        if self.board_open:
            body.controls.append(self._board_horizontal())
        return ft.Container(
            content=ft.Column(controls=[header, body], spacing=6),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border_radius=14,
            bgcolor=CARD_BG,
            border=ft.Border.all(1, CARD_BORDER),
        )

    def _toggle_board(self, e: ft.ControlEvent) -> None:
        self.board_open = not self.board_open
        self._render()

    def _board_horizontal(self) -> ft.Control:
        # The reference diagram rotated 90° clockwise: its left column
        # (1,3..39) becomes the top row and its right column (2,4..40) the
        # second row, with each column's first pins (1 and 2) at the RIGHT
        # end. Wide layout only — portrait uses the detailed Board page.
        odd = [pins.HEADER[i] for i in range(38, -1, -2)]   # 39,37..1
        even = [pins.HEADER[i] for i in range(39, -1, -2)]  # 40,38..2
        grid = ft.Column(
            controls=[
                ft.Row(controls=[self._board_cell(c) for c in odd],
                       spacing=2, tight=True),
                ft.Row(controls=[self._board_cell(c) for c in even],
                       spacing=2, tight=True),
            ],
            spacing=4,
            tight=True,
        )
        # One horizontal scroller for both rows, so they stay aligned.
        return ft.Row(controls=[grid], scroll=ft.ScrollMode.AUTO)

    @staticmethod
    def _is_wired(cell: PinProfile) -> bool:
        if cell.bcm is not None and pins.PROFILE.get(cell.bcm) is cell:
            return True
        return cell.name.endswith("(echo)") or bool(cell.claimed_by)

    def _cell_tooltip(self, cell: PinProfile) -> str:
        parts = [cell.name, f"physical #{cell.physical}"]
        if cell.bcm is not None:
            parts.append(f"BCM {cell.bcm}")
        if cell.alt_function:
            parts.append(cell.alt_function)
        if cell.claimed_by:
            parts.append(f"used by {cell.claimed_by}")
        return " · ".join(parts)

    def _cell_initial_badge(self, cell: PinProfile) -> tuple[str, str, str]:
        """(text, color, bgcolor) before any state arrives."""
        if not self._is_wired(cell):
            return "", TEXT, CARD_BG
        if cell.name.endswith("(echo)") or cell.claimed_by:
            return "aux", TEXT_SUB, IDLE_BG
        return cell.name, TEXT, CARD_BG

    @staticmethod
    def _strip_pin_label(cell: PinProfile) -> str:
        """Compact strip label: the BCM number, or the rail name (3V3/5V/GND).

        "GPIO17" would ellipsize in a 54 px cell at 13 px; the BCM number is
        the wiring-relevant part and the tooltip/Board page carry the rest.
        """
        return str(cell.bcm) if cell.bcm is not None else cell.name

    def _board_cell(self, cell: PinProfile) -> ft.Container:
        ring = _FUNCTION_COLORS[cell.function]
        badge_text, badge_color, badge_bg = self._cell_initial_badge(cell)
        badge = ft.Text(badge_text, size=FS_TINY, weight=ft.FontWeight.BOLD,
                        color=badge_color, text_align=ft.TextAlign.CENTER,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        container = ft.Container(
            width=54,
            height=52,
            border=ft.Border.all(2, ring),
            border_radius=8,
            bgcolor=badge_bg,
            alignment=ft.Alignment.CENTER,
            padding=2,
            tooltip=self._cell_tooltip(cell),
            key=f"cell-{cell.physical}",
            content=ft.Column(
                controls=[
                    ft.Text(f"#{cell.physical}", size=FS_TINY,
                            color=TEXT_SUB,
                            text_align=ft.TextAlign.CENTER, max_lines=1),
                    ft.Text(self._strip_pin_label(cell), size=FS_SMALL,
                            color=TEXT,
                            weight=ft.FontWeight.BOLD,
                            text_align=ft.TextAlign.CENTER, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    badge,
                ],
                spacing=0,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
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

    # ------------------------------------------------------------- script ----

    def _script_page(self) -> ft.Control:
        script = self.script
        status_text, status_color = self._script_status_text(script)
        enabled = bool(script and script.enabled)
        running = bool(script and script.running)

        self.script_status = ft.Text(status_text, size=FS_STATUS,
                                     weight=ft.FontWeight.BOLD,
                                     color=status_color)
        self.script_run = ft.Button(
            content="Run on Pi", key="script-run", icon=ft.Icons.PLAY_ARROW,
            bgcolor=OK, color="#FFFFFF", height=44,
            disabled=not (self.connected and enabled and not running),
            opacity=1.0 if (self.connected and enabled and not running) else 0.55,
            on_click=lambda e: self.page.run_task(self._run_script),
        )
        self.script_stop = ft.Button(
            content="Stop", key="script-stop", icon=ft.Icons.STOP,
            bgcolor=DANGER, color="#FFFFFF", height=44,
            disabled=not (self.connected and running),
            opacity=1.0 if (self.connected and running) else 0.55,
            on_click=lambda e: self.page.run_task(self._stop_script),
        )
        self.script_clear = ft.Button(
            content="Clear", key="script-clear", icon=ft.Icons.CLEAR,
            disabled=not (self.script_code or "").strip(),
            on_click=lambda e: self._clear_script(),
        )
        # Indent/outdent buttons: Tab does the same thing on a desktop
        # keyboard, but phones have no Tab key at all.
        self.script_indent = ft.Button(
            content="Indent", key="script-indent",
            icon=ft.Icons.FORMAT_INDENT_INCREASE,
            on_click=lambda e: self._indent_script(),
        )
        self.script_outdent = ft.Button(
            content="Outdent", key="script-outdent",
            icon=ft.Icons.FORMAT_INDENT_DECREASE,
            on_click=lambda e: self._indent_script(outdent=True),
        )
        examples = ft.PopupMenuButton(
            key="script-example",
            content=ft.Text("Examples", size=FS_BODY),
            icon=ft.Icons.CODE,
            tooltip="Insert an example",
            items=[
                ft.PopupMenuItem(
                    content=ft.Text(title, size=FS_BODY),
                    on_click=lambda e, c=code: self._insert_example(c),
                )
                for title, code in SCRIPT_EXAMPLES
            ],
        )

        field = ft.TextField(
            value=self.script_code,
            multiline=True,
            min_lines=8,
            max_lines=None,
            expand=True,
            label="Python script (runs on the Pi)",
            hint_text="import time; print('hello from the Pi')",
            text_style=ft.TextStyle(font_family="monospace", size=FS_CODE),
            border=ft.OutlineInputBorder(border_radius=10),
            key="script-code",
            # Always editable — you can compose while offline or while a job
            # runs; only Run/Stop are gated by connection + --allow-code.
        )
        field.on_change = self._on_script_edit
        field.on_selection_change = self._on_script_selection
        self.script_field = ft.KeyboardListener(
            content=field,
            key="script-keys",
            expand=True,
            on_key_down=self._on_script_key,
        )

        self.console_text = ft.Text(
            self._console_text(script),
            size=FS_CONSOLE,
            font_family="monospace",
            color=CONSOLE_FG,
            selectable=True,
        )
        self.console = ft.Column(controls=[self.console_text], expand=True,
                                 scroll=ft.ScrollMode.AUTO, spacing=0)
        console_box = ft.Container(
            content=self.console,
            expand=True,
            padding=12,
            border_radius=10,
            bgcolor=CONSOLE_BG,
        )
        console_head = ft.Row(
            controls=[
                ft.Icon(ft.Icons.TERMINAL, size=20, color=ACCENT),
                ft.Text("Console", size=FS_BODY,
                        weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text("stdout + stderr · newest 200 lines",
                        size=FS_SMALL, color=TEXT_SUB),
            ],
            spacing=8, wrap=True, run_spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        toolbar = ft.Column(
            controls=[
                ft.Row(
                    controls=[self.script_status, self.script_run,
                              self.script_stop],
                    wrap=True, spacing=10, run_spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    controls=[self.script_clear, self.script_indent,
                              self.script_outdent, examples],
                    wrap=True, spacing=8, run_spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=8,
        )

        editor_panel = ft.Column(
            controls=[
                ft.Text("Editor", size=FS_BODY, weight=ft.FontWeight.BOLD,
                        color=TEXT),
                self.script_field,
            ],
            spacing=6, expand=3,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        console_panel = ft.Column(
            controls=[console_head, console_box],
            spacing=6, expand=2,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        if self.narrow:
            work: ft.Control = ft.Column(
                controls=[editor_panel, console_panel],
                spacing=12, expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        else:
            work = ft.Row(
                controls=[editor_panel, console_panel],
                spacing=12, expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        return ft.Container(
            content=ft.Column(controls=[toolbar, work], spacing=12,
                              expand=True,
                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            expand=True,
        )

    @staticmethod
    def _script_status_text(script: ScriptStatus | None) -> tuple[str, str]:
        if script is None:
            return "waiting for the Pi…", TEXT_SUB
        if not script.enabled:
            return "requires pi.py --allow-code", DANGER
        if script.running:
            return f"running · job {script.job}", WARN
        if script.job:
            ok = script.exit == 0
            return (f"job {script.job} finished · exit {script.exit}",
                    OK if ok else DANGER)
        return "idle", TEXT_SUB

    @staticmethod
    def _console_text(script: ScriptStatus | None) -> str:
        if script is not None and script.output:
            return script.output
        return "(no output yet)"

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

    def _insert_example(self, code: str) -> None:
        """Insert a snippet at the caret (or over the current selection)."""
        field = self.script_text_field
        if field is None:
            return
        text = field.value or ""
        start, end = self.script_span
        if not (0 <= start <= end <= len(text)):
            start = end = min(self.script_caret, len(text))
        prefix = "" if not text or start == 0 or text[start - 1] == "\n" else "\n"
        block = prefix + code
        value = text[:start] + block + text[end:]
        caret = start + len(block)
        field.value = value
        field.selection = ft.TextSelection(base_offset=caret,
                                           extent_offset=caret)
        self.script_code = value
        self.script_caret = caret
        self.script_span = (caret, caret)
        if self.script_clear is not None:
            self.script_clear.disabled = not value.strip()
        self.page.update()

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
                # Caret inside the removed indentation would land before the
                # line start (negative offset): clamp it to the new line start.
                caret = max(line_start, start - remove)
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
        if result.ok and isinstance(result.value, int):
            # Reflect the new job at once instead of waiting for the next
            # poll: Run disables and Stop enables without a 1.5 s lag.
            self.script = ScriptStatus(
                enabled=True, running=True, job=result.value,
                output=(self.script.output if self.script else ""))
            self._apply_script()
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
        """Refresh Run/Stop/status/console from the latest polled status."""
        script = self.script
        enabled = bool(script and script.enabled)
        running = bool(script and script.running)
        if self.script_run is not None:
            can_run = bool(self.connected and enabled and not running)
            self.script_run.disabled = not can_run
            self.script_run.opacity = 1.0 if can_run else 0.55
        if self.script_stop is not None:
            can_stop = bool(self.connected and running)
            self.script_stop.disabled = not can_stop
            self.script_stop.opacity = 1.0 if can_stop else 0.55
        if self.script_status is not None:
            text, color = self._script_status_text(script)
            self.script_status.value = text
            self.script_status.color = color
        if self.console_text is not None:
            output = script.output if script is not None else ""
            self.console_text.value = output or "(no output yet)"
            self.console_text.color = CONSOLE_FG if output else "#7A8B99"

    def _scroll_console(self) -> None:
        """Follow new output — only called after the page has been updated."""
        if self._session_lost:
            return
        if self.console is None or self.console_text is None:
            return
        if not self.console_text.value or self.console_text.value.startswith(
                "(no output"):
            return
        self.page.run_task(self._scroll_console_task)

    async def _scroll_console_task(self) -> None:
        """Catch failures *inside* the task: Flet re-raises a failed task in
        its completion callback, which is what printed 'Session closed'
        tracebacks when the window was closed mid-poll."""
        if self.console is None:
            return
        try:
            await self.console.scroll_to(offset=-1.0)
        except Exception:  # noqa: BLE001 — session closed / control gone
            pass

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
