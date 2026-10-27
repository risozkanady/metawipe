"""PyInstaller entry point: the same as `python -m metawipe` (no arguments in a terminal opens the TUI)."""
import sys

from metawipe.cli import main

sys.exit(main())
