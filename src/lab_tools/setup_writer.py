# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/setup_writer.py
# @brief LAB-36, LAB-37: the one reader of provisional.yaml and the run setup JSON of a resim case (VANTAGE_Lab_Design.md sections 6.1, 7).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Run setups for the resim cases of a catalogue scenario.

``load_provisional`` is the only reader of ``catalogue/provisional.yaml``: the generator takes the
``calibration:`` part, ``write_run_setup`` the ``run_setup:`` part. ``write_run_setup`` writes one JSON per
resim case with the keys of ARCH section 2.3 and no others; the provisional values are those of design
section 7, and because a JSON setup has no room for a status, a sidecar ``<setup>.provisional.json`` lists
every value that is a provisional lab choice. A case that names a stage the topology does not have, a key it
does not know, an input that is not declared or a bulk port that does not exist is refused, never guessed.
The writer opens the libraries only to read their manifests (ports, ABI version, bundle hash); it runs none.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .abi import LabLibrary, Manifest
from .bundles import bundle_variant
from .topology import Scenario, Topology

RUN_SPEC_SCHEMA = "adas.sim.runspec/2.0"  # ARCH 2.3: "schema moves to a new version string"
CASE_KEYS = ("name", "measured", "trigger", "config", "bulk", "livemeas", "jump_to_ms")
LIVEMEAS_KEYS = ("cycle_slot_count", "cycle_slot_size", "bulk_slot_count", "bulk_slot_size")
CONFIG_KEYS = ("set", "remove")
CONTROL_PORT = "control"
SOURCE_PRODUCER = "source"  # the reserved producer word of the pipeline grammar (R-SIM-21)
PROVISIONAL_PARTS = ("calibration", "run_setup")


class ProvisionalError(Exception):
    """``provisional.yaml`` is unreadable or does not have the shape of design section 7."""


class RunSetupError(Exception):
    """A resim case cannot be written as a run setup; the message names the case and the key."""


# ---- the one reader of provisional.yaml (LAB-37) ------------------------------------------------

def load_provisional(path: Path | str, part: str = "calibration") -> dict[str, Any]:
    """One part of ``catalogue/provisional.yaml``.

    ``calibration``: topology -> component -> key -> ``{value, status}`` (the generator reads this).
    ``run_setup``: the run-setup values; every leaf is ``{value, status: provisional}`` (checked here)."""
    path = Path(path)
    if part not in PROVISIONAL_PARTS:
        raise ProvisionalError(f"{part!r}: not a part of {path.name} (parts: {', '.join(PROVISIONAL_PARTS)})")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProvisionalError(f"{path}: cannot be read: {exc}") from exc
    entries = raw.get(part) if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        shape = "topology -> component -> key" if part == "calibration" else "key -> {value, status}"
        raise ProvisionalError(f"{path}: {part!r} must be a mapping of {shape}")
    if part == "run_setup":
        for key, leaf in _leaves(entries):
            if leaf.get("status") != "provisional" or "value" not in leaf:
                raise ProvisionalError(f"{path}: run_setup.{key} must be {{value: ..., status: provisional}}")
    return entries


def _leaves(entries: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Mapping[str, Any]]]:
    """Every ``{value, status}`` leaf of a nested mapping, with its dotted key."""
    found: list[tuple[str, Mapping[str, Any]]] = []
    for key, entry in entries.items():
        where = f"{prefix}{key}"
        if not isinstance(entry, dict):
            raise ProvisionalError(f"{where}: must be a mapping")
        if "value" in entry or "status" in entry:
            found.append((where, entry))
        else:
            found.extend(_leaves(entry, where + "."))
    return found


def provisional_values(run_setup: Mapping[str, Any]) -> dict[str, Any]:
    """``load_provisional(.., "run_setup")`` as plain nested values, status dropped."""
    return {key: (provisional_values(entry) if not ("value" in entry or "status" in entry) else entry["value"])
            for key, entry in run_setup.items()}


# ---- the run setup (LAB-36) --------------------------------------------------------------------------

def livemeas_name(topology: str, scenario: str, case: str) -> str:
    """Design section 7: ``lab_<topology>_<scenario>_<case>``."""
    return f"lab_{topology}_{scenario}_{case}"


def setup_stem(scenario: str, case: str) -> str:
    return f"{scenario}.{case}"


def load_manifests(topology: Topology, index: Mapping[str, Any]) -> dict[str, Manifest]:
    """Each component's manifest, read from the library ``index.json`` names; no component is initialised."""
    manifests: dict[str, Manifest] = {}
    for comp in topology.components:
        entry = (index.get("components") or {}).get(comp.name)
        if entry is None:
            raise RunSetupError(f"index.json: no library for component {comp.name}")
        manifests[comp.name] = LabLibrary.load(entry["library"]).manifest()
    return manifests


