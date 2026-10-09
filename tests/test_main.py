"""GUI smoke test: the whole view is generated from pins.HEADER/PROFILE.

Runs against the real `src/main.py` (device mode, flet pytest plugin). No Pi
is reachable in CI, so the app starts disconnected — everything must still
render, and the Board strip must toggle.
"""

import flet.testing as ftt

WIRED_NAMES = (
    "Status LED",
    "Relay 1",
    "Relay 2",
    "RGB LED",
    "Buzzer",
    "Door button",
    "PIR sensor",
    "Room temp",
    "Distance",
    "Light (ADC)",
    "CPU temp",
    "System load",
    "Desk dimmer",
    "Motor",
)


async def test_gui_renders_from_profile(flet_app: ftt.FletTestApp):
    tester = flet_app.tester

    await tester.pump_and_settle()

    # Connection bar + section controls
    assert (await tester.find_by_key("host")).count == 1
    assert (await tester.find_by_key("connect")).count == 1
    assert (await tester.find_by_key("board-toggle")).count == 1
    assert (await tester.find_by_key("all-off")).count == 1

    # Latching output switches (BCM 17/22/27/19)
    for bcm in (17, 22, 27, 19):
        assert (await tester.find_by_key(f"switch-{bcm}")).count == 1

    # Momentary buzzer button + PWM sliders (BCM 18/13)
    assert (await tester.find_by_key("pulse-23")).count == 1
    for bcm in (18, 13):
        assert (await tester.find_by_key(f"slider-{bcm}")).count == 1

    # Motor slider spans reverse..forward, dimmer stays 0..100
    motor = await tester.find_by_key("slider-13")
    dimmer = await tester.find_by_key("slider-18")
    assert motor.controls[0].min == -100 and motor.controls[0].max == 100
    assert dimmer.controls[0].min == 0 and dimmer.controls[0].max == 100

    # RGB swatches + buzzer note buttons render
    assert (await tester.find_by_key("rgb-19-red")).count == 1
    assert (await tester.find_by_key("tone-23-440")).count == 1

    # Script section (input + run/stop, gated until the bridge allows code)
    assert (await tester.find_by_key("script-code")).count == 1
    assert (await tester.find_by_key("script-run")).count == 1
    assert (await tester.find_by_key("script-stop")).count == 1

    # Every wired device renders its card (single source of truth: pins.WIRED)
    for name in WIRED_NAMES:
        assert (await tester.find_by_text(name)).count >= 1, name

    # Board strip: header cell for the Status LED position (physical #11)
    assert (await tester.find_by_key("cell-11")).count == 1

    # Collapse / re-expand the Board strip
    await tester.tap(await tester.find_by_key("board-toggle"))
    await tester.pump_and_settle()
    assert (await tester.find_by_key("cell-11")).count == 0

    await tester.tap(await tester.find_by_key("board-toggle"))
    await tester.pump_and_settle()
    assert (await tester.find_by_key("cell-11")).count == 1
