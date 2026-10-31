# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/__main__.py
# @brief The lab command line; this package state has the verbs `car`, `setup` and `reference` (VANTAGE_Lab_Design.md section 8).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""``python -m lab_tools car --topology catalogue/running_example.yaml --scenario base --out base.mcap``

or, naming the catalogue file and writing ``lab/<topology>/<scenario>.mcap``:
``python -m lab_tools car running_example base``. The libraries come from ``--index`` (an
``index.json`` of a build tree), else ``LAB_INDEX``, else the newest ``conan_workarea/build-sil-*``.
``python -m lab_tools setup running_example base`` writes one run setup per resim case of the scenario
next to the recording (``lab/<topology>/<scenario>.<case>.run.json``); ``python -m lab_tools reference
running_example base`` re-runs the libraries on the recording by its refSyncs and compares (``--no-ref-sync``
counts the runs the time rule gets wrong instead). The other verbs of the design (``generate``,
``supervise``, ``all``) are later packages.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .abi import load_index
from .car import Car, CarError, find_index
from .reference import ReferenceResim, ReferenceResimError
from .setup_writer import ProvisionalError, RunSetupError, load_provisional, provisional_values, write_run_setup
from .topology import Topology, TopologyError, load_topology

LAB_ROOT = Path(__file__).resolve().parents[2]


def _scenarios(args: argparse.Namespace) -> tuple[Topology, list, Path]:
    """The topology, the scenarios asked for (all when none is named) and the built libraries' index."""
    if not args.topology_name:
        raise CarError("name a topology: `<verb> <name> [<scenario>]`")
    topology_file = LAB_ROOT / "catalogue" / f"{args.topology_name}.yaml"
    topology = load_topology(topology_file)
    scenarios = [s for s in topology.scenarios if args.scenario_name in (None, s.name)]
    if not scenarios:
        raise CarError(f"{topology_file}: no scenario {args.scenario_name!r} "
                       f"(it has: {', '.join(s.name for s in topology.scenarios)})")
    index = Path(args.index) if args.index else find_index(topology.name, LAB_ROOT)
    if index is None or not index.is_file():
        raise CarError(f"no built libraries for {topology.name}: pass --index, set LAB_INDEX, or build first")
    return topology, scenarios, index


def _recording_of(args: argparse.Namespace, topology: Topology, scenario) -> Path:
    if args.recording:
        return Path(args.recording)
    return Path("lab") / topology.name / f"{scenario.name}.mcap"


def _setup(args: argparse.Namespace) -> int:
    topology, scenarios, index_file = _scenarios(args)
    provisional_file = Path(args.provisional) if args.provisional else LAB_ROOT / "catalogue" / "provisional.yaml"
    values = provisional_values(load_provisional(provisional_file, "run_setup"))
    index = load_index(index_file)
    out_dir = Path(args.out) if args.out else Path("lab") / topology.name
    for scenario in scenarios:
        for case in scenario.resim:
            path = write_run_setup(topology, scenario, case, recording=_recording_of(args, topology, scenario),
                                   index=index, out_dir=out_dir, run_setup_values=values)
            print(f"{topology.name}/{scenario.name}/{case['name']}: {path}")
    return 0


def _reference(args: argparse.Namespace) -> int:
    topology, scenarios, index = _scenarios(args)
    order = args.order.split(",") if args.order else None
    unequal = 0
    for scenario in scenarios:
        resim = ReferenceResim(topology, _recording_of(args, topology, scenario), index)
        for case in scenario.resim:
            label = f"{topology.name}/{scenario.name}/{case['name']}"
            if "jump_to_ms" in case:
                print(f"{label}: skipped, a jump is a Vantage host call (simEngineSeek)")
                continue
            result = resim.run(case.get("measured", []), order, use_ref_sync=not args.no_ref_sync,
                               interleave=args.interleave)
            print(f"{label}: " + ("equal outputs" if result.equal else f"{result.mismatched_runs} run(s) differ")
                  + f" (use_ref_sync={not args.no_ref_sync})")
            for comp in result.components.values():
                print(f"  {comp.component:3s} {comp.runs:5d} runs  {comp.mismatched_runs:5d} differ")
            for line in result.describe():
                print(f"  {line}")
            if not args.no_ref_sync and not result.equal:
                unequal += 1
    return 1 if unequal else 0


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
    for verb, text in (("setup", "write one run setup JSON per resim case of a catalogue scenario"),
                       ("reference", "re-run the libraries on a recording by its refSyncs and compare")):
        sub = verbs.add_parser(verb, help=text)
        sub.add_argument("topology_name", nargs="?", help="catalogue file name without .yaml")
        sub.add_argument("scenario_name", nargs="?", help="scenario name (default: every scenario)")
        sub.add_argument("--recording", help="the .mcap (default lab/<topology>/<scenario>.mcap)")
        sub.add_argument("--index", help="index.json of the built libraries")
        if verb == "setup":
            sub.add_argument("--out", help="folder for the run setups (default lab/<topology>)")
            sub.add_argument("--provisional", help="provisional values (default catalogue/provisional.yaml)")
            sub.set_defaults(handler=_setup)
        else:
            sub.add_argument("--no-ref-sync", action="store_true", help="time rule only; count the runs that differ")
            sub.add_argument("--order", help="component names, comma separated, in the order to re-run them")
            sub.add_argument("--interleave", action="store_true", help="alternate runs between components")
            sub.set_defaults(handler=_reference)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (CarError, TopologyError, RunSetupError, ProvisionalError, ReferenceResimError) as exc:
        print(f"lab_tools {args.verb}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
