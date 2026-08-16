"""Scanner fixtures shared by several test modules."""

from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
PROJ = FIXTURES_ROOT / "proj"
