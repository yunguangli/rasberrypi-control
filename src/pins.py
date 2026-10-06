"""Pin profile: the single source of truth for wired GPIO.

Feeds BOTH sides: pi.py validates ops against ROLE_OPS/PROFILE, and the GUI
renders controls from HEADER/PROFILE. Stdlib-only (dataclasses/enum/typing) —
no flet, no gpiozero. Import boundary is enforced by tests/test_import_boundary.py.

Deployment note: this file is part of the 3-file copy set for the Pi
(pi.py + pins.py + protocol.py, flat siblings).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    """What a pin is wired as — decides allowed ops and which GUI card renders."""

    FREE = "free"        # GPIO cell with no device attached
    OUTPUT = "output"    # LED, relay, buzzer — we drive it
    INPUT = "input"      # button, PIR — we read it
    PWM = "pwm"          # dimmer, motor speed
    SENSOR = "sensor"    # DHT22, distance, (ADC-backed light)
    POWER = "power"      # 5V / 3V3 — inert
    GND = "gnd"          # inert
    ID = "id"            # EEPROM — inert


class FunctionColor(str, Enum):
    """Intrinsic header function — the Board strip's static ring colour."""

    GPIO = "gpio"
    POWER = "power"
    GND = "gnd"
    UART = "uart"
    SPI = "spi"
    I2C = "i2c"
    ID = "id"


@dataclass(frozen=True)
class PinProfile:
    """One header position / device. Frozen: contract data, never mutated.

    `physical` is None for devices with no header position (e.g. the
    ADC-backed light sensor); such devices use sentinel `bcm=-1` so PROFILE
    stays int-keyed while never matching a real pin.
    """

    name: str                          # display name: "Status LED", "GPIO17", "5V"
    bcm: int | None                    # BCM number for gpiozero (None = not on header)
    physical: int | None               # header position 1..40 (None = not on header)
    role: Role
    function: FunctionColor = FunctionColor.GPIO   # ring colour on the Board strip
    alt_function: str = ""             # "SPI0_MOSI", "SDA1", "PCM_CLK" ...
    unit: str = ""                     # "°C", "m", "lx", "%"
    driver: str = ""                   # gpiozero class hint: "LED", "Buzzer", "DHT22" ...
    active_low: bool = False           # relay boards energised on LOW
    needs_adc: bool = False            # render disabled until an ADC exists
    momentary: bool = False            # press-and-hold output (buzzer) vs latching switch
    pull_up: bool = False              # INPUT: Button(pull_up=...)
    echo: int | None = None            # SENSOR: second BCM pin (HC-SR04 echo)


