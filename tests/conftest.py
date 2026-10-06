"""Shared test setup — runs before any test module is imported.

Forces gpiozero's mock pin factory (with PWM support) so no test ever touches
real hardware, on any platform. The env vars must be in place before the first
gpiozero device is constructed: the pin factory reads them lazily.
"""

from __future__ import annotations

import os

os.environ.setdefault("GPIOZERO_PIN_FACTORY", "mock")
os.environ.setdefault("GPIOZERO_MOCK_PIN_CLASS", "mockpwmpin")
