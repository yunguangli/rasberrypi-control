"""Import-boundary test: fences the PC/Pi separation rule from plan.md §3.

  main.py          -> flet, stdlib, pins, protocol   (no gpiozero)
  pi.py            -> stdlib, gpiozero, pins, protocol (no flet)
  pins/protocol.py -> stdlib only

Missing files are skipped (files land as the build order progresses); once a
file exists, its boundary is enforced on every `uv run pytest`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
STDLIB = sys.stdlib_module_names  # Python >= 3.10

# filename -> allowed top-level import roots
BOUNDARY = {
    "main.py":     {"flet", "pins", "protocol"},
    "pi.py":       {"gpiozero", "pins", "protocol"},
    "pins.py":     set(),
    "protocol.py": set(),
}


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — flat siblings only, no packages
                pytest.fail(f"{path.name}:{node.lineno} uses a relative import")
            if node.module:
                found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("filename", sorted(BOUNDARY))
def test_import_boundary(filename):
    path = SRC / filename
    if not path.exists():
        pytest.skip(f"{filename} not created yet (build order)")

    imports = _top_level_imports(path)
    third_party = imports - STDLIB - {"__future__"}
    allowed_third_party = BOUNDARY[filename]
    forbidden = third_party - allowed_third_party
    assert not forbidden, (
        f"{filename} imports forbidden modules {sorted(forbidden)}; "
        f"allowed third-party: {sorted(allowed_third_party) or 'none'}"
    )


def test_main_py_never_imports_gpiozero():
    """The PC GUI talks HTTP only — gpiozero (and its pin factories) stays
    off the PC's runtime path except via pi.py --dev."""
    path = SRC / "main.py"
    if not path.exists():
        pytest.skip("main.py not created yet")
    assert "gpiozero" not in _top_level_imports(path)


def test_contract_modules_are_stdlib_only():
    """pins.py and protocol.py are the shared contract: they must import
    nothing outside the standard library — not even each other's siblings
    beyond stdlib, and never flet/gpiozero."""
    for filename in ("pins.py", "protocol.py"):
        path = SRC / filename
        if not path.exists():
            pytest.skip(f"{filename} not created yet")
        imports = _top_level_imports(path)
        third_party = imports - STDLIB - {"__future__"}
        assert not third_party, f"{filename} must be stdlib-only, found {third_party}"
