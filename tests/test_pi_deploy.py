"""Deploy-smoke test: executes the actual copy-to-Pi workflow.

Copies the 3-file set (pi.py + pins.py + protocol.py) into a temp dir — flat,
no src/, no project config — and imports it there. If the Pi set ever stops
being self-contained, this fails on every `uv run pytest` instead of on the
Pi.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
COPY_SET = ("pi.py", "pins.py", "protocol.py")


@pytest.mark.parametrize("filename", COPY_SET)
def test_copy_set_exists(filename):
    assert (SRC / filename).exists(), f"{filename} missing from src/"


def test_flat_copy_set_imports_anywhere(tmp_path):
    for name in COPY_SET:
        shutil.copy(SRC / name, tmp_path / name)

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["GPIOZERO_PIN_FACTORY"] = "mock"
    env["GPIOZERO_MOCK_PIN_CLASS"] = "mockpwmpin"

    proc = subprocess.run(
        [sys.executable, "-c", "import pi; print('import-ok')"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"the Pi copy set is not self-contained:\n{proc.stderr}")
    assert "import-ok" in proc.stdout


def test_copy_set_has_no_stray_imports(tmp_path):
    """After copying, nothing outside the 3 files should be needed — verify
    pins/protocol import cleanly with only stdlib available in their scope."""
    for name in ("pins.py", "protocol.py"):
        shutil.copy(SRC / name, tmp_path / name)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", "import pins, protocol; print('contract-ok')"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "contract-ok" in proc.stdout