# --------------------------------------------------------------------------
# Static 40-pin header table (physical, label, bcm, function family, alt fn).
# Generated data, never transcribed per-view: fixes the pin-12/14 swap in the
# user's reference drawing (real header: 12 = GPIO18, 14 = GND).
# --------------------------------------------------------------------------
_STATIC: tuple[tuple[int, str, int | None, FunctionColor, str], ...] = (
    (1, "3V3", None, FunctionColor.POWER, ""),
    (2, "5V", None, FunctionColor.POWER, ""),
    (3, "GPIO2", 2, FunctionColor.I2C, "SDA1"),
    (4, "5V", None, FunctionColor.POWER, ""),
    (5, "GPIO3", 3, FunctionColor.I2C, "SCL1"),
    (6, "GND", None, FunctionColor.GND, ""),
    (7, "GPIO4", 4, FunctionColor.GPIO, ""),
    (8, "GPIO14", 14, FunctionColor.UART, "UART0_TXD"),
    (9, "GND", None, FunctionColor.GND, ""),
    (10, "GPIO15", 15, FunctionColor.UART, "UART0_RXD"),
    (11, "GPIO17", 17, FunctionColor.GPIO, ""),
    (12, "GPIO18", 18, FunctionColor.GPIO, "PCM_CLK"),
    (13, "GPIO27", 27, FunctionColor.GPIO, ""),
    (14, "GND", None, FunctionColor.GND, ""),
    (15, "GPIO22", 22, FunctionColor.GPIO, ""),
    (16, "GPIO23", 23, FunctionColor.GPIO, ""),
    (17, "3V3", None, FunctionColor.POWER, ""),
    (18, "GPIO24", 24, FunctionColor.GPIO, ""),
    (19, "GPIO10", 10, FunctionColor.SPI, "SPI0_MOSI"),
    (20, "GND", None, FunctionColor.GND, ""),
    (21, "GPIO9", 9, FunctionColor.SPI, "SPI0_MISO"),
    (22, "GPIO25", 25, FunctionColor.GPIO, ""),
    (23, "GPIO11", 11, FunctionColor.SPI, "SPI0_SCLK"),
    (24, "GPIO8", 8, FunctionColor.SPI, "SPI0_CE0_N"),
    (25, "GND", None, FunctionColor.GND, ""),
    (26, "GPIO7", 7, FunctionColor.SPI, "SPI0_CE1_N"),
    (27, "ID_SD", None, FunctionColor.ID, "I2C ID EEPROM"),
    (28, "ID_SC", None, FunctionColor.ID, "I2C ID EEPROM"),
    (29, "GPIO5", 5, FunctionColor.GPIO, ""),
    (30, "GND", None, FunctionColor.GND, ""),
    (31, "GPIO6", 6, FunctionColor.GPIO, ""),
    (32, "GPIO12", 12, FunctionColor.GPIO, ""),
    (33, "GPIO13", 13, FunctionColor.GPIO, ""),
    (34, "GND", None, FunctionColor.GND, ""),
    (35, "GPIO19", 19, FunctionColor.GPIO, ""),
    (36, "GPIO16", 16, FunctionColor.GPIO, ""),
    (37, "GPIO26", 26, FunctionColor.GPIO, ""),
    (38, "GPIO20", 20, FunctionColor.GPIO, ""),
    (39, "GND", None, FunctionColor.GND, ""),
    (40, "GPIO21", 21, FunctionColor.GPIO, ""),
)


# --------------------------------------------------------------------------
# Wired devices — the deployable set. Add a pin = append one line here; the
# GUI card and the pi.py validation pick it up automatically.
# Placeholders until real wiring is confirmed; avoids I2C (2/3), SPI (7-11),
# UART (14/15) and EEPROM (0/1) pins.
# --------------------------------------------------------------------------
WIRED: tuple[PinProfile, ...] = (
    PinProfile(name="Status LED", bcm=17, physical=11, role=Role.OUTPUT, driver="LED"),
    PinProfile(name="Relay 1", bcm=22, physical=15, role=Role.OUTPUT,
               active_low=True, driver="LED"),
    PinProfile(name="Relay 2", bcm=27, physical=13, role=Role.OUTPUT,
               active_low=True, driver="LED"),
    PinProfile(name="Buzzer", bcm=23, physical=16, role=Role.OUTPUT,
               momentary=True, driver="Buzzer"),
    PinProfile(name="Door button", bcm=24, physical=18, role=Role.INPUT,
               pull_up=True, driver="Button"),
    PinProfile(name="PIR sensor", bcm=25, physical=22, role=Role.INPUT,
               driver="MotionSensor"),
    PinProfile(name="Room temp", bcm=4, physical=7, role=Role.SENSOR,
               unit="°C", driver="DHT22"),
    PinProfile(name="Distance", bcm=5, physical=29, role=Role.SENSOR,
               unit="m", driver="HCSR04", echo=6),
    # No header position: lives behind an ADC (MCP3008/ADS1115) that doesn't
    # exist yet — rendered disabled, reads rejected with a clear fault.
    # Sentinel bcm=-1 (never a real pin) keeps PROFILE int-keyed so dispatch
    # can produce the "needs an ADC" error instead of "not in the profile".
    PinProfile(name="Light (ADC)", bcm=-1, physical=None, role=Role.SENSOR,
               unit="lx", driver="LDR", needs_adc=True),
    PinProfile(name="Desk dimmer", bcm=18, physical=12, role=Role.PWM,
               unit="%", driver="PWMLED"),
    PinProfile(name="Motor speed", bcm=13, physical=33, role=Role.PWM,
               unit="%", driver="PWMLED"),
)


