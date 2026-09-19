"""APEX research pilots — additive, read-only, offline-by-default packages.

Everything under `apex.research` is a sealed research pilot: it is never
imported by `apex.config`, `apex.scheduler`, `apex.db`, `apex.opportunity`,
`apex.strategy`, `apex.notifications`, `apex.app`, or any other production
path, and nothing here places orders, sizes positions, or sends alerts. See
`apex/research/gmgn/__init__.py` for the first pilot.
"""
from __future__ import annotations
