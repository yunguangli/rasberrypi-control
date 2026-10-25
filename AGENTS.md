# AGENTS.md

## What this is
Flet >=1.0 Python app (desktop + web, portrait-first for phones). Design brief: `plan.txt`;
authoritative implementation plan: `plan.md` (read before changing architecture). The PC-side
Flet GUI (`src/main.py`) sends whitelisted JSON ops over LAN HTTP to a Pi-side console bridge
(`src/pi.py`) that drives gpiozero. Transport decided: stdlib threaded webserver on the Pi.

**Current focus:** the typed agent plus the beginner sensor/actuator bench (light,
temperature, distance, motion, button, CPU temp/load). **Eventual goal:** the agent
controls the board and its gadgets by typed or spoken commands; cars/hands are future
skill layers (`plan.md` §10), and voice is a later front end to the same agent.

Build order (§8 of `plan.md`) is implemented:
- `src/pins.py` + `src/protocol.py` — the shared contract (pin profile + wire dataclasses),
  stdlib-only; import boundaries are enforced by `tests/test_import_boundary.py`.
- `src/pi.py` — the Pi-side bridge and the only file set copied to the Pi
  (`pi.py` + `pins.py` + `protocol.py`, flat siblings); `--dev` runs it with mock pins.
- `src/capabilities.py` + `src/agent.py` — the typed-natural-language layer (PC only,
  no flet/gpiozero): tool schema generated from `pins.PROFILE`, `ToolCompiler`
  (validate → `protocol.Op`), `Facts`, the `Agent` loop + ports, and an
  OpenAI-compatible `OpenAIChat` adapter. Typed text only for now; voice/MCP are seams.
- `src/main.py` — the GUI: a fixed header (target + status), four pages
  (**Board | Control | Script | Agent**) navigated by `Tabs` on wide screens and a bottom
  `NavigationBar` in portrait, a poll loop, an activity log, and a generous type scale.
  The Board page is the detailed 40-pin reference; Control holds the device cards
  (outputs/PWM + inputs/sensors); Script owns the editor + console; Agent is the
  transcript/composer page with action chips, question cards, and a hidden-by-default
  Pi traffic panel showing the exact `POST /op` JSON.

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
- Agent model (typed commands): configure on the Agent page's gear, or set
  `PICONTROL_LLM_BASE` / `PICONTROL_LLM_MODEL` / `PICONTROL_LLM_KEY`. Saved settings live
  at `~/.config/picontrol/agent.json` (mode 0600); learned aliases at
  `PICONTROL_FACTS_PATH` (default `~/.config/picontrol/facts.json`). Any tool-calling
  OpenAI-compatible endpoint works (OpenRouter, Ollama). Prefer a specific tool-capable
  model — `openrouter/free` is a router that picks a different free model per turn.
  Setup table: README → *Agent*.
