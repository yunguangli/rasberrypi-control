# AGENTS.md

## What this is
Flet >=1.0 Python app (desktop + web, portrait-first for phones). Design brief: `plan.txt`;
authoritative implementation plan: `plan.md` (read before changing architecture). The PC-side
Flet GUI (`src/main.py`) sends whitelisted JSON ops over LAN HTTP to a Pi-side console bridge
(`src/pi.py`) that drives gpiozero. Transport decided: stdlib threaded webserver on the Pi.

Build order (§8 of `plan.md`) is implemented:
- `src/pins.py` + `src/protocol.py` — the shared contract (pin profile + wire dataclasses),
  stdlib-only; import boundaries are enforced by `tests/test_import_boundary.py`.
- `src/pi.py` — the Pi-side bridge and the only file set copied to the Pi
  (`pi.py` + `pins.py` + `protocol.py`, flat siblings); `--dev` runs it with mock pins.
- `src/main.py` — the GUI: a fixed header (target + status), three pages
  (**Board | Control | Script**) navigated by `Tabs` on wide screens and a bottom
  `NavigationBar` in portrait, a poll loop, an activity log, and a generous type scale.
  The Board page is the detailed 40-pin reference; Control holds the device cards
  (outputs/PWM + inputs/sensors); Script owns the editor + console.

## Commands (run from repo root; the path contains spaces — quote it)
- Run desktop: `uv run flet run`
- Run web: `uv run flet run --web`
- Build: `flet build windows -v` (also: apk/ipa/macos/linux/web) — see README.
  Bare `flet` resolves to the shared venv `..\..\coding\venv` (flet 1.0.3 + flet-cli), not a
  project env. `uv run flet …` is the reproducible form — the project has its own
  `.venv` + `uv.lock` (flet 1.0.3, dev group: flet-cli/desktop/web, flet[test], gpiozero),
  which `uv run` keeps in sync.
- Tests: `uv run pytest`; single test: `uv run pytest tests/test_main.py::test_gui_renders_from_profile`.
  `flet test` runs the same suite.
- Pi side on this PC (no hardware): `uv run python src/pi.py --dev` — mock pins +
  synthetic sensors; the dev-only `set_input` op simulates button/PIR levels. Live mode
  without `--dev` exits with a hint (expected on a PC). Details: `plan.md` §5.
- Pi target environment (the user's board): **Python 3.11 + gpiozero 1.6.2** with the old
  RPi.GPIO backend. The dev venv here runs gpiozero 2.x, so also run the Pi-side tests
  against 1.6.2 when touching `pi.py`:
  `uv venv --python 3.11 /tmp/opencode/gz162-py311 && uv pip install --python /tmp/opencode/gz162-py311/bin/python 'gpiozero==1.6.2' 'setuptools<81' pytest`,
  then `PYTHONPATH=src /tmp/opencode/gz162-py311/bin/python -m pytest tests/test_pi_dispatcher.py tests/test_pins.py tests/test_protocol.py tests/test_pi_deploy.py tests/test_import_boundary.py -q`
  (the mock env vars come from `tests/conftest.py`). Keep both versions green.

## Testing gotchas (verified on this machine)
- Tests are integration tests: the flet pytest plugin's `flet_app` fixture builds an app shell
  into `build/` and spawns `flutter test`. Needs the Flutter SDK on PATH; first run ~85 s.
- On the current headless Linux box `tests/test_main.py` errors at fixture setup: the Flutter
  Linux host build fails because the environment points `CXX` at `clang++`, which is not
  installed (`CMakeDetermineCXXCompiler`: "Could not find the compiler … clang++"). The
  Pi-side suites (`test_pi_dispatcher`, `test_pins`, `test_protocol`, `test_pi_deploy`,
  `test_import_boundary`) run fine and are the ones to use for `pi.py` work; run the GUI
  test on a desktop session (or install `clang`).
- Known Windows failure: setup dies with `RuntimeError: Flutter process exited early with code 1`,
  caused by "Building with plugins requires symlink support. Please enable Developer Mode".
  Enable Windows Developer Mode (`start ms-settings:developers`) before expecting tests to pass.
- pytest config is in `pyproject.toml`: `testpaths=["tests"]`, `pythonpath=["src"]`,
  `asyncio_mode="auto"`.

## Layout / conventions
- GUI entrypoint `src/main.py` (`ft.run(main)`); `[tool.flet.app] path = "src"`. Not a package —
  imports resolve from `src/` via pytest `pythonpath`.
- `src/assets/icon.png` is the icon for every platform: keep it transparent and full-canvas
  (rationale in the `[tool.flet]` comment in `pyproject.toml`).
- Give interactive controls a `key=`; tests locate them with `tester.find_by_key(...)`.
- Use the Flet MCP tools for API/syntax verification (requested in `plan.txt`; flet-mcp is
  installed in the shared venv).
- `.flet/` and `build/` are gitignored runtime artifacts — never edit or commit them.

## Not configured
- No lint, formatter, typecheck, codegen, CI, or pre-commit config exists — don't guess commands.
- Git repo with a single `first commit` (it is version-controlled as of 2026-10-07).