def _as_list(case: str, key: str, value: Any) -> list:
    if not isinstance(value, list):
        raise RunSetupError(f"case {case}: {key} must be a list, got {value!r}")
    return value


def _check_case(topology: Topology, case: Mapping[str, Any], manifests: Mapping[str, Manifest]) -> str:
    """Refuse a case with an unknown key, stage, input or number; return its name."""
    name = case.get("name")
    if not isinstance(name, str) or not name:
        raise RunSetupError("a resim case needs a name")
    unknown = sorted(set(case) - set(CASE_KEYS))
    if unknown:
        raise RunSetupError(f"case {name}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(CASE_KEYS)})")
    stages = {c.name for c in topology.components}

    def stage(key: str, value: Any) -> None:
        if value not in stages:
            raise RunSetupError(f"case {name}: {key} names {value!r}, which is not a stage of {topology.name} "
                                f"(stages: {', '.join(sorted(stages))})")

    for s in _as_list(name, "measured", case.get("measured", [])):
        stage("measured", s)
    triggers = case.get("trigger", {})
    if not isinstance(triggers, dict):
        raise RunSetupError(f"case {name}: trigger must map a stage to 'control' or an input name")
    for s, value in triggers.items():
        stage("trigger", s)
        inputs = [i.name for i in topology.component(s).inputs]  # type: ignore[union-attr]
        if value != CONTROL_PORT and value not in inputs:
            raise RunSetupError(f"case {name}: trigger of {s} is {value!r}; it must be 'control' or one of "
                                f"its inputs ({', '.join(inputs) or 'none'})")
    configs = case.get("config", {})
    if not isinstance(configs, dict):
        raise RunSetupError(f"case {name}: config must map a stage to {{set, remove}}")
    for s, change in configs.items():
        stage("config", s)
        if not isinstance(change, dict) or not change or set(change) - set(CONFIG_KEYS):
            raise RunSetupError(f"case {name}: config of {s} must be a mapping with {' and/or '.join(CONFIG_KEYS)}")
    live = case.get("livemeas", {})
    if not isinstance(live, dict) or set(live) - set(LIVEMEAS_KEYS):
        raise RunSetupError(f"case {name}: livemeas may override only {', '.join(LIVEMEAS_KEYS)}")
    for key, value in live.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RunSetupError(f"case {name}: livemeas.{key} must be a whole number of at least 1, got {value!r}")
    jump = case.get("jump_to_ms")
    if jump is not None and (isinstance(jump, bool) or not isinstance(jump, int) or not 0 <= jump <= topology.duration_ms):
        raise RunSetupError(f"case {name}: jump_to_ms must be a whole number of ms within the run, got {jump!r}")
    for entry in _as_list(name, "bulk", case.get("bulk", [])):
        _bulk_ports(topology, manifests, name, entry)
    return name


def _bulk_ports(topology: Topology, manifests: Mapping[str, Manifest], case: str, entry: Any) -> set[tuple[str, str]]:
    """The (stage, port) pairs a ``bulk`` entry flags: ``source.<name>`` flags every input wired to that source;
    ``<stage>.<port>`` flags that port and every input wired to it."""
    producer, _, port = str(entry).partition(".")
    if producer == SOURCE_PRODUCER:
        if topology.source(port) is None:
            raise RunSetupError(f"case {case}: bulk names {entry!r}, but {topology.name} has no source {port!r}")
        return {(c.name, i.name) for c in topology.components for i in c.inputs if i.producer == port}
    manifest = manifests.get(producer)
    if manifest is None or port not in {p.name for p in manifest.outputs}:
        raise RunSetupError(f"case {case}: bulk names {entry!r}, which is not a port a stage of {topology.name} "
                            f"publishes (use source.<name> or <stage>.<port>)")
    return {(producer, port)} | {(c.name, i.name) for c in topology.components for i in c.inputs
                                 if i.producer == producer and i.port == port}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage(topology: Topology, comp, manifest: Manifest, case: Mapping[str, Any], entry: Mapping[str, str],
           bulk: set[tuple[str, str]], bundle_sha: str, depth: int) -> dict[str, Any]:
    library = Path(entry["library"])
    ports = [{"name": p.name, "dir": "req", "kind": "data", "proto_type": p.proto_type, "record": "full",
              "fixed_slots": None, "bulk": (comp.name, p.name) in bulk, "buffer_depth": depth}
             for p in manifest.inputs]
    ports += [{"name": p.name, "dir": "pro", "kind": "data", "proto_type": p.proto_type, "record": "full",
               "fixed_slots": None, "bulk": (comp.name, p.name) in bulk, "buffer_depth": depth}
              for p in manifest.outputs]
    inputs = [{"port": i.name,
               "producer": SOURCE_PRODUCER if topology.source(i.producer) is not None else i.producer,
               "producer_port": i.producer if topology.source(i.producer) is not None else i.port,
               "previous_cycle": i.previous_cycle} for i in comp.inputs]
    return {
        "name": comp.name,
        "mode": "measured" if comp.name in case.get("measured", []) else "simulate",
        "impl": {"kind": "dll", "path": library.absolute().as_posix()},
        "abi_version": manifest.abi_version,
        "config_bundle_sha256": bundle_sha,
        "trigger": (case.get("trigger") or {}).get(comp.name, CONTROL_PORT),
        "clock_period_ns": 0,  # every lab case starts a stage by its recorded control or a named input
        "ports": ports,
        "inputs": inputs,
    }


