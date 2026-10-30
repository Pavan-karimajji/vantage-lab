#!/usr/bin/env python3
"""Bootstrap stub for the component-level setup - NS-12/D4c
(cache_first_no_server_plan.md): "the same setup exists at the root and
inside every component", so the instruction is always *setup, then
build*. Same-folder delegation into build.py's own main(), just
targeting `adas_build_tools.setup_cli` instead of `.cli` - the setup
LOGIC lives once in adas_build_tools (R-BUILD-1/6), never eight copies of
it; this file's only job, like build.py's and build_gui.py's own stubs,
is finding/creating .venv and installing the pinned adas_build_tools
before exec-ing into it.

Physically identical across every component that carries it -
propagating a future fix means editing modules/toolchain/copy_files/
scripts/setup.py, running sync_copy_files.py, and committing the fan-out
into every component's own repo (R-BUILD-6), same discipline as
build.py's/build_gui.py's own stubs.

Usage: python setup.py
       (or: setup.bat)
"""
import sys

import build  # same-folder import: Python puts this script's own directory
               # first on sys.path automatically, so build.py resolves with
               # no sys.path surgery of our own and no package/__init__.py.


def main():
    build.main(sys.argv[1:], module="adas_build_tools.setup_cli")


if __name__ == "__main__":
    main()
