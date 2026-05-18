"""ID generation utilities."""
from __future__ import annotations

import uuid


def new_uid() -> str:
    return uuid.uuid4().hex
