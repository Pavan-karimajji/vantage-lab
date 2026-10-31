# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/__main__.py
# @brief The lab command line; this package state has the verb `car` (VANTAGE_Lab_Design.md section 8).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""``python -m lab_tools car --topology catalogue/running_example.yaml --scenario base --out base.mcap``

or, naming the catalogue file and writing ``lab/<topology>/<scenario>.mcap``:
``python -m lab_tools car running_example base``. The libraries come from ``--index`` (an
``index.json`` of a build tree), else ``LAB_INDEX``, else the newest ``conan_workarea/build-sil-*``.
The other verbs of the design (``generate``, ``setup``, ``reference``, ``supervise``, ``all``) are
later packages.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .car import Car, CarError, find_index
from .topology import TopologyError, load_topology

LAB_ROOT = Path(__file__).resolve().parents[2]


def _car(args: argparse.Namespace) -> int:
    if args.topology_name:
        topology_file = LAB_ROOT / "catalogue" / f"{args.topology_name}.yaml"
    elif args.topology:
        topology_file = Path(args.topology)
    else:
        raise CarError("name a topology: `car <name> <scenario>` or `car --topology <file> --scenario <name>`")
    scenario_name = args.scenario_name or args.scenario
    if not scenario_name:
        raise CarError("name a scenario: `car <name> <scenario>` or `--scenario <name>`")
    topology = load_topology(topology_file)
    scenario = next((s for s in topology.scenarios if s.name == scenario_name), None)
    if scenario is None:
        raise CarError(f"{topology_file}: no scenario {scenario_name!r} "
                       f"(it has: {', '.join(s.name for s in topology.scenarios)})")
    out = Path(args.out) if args.out else Path("lab") / topology.name / f"{scenario.name}.mcap"
    index = Path(args.index) if args.index else find_index(topology.name, LAB_ROOT)
    if index is None or not index.is_file():
        raise CarError(f"no built libraries for {topology.name}: pass --index, set LAB_INDEX, or build first")
    with Car(topology, scenario, index) as car:
        summary = car.record(out)
    print(f"{topology.name}/{scenario.name} seed {topology.seed}: {out} ({summary.file_size} bytes)")
    for name, count in summary.by_class().items():
        print(f"  {name:9s} {count:6d}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab_tools")
    verbs = parser.add_subparsers(dest="verb", required=True)
    car = verbs.add_parser("car", help="run a scenario of a topology in the fake car and write the recording")
    car.add_argument("topology_name", nargs="?", help="catalogue file name without .yaml")
    car.add_argument("scenario_name", nargs="?", help="scenario name")
    car.add_argument("--topology", help="topology file")
    car.add_argument("--scenario", help="scenario name")
    car.add_argument("--out", help="the .mcap to write (default lab/<topology>/<scenario>.mcap)")
    car.add_argument("--index", help="index.json of the built libraries")
    car.set_defaults(handler=_car)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (CarError, TopologyError) as exc:
        print(f"lab_tools {args.verb}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
