"""Math utilities."""
from __future__ import annotations

import math


def pct_diff(a: float, b: float) -> float:
    """Percentage difference between a and b, relative to a."""
    if a == 0:
        return 0.0
    return abs(a - b) / a * 100


def round_sig(x: float, sig: int = 4) -> float:
    if x == 0:
        return 0.0
    d = math.ceil(math.log10(abs(x)))
    power = sig - d
    factor = 10**power
    return round(x * factor) / factor