def _build_header() -> tuple[PinProfile, ...]:
    cells: list[PinProfile] = []
    for physical, label, bcm, function, alt in _STATIC:
        if label == "GND":
            role = Role.GND
        elif label in ("5V", "3V3"):
            role = Role.POWER
        elif function is FunctionColor.ID:
            role = Role.ID
        else:
            role = Role.FREE
        cells.append(PinProfile(name=label, bcm=bcm, physical=physical,
                                role=role, function=function, alt_function=alt))

    # Overlay wired devices onto their header positions (identity is shared:
    # HEADER[phys-1] IS the WIRED entry, so both views show one object).
    seen_bcm: dict[int, str] = {}
    for dev in WIRED:
        if dev.bcm is None or dev.physical is None:
            continue  # not on the header (ADC sensor)
        if dev.bcm in seen_bcm:
            raise ValueError(f"BCM {dev.bcm} wired twice: {seen_bcm[dev.bcm]} and {dev.name}")
        seen_bcm[dev.bcm] = dev.name
        cell = cells[dev.physical - 1]
        if cell.bcm != dev.bcm:
            raise ValueError(
                f"{dev.name}: physical pin {dev.physical} is {cell.name}, not BCM {dev.bcm}")
        cells[dev.physical - 1] = dev

    # Secondary pins (HC-SR04 echo) also occupy a header cell, shown claimed.
    for dev in WIRED:
        if dev.echo is None:
            continue
        if dev.echo in seen_bcm:
            raise ValueError(f"echo BCM {dev.echo} collides with {seen_bcm[dev.echo]}")
        echo_cell = next((c for c in cells if c.bcm == dev.echo), None)
        if echo_cell is None:
            raise ValueError(f"echo BCM {dev.echo} not found on the 40-pin header")
        cells[echo_cell.physical - 1] = PinProfile(
            name=f"{dev.name} (echo)", bcm=dev.echo, physical=echo_cell.physical,
            role=Role.SENSOR, function=echo_cell.function,
            alt_function=echo_cell.alt_function, driver=dev.driver)

    return tuple(cells)


# All 40 header positions, index physical-1. GUI Board strip renders these.
HEADER: tuple[PinProfile, ...] = _build_header()

# Wired devices keyed by BCM. The ADC light sensor sits under sentinel key -1
# (not a real pin): reachable from the wire for a clear "needs an ADC" error,
# never present on HEADER (physical is None). Cards iterate .values().
PROFILE: dict[int, PinProfile] = {dev.bcm: dev for dev in WIRED}

# Role -> ops pi.py may dispatch. Roles absent from wire traffic (POWER/GND/ID)
# and FREE allow nothing. system ops (noop/all_off) bypass this table and are
# defined in protocol.py; tests assert every value here is a known wire op.
ROLE_OPS: dict[Role, frozenset[str]] = {
    Role.FREE: frozenset(),
    Role.OUTPUT: frozenset({"digital_write", "pulse", "digital_read"}),
    Role.INPUT: frozenset({"digital_read", "set_input"}),
    Role.PWM: frozenset({"pwm_write", "digital_read"}),
    Role.SENSOR: frozenset({"read"}),
    Role.POWER: frozenset(),
    Role.GND: frozenset(),
    Role.ID: frozenset(),
}


def ops_for(role: Role) -> frozenset[str]:
    return ROLE_OPS[role]


def header_label(cell: PinProfile) -> str:
    """Intrinsic header label for a Board strip cell: "GPIO18" for any GPIO
    position (even when wired — `name` then carries the friendly device
    name), the cell's own name for power/GND/ID."""
    return f"GPIO{cell.bcm}" if cell.bcm is not None else cell.name