- Pi target environment (the user's board): **Python 3.11 + gpiozero 1.6.2** with the old
  RPi.GPIO backend. The dev venv here runs gpiozero 2.x, so also run the Pi-side tests
  against 1.6.2 when touching `pi.py`:
  `uv venv --python 3.11 /tmp/opencode/gz162-py311 && uv pip install --python /tmp/opencode/gz162-py311/bin/python 'gpiozero==1.6.2' 'setuptools<81' pytest`,
  then `PYTHONPATH=src /tmp/opencode/gz162-py311/bin/python -m pytest tests/test_pi_dispatcher.py tests/test_pins.py tests/test_protocol.py tests/test_pi_deploy.py tests/test_import_boundary.py tests/test_agent.py -q`
  (the mock env vars come from `tests/conftest.py`; `test_agent.py` is stdlib-only, so it
  runs there too). Keep both versions green.

## Testing gotchas (verified on this machine)
- `tests/test_agent_page.py` is **headless**: it builds `App` against a stub `Page` and
  drives the worker/queue/settings flows without Flutter. Use it (plus `test_agent.py`)
  for Agent work on this box. It stubs `flet.run` only for the duration of the import.
- Tests are integration tests: the flet pytest plugin's `flet_app` fixture builds an app shell
  into `build/` and spawns `flutter test`. Needs the Flutter SDK on PATH; first run ~85 s.
- `tests/test_main.py` (the real Flutter host) needs two local toolchain workarounds on this
  headless Linux box, plus one test-API caveat; with them the whole suite passes (~100 s):
  1. The packaged Python pins `CXX=clang++ -pthread` and clang is not installed. Put
     `clang`/`clang++` shims that exec gcc/g++ on PATH, e.g.
     `mkdir -p /tmp/opencode/bin && printf '#!/bin/sh\nexec g++ "$@"\n' > /tmp/opencode/bin/clang++ && chmod +x /tmp/opencode/bin/clang++`
     (same for `clang` → `gcc`).
  2. The generated `build/flutter/pubspec.yaml` overrides `jni: 1.0.0`, but
     `jni_flutter 1.0.4+1` requires `jni ^1.1.0`: change that line to `jni: ^1.1.0`
     and run `/home/panger/flutter/3.44.8/bin/flutter pub get` in `build/flutter`.
     Then `PATH=/tmp/opencode/bin:$PATH .venv/bin/python -m pytest tests/test_main.py`.
     The provisioning cache survives until the Flutter template changes; if the jni
     error returns, redo step 2. Both are toolchain issues, not app bugs.
  3. `tests/test_main.py` cannot read widget properties: in flet 1.0 `find_by_key`
     returns a `Finder` with only `count`/`first`/`at` (no `controls`). Property
     assertions belong in the headless `tests/test_agent_page.py` instead.
- Known Windows failure: setup dies with `RuntimeError: Flutter process exited early with code 1`,
  caused by "Building with plugins requires symlink support. Please enable Developer Mode".
  Enable Windows Developer Mode (`start ms-settings:developers`) before expecting tests to pass.
- pytest config is in `pyproject.toml`: `testpaths=["tests"]`, `pythonpath=["src"]`,
  `asyncio_mode="auto"`.

## Layout / conventions
- GUI entrypoint `src/main.py` (`ft.run(main)`); `[tool.flet.app] path = "src"`. Not a package —
  imports resolve from `src/` via pytest `pythonpath`.
- Agent layering is enforced by `tests/test_import_boundary.py`: `capabilities.py` may import
  only `pins`/`protocol`; `agent.py` only `capabilities`/`protocol`. Never import flet or
  gpiozero there — the same tools must be reusable by an MCP server or a voice front end.
- Add a device in `pins.py` only; the tool schema, the Board cards and the Control cards all
  derive from `PROFILE`. Tool names are sanitised (`Light (ADC)` → `light_adc`).
- Provider quirks: `AssistantMessage.to_message()` must rebuild `tool_calls` from the parsed
  calls (synthesised ids, `json.dumps(arguments)`) instead of echoing the raw provider
  payload. Some free providers omit `tool_call.id` or truncate the arguments JSON, and
  OpenRouter rejects that in the *next* request with a 400 (`tool_call_id must be non-empty`).
  `tests/test_agent.py` has regression tests for both. Also surface
  `finish_reason=length` empty replies as errors — reasoning models can spend the whole
  output budget before emitting a tool call.
- The agent acts immediately (no approval step); `write_script` only offers code that the
  operator loads into the editor. Keep that split: never auto-run model-generated code.
- `src/assets/icon.png` is the icon for every platform: keep it transparent and full-canvas
  (rationale in the `[tool.flet]` comment in `pyproject.toml`).
- Give interactive controls a `key=`; tests locate them with `tester.find_by_key(...)`.
- Use the Flet MCP tools for API/syntax verification (requested in `plan.txt`; flet-mcp is
  installed in the shared venv).
- `.flet/` and `build/` are gitignored runtime artifacts — never edit or commit them.
  (Exception: the local `jni` workaround above edits `build/flutter/pubspec.yaml`; it is
  a generated file and is never committed.)

## Not configured
- No lint, formatter, typecheck, codegen, CI, or pre-commit config exists — don't guess commands.
- Git repo with a single `first commit` (it is version-controlled as of 2026-10-07).