def write_run_setup(topology: Topology, scenario: Scenario, case: Mapping[str, Any], *, recording: Path | str,
                    index: Mapping[str, Any], out_dir: Path | str, run_setup_values: Mapping[str, Any],
                    manifests: Mapping[str, Manifest] | None = None) -> Path:
    """Write ``<out_dir>/<scenario>.<case>.run.json`` and its ``.provisional.json`` sidecar; return the first.

    ``run_setup_values`` is ``provisional_values(load_provisional(file, "run_setup"))``. The keys written are
    those of ARCH section 2.3 and no others. A ``config`` entry of the case becomes a repacked bundle copy under
    ``<out_dir>/<scenario>.<case>.bundles/<stage>/`` (``bundles.bundle_variant``) whose hash the stage carries."""
    recording, out_dir = Path(recording), Path(out_dir)
    if not recording.is_file():
        raise RunSetupError(f"{recording}: the recording of scenario {scenario.name} does not exist (run `car` first)")
    manifests = manifests if manifests is not None else load_manifests(topology, index)
    name = _check_case(topology, case, manifests)
    missing = [k for k in ("readiness_limit_steps", "fallback_max_gap", "buffer_depth", "livemeas", "limits",
                           "coherence_verdict") if k not in run_setup_values]
    if missing:
        raise RunSetupError(f"provisional.yaml run_setup: no value for {', '.join(missing)} (no default in code)")
    stem = setup_stem(scenario.name, name)
    live_name = livemeas_name(topology.name, scenario.name, name)

    bulk: set[tuple[str, str]] = set()
    for entry in case.get("bulk", []):
        bulk |= _bulk_ports(topology, manifests, name, entry)
    out_dir.mkdir(parents=True, exist_ok=True)

    stages = []
    for comp in topology.components:
        entry = index["components"][comp.name]
        sha = manifests[comp.name].config_bundle_sha256
        change = (case.get("config") or {}).get(comp.name)
        if change:
            target = out_dir / f"{stem}.bundles" / comp.name
            if target.exists():
                shutil.rmtree(target)  # our own output of an earlier write
            sha = bundle_variant(entry["config"], target, set_values=change.get("set"), remove=change.get("remove", ()))
        stages.append(_stage(topology, comp, manifests[comp.name], case, entry, bulk, sha,
                             run_setup_values["buffer_depth"]))

    livemeas = {"name": live_name, **{k: run_setup_values["livemeas"][k] for k in LIVEMEAS_KEYS}}
    livemeas.update(case.get("livemeas") or {})
    data: dict[str, Any] = {
        "schema": RUN_SPEC_SCHEMA,
        "run_id": live_name,
        "spec_sha256": "",
        "sources": [{"path": recording.absolute().as_posix(), "sha256": _sha256_file(recording), "codec": "mcap",
                     "ingest": [], "absent": []}],
        "stages": stages,
        "mutations": [],
        "outputs": {"path": (out_dir / f"{stem}.out.mcap").absolute().as_posix(), "codec": "mcap",
                    "livemeas": livemeas},
        "readiness_limit_steps": run_setup_values["readiness_limit_steps"],
        "fallback_max_gap": run_setup_values["fallback_max_gap"],
        "limits": dict(run_setup_values["limits"]),
        "coherence_verdict": run_setup_values["coherence_verdict"],
    }
    # the identity hash is over the canonical form with spec_sha256 blank
    data["spec_sha256"] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = out_dir / f"{stem}.run.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    provisional = {"readiness_limit_steps": data["readiness_limit_steps"], "fallback_max_gap": data["fallback_max_gap"],
                   "stages[].ports[].buffer_depth": run_setup_values["buffer_depth"],
                   **{f"outputs.livemeas.{k}": livemeas[k] for k in LIVEMEAS_KEYS},
                   **{f"limits.{k}": v for k, v in data["limits"].items()},
                   "coherence_verdict": data["coherence_verdict"]}
    sidecar = {"run_setup": path.name, "status": "provisional",
               "note": "every value below is a lab choice (VANTAGE_Lab_Design.md section 7), none a customer figure",
               "values": provisional}
    (out_dir / f"{stem}.run.provisional.json").write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
                                                          encoding="utf-8", newline="\n")
    return path
