#!/usr/bin/env python3
"""
ralph.py — AI sprint runner.

Executes prd.json task backlogs via AI agents with autonomous failure recovery.
Each task becomes a git branch, gets coded by an AI agent, reviewed, CI-gated,
and merged — without human intervention for recoverable failures.

Usage:
    ./ralph.py [OPTIONS]
    ralph [OPTIONS]   # when installed via pipx

Run with --help for full option list.
"""

import sys


def main() -> int:
    print("ralph.py — not yet implemented. See roadmap.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
