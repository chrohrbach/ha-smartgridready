"""Shared test setup.

Adds the add-on source directory to ``sys.path`` so test files can
``from src.rules_engine import …`` without installing the add-on.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON_SRC = ROOT / "smartgridready"
sys.path.insert(0, str(ADDON_SRC))
