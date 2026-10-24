"""GUI smoke test: the whole view is generated from pins.HEADER/PROFILE.

Runs against the real `src/main.py` (device mode, flet pytest plugin). No Pi
is reachable in CI, so the app starts disconnected — everything must still
render, and the pages must be reachable in both layouts (wide: Tabs, narrow:
bottom NavigationBar).
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


async def _tap_if_present(tester, key: str) -> bool:
    """Tap a control if this layout renders it; report whether it did."""
    found = await tester.find_by_key(key)
    if not found.count:
        return False
    await tester.tap(found)
    await tester.pump_and_settle()
    return True


async def _go_to(tester, wide_key: str, narrow_key: str) -> None:
    """Switch pages whether the layout uses tabs (wide) or the nav bar."""
    if not await _tap_if_present(tester, wide_key):
        await _tap_if_present(tester, narrow_key)


async def test_gui_renders_from_profile(flet_app: ftt.FletTestApp):
    tester = flet_app.tester

    await tester.pump_and_settle()

    # Connection bar
    assert (await tester.find_by_key("host")).count == 1
    assert (await tester.find_by_key("connect")).count == 1
    assert (await tester.find_by_key("preset")).count == 1

    # Control page: ALL OFF + latching outputs (BCM 17/22/27/19)
    assert (await tester.find_by_key("all-off")).count == 1
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

    # Every wired device renders its card (single source of truth: pins.WIRED)
    for name in WIRED_NAMES:
        assert (await tester.find_by_text(name)).count >= 1, name

    # Activity log controls
    assert (await tester.find_by_key("log-toggle")).count == 1
    assert (await tester.find_by_key("log-clear")).count == 1

    # Script page (its own tab / nav destination now)
    await _go_to(tester, "tab-script", "nav-script")
    if not (await tester.find_by_key("script-code")).count:
        # Fallback: tap the visible tab label (some hosts key the TabBar
        # differently from the Tab control).
        label = await tester.find_by_text("Script")
        if label.count:
            await tester.tap(label)
            await tester.pump_and_settle()
    assert (await tester.find_by_key("script-code")).count == 1
    assert (await tester.find_by_key("script-keys")).count == 1
    assert (await tester.find_by_key("script-run")).count == 1
    assert (await tester.find_by_key("script-stop")).count == 1
    assert (await tester.find_by_key("script-clear")).count == 1
    assert (await tester.find_by_key("script-indent")).count == 1
    assert (await tester.find_by_key("script-outdent")).count == 1
    assert (await tester.find_by_key("script-example")).count == 1

    # Board page carries the detailed pin cards
    await _go_to(tester, "tab-board", "nav-board")
    assert (await tester.find_by_key("pin-11")).count == 1
    assert (await tester.find_by_key("pin-1")).count == 1
    assert (await tester.find_by_key("board-wired-only")).count == 1

    await _go_to(tester, "tab-control", "nav-control")

    # Wide layout: the pinned Board strip collapses / re-expands
    if (await tester.find_by_key("board-toggle")).count:
        assert (await tester.find_by_key("cell-11")).count == 1
        await tester.tap(await tester.find_by_key("board-toggle"))
        await tester.pump_and_settle()
        assert (await tester.find_by_key("cell-11")).count == 0

        await tester.tap(await tester.find_by_key("board-toggle"))
        await tester.pump_and_settle()
        assert (await tester.find_by_key("cell-11")).count == 1
