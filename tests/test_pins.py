"""Unit tests for src/pins.py — the shared pin profile.

Pure data tests: no gpiozero, no network, no Pi needed.
"""

from __future__ import annotations

import pins
from pins import FunctionColor, PinProfile, Role


def test_header_has_all_40_positions():
    assert len(pins.HEADER) == 40
    assert sorted(c.physical for c in pins.HEADER) == list(range(1, 41))


def test_header_canonical_mappings():
    """Spot-check against the real 40-pin header — the user's reference
    drawing had pins 12/14 swapped, so pin this down explicitly."""
    by_phys = {c.physical: c for c in pins.HEADER}
    # phys 12 is wired (Desk dimmer), so its *name* is the friendly device
    # name — assert on the BCM, which is the invariant.
    assert by_phys[12].bcm == 18
    assert by_phys[14].name == "GND" and by_phys[14].role is Role.GND
    assert by_phys[1].name == "3V3" and by_phys[1].role is Role.POWER
    assert by_phys[40].name == "GPIO21" and by_phys[40].bcm == 21
    assert by_phys[27].name == "ID_SD" and by_phys[27].role is Role.ID
    assert by_phys[3].function is FunctionColor.I2C
    assert by_phys[19].function is FunctionColor.SPI
    assert by_phys[8].function is FunctionColor.UART


def test_header_bcm_mapping_well_known_pins():
    """Physical->BCM for pins people wire by habit — catches row/parity
    mistakes in the static table. (Physical n != BCM n in general: the
    offset only becomes 1:1 in the middle block.)"""
    by_phys = {c.physical: c for c in pins.HEADER}
    expected = {
        3: 2, 5: 3, 7: 4,          # I2C block start: phys = bcm + 1
        11: 17, 13: 27, 15: 22, 16: 23, 18: 24,
        29: 5, 31: 6, 32: 12, 33: 13,
        35: 19, 36: 16, 37: 26, 38: 20, 40: 21,
    }
    for phys, bcm in expected.items():
        assert by_phys[phys].bcm == bcm, (
            f"physical {phys} should be BCM {bcm}, "
            f"got {by_phys[phys].bcm} ({by_phys[phys].name})")


def test_wired_bcms_are_unique_and_on_header():
    bcm_seen: dict[int, str] = {}
    phys_seen: dict[int, str] = {}
    for dev in pins.WIRED:
        if dev.physical is None:
            continue  # ADC sensor has no header position
        assert dev.bcm not in bcm_seen, f"BCM {dev.bcm} wired twice"
        bcm_seen[dev.bcm] = dev.name
        assert dev.physical not in phys_seen, f"physical {dev.physical} wired twice"
        phys_seen[dev.physical] = dev.name
        # every wired primary IS the header cell at its position (shared object)
        cell = pins.HEADER[dev.physical - 1]
        assert cell is dev


def test_light_sensor_has_no_header_position():
    light = pins.PROFILE[-1]
    assert light.needs_adc is True
    assert light.bcm == -1 and light.physical is None
    assert all(c is not light for c in pins.HEADER)


def test_echo_pin_is_claimed_on_header():
    """HC-SR04's echo pin occupies physical 31 (BCM 6), shown as claimed."""
    echo_cells = [c for c in pins.HEADER if "(echo)" in c.name]
    assert len(echo_cells) == 1
    echo = echo_cells[0]
    assert echo.bcm == 6 and echo.physical == 31
    assert echo.role is Role.SENSOR
    assert echo.name.startswith("Distance")


def test_every_wired_device_has_a_role_with_ops():
    for dev in pins.WIRED:
        assert pins.ops_for(dev.role), f"{dev.name} ({dev.role}) allows no ops"


def test_sensors_only_allow_read_and_inputs_only_readback():
    assert pins.ops_for(Role.SENSOR) == frozenset({"read"})
    assert pins.ops_for(Role.INPUT) == {"digital_read", "set_input"}
    assert "pwm_write" not in pins.ops_for(Role.OUTPUT)
    assert "digital_write" not in pins.ops_for(Role.INPUT)


def test_inert_roles_allow_nothing():
    for role in (Role.FREE, Role.POWER, Role.GND, Role.ID):
        assert pins.ops_for(role) == frozenset()


def test_adding_a_pin_is_one_line(tmp_path):
    """Sanity: PROFILE keys mirror WIRED — adding an entry lands in both."""
    assert set(pins.PROFILE.values()) == set(pins.WIRED)
    assert len(pins.PROFILE) == len(pins.WIRED)  # no duplicate keys collapsing


def test_header_label_works_for_wired_and_inert_cells():
    by_phys = {c.physical: c for c in pins.HEADER}
    # wired cell still shows its intrinsic header label
    assert pins.header_label(by_phys[12]) == "GPIO18"
    assert pins.header_label(by_phys[11]) == "GPIO17"
    # free cells too
    assert pins.header_label(by_phys[7]) == "GPIO4"
    # power / GND / ID keep their own names
    assert pins.header_label(by_phys[1]) == "3V3"
    assert pins.header_label(by_phys[6]) == "GND"
    assert pins.header_label(by_phys[27]) == "ID_SD"
