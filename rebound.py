#!/usr/bin/env python
"""Launcher, so nothing depends on the caller getting PYTHONPATH right.

    python rebound.py run
    python rebound.py eval-policy

Works identically in PowerShell, cmd, bash and zsh. `python -m rebound` still
works too if `backend` is on the path, but this is the entry point the docs use
because "set an environment variable first" is the single most common way a
reviewer fails to run someone's project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from rebound.cli import main  # noqa: E402  (path setup must happen first)

if __name__ == "__main__":
    sys.exit(main())
