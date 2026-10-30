#!/usr/bin/env python3
"""Bootstrap stub for the Build & Publish GUI - docs/imp_design.md §13
(BS-02). Same-folder delegation into build.py's own main(), just targeting
`adas_build_tools.gui` instead of `.cli` - replaces the former ~324-line
duplicate (§13.2: 79% of it was a literal copy of build.py, kept in sync by
hand across every bootstrap fix since WP-03).

Physically identical across every component that carries it - propagating
a future fix means editing modules/toolchain/copy_files/scripts/
build_gui.py, running sync_copy_files.py, and committing the fan-out into
every component's own repo (R-BUILD-6), same discipline as build.py's own
stub.

Usage: python build_gui.py
       (or: gui.bat)
"""
import sys

import build  # same-folder import: Python puts this script's own directory
               # first on sys.path automatically, so build.py resolves with
               # no sys.path surgery of our own and no package/__init__.py.


def main():
    build.main(sys.argv[1:], module="adas_build_tools.gui")


if __name__ == "__main__":
    main()
