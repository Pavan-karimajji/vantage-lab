# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/topology.py
# @brief Reads a topology file into a Topology and checks it (VANTAGE_Lab_Design.md section 3).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Topology loader (LAB-07).

``load_topology`` reads one ``catalogue/<topology>.yaml`` and returns a ``Topology``. Every
check of design section 3 raises ``TopologyError`` naming the file and the key. Standard
library and PyYAML only, so the build needs no Python environment of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


class TopologyError(Exception):
    """A topology file broke a rule of design section 3; names the file and the key."""

    def __init__(self, file: Path | str, key: str, message: str):
        self.file = str(file)
        self.key = key
        self.message = message
        super().__init__(f"{self.file}: {key}: {message}")


EXPECT_VALUES = ("exact", "diverges", "refused_at_load")
IMAGE_KIND = "image"
SAMPLE_KIND = "sample"

_SOURCE_KEYS = {"name", "period_ms", "phase_ms", "jitter_ms", "kind", "width", "height", "big_frame"}
_COMPONENT_KEYS = {"name", "period_ms", "trigger", "phase_ms", "jitter_ms", "inputs", "outputs", "memory"}
_INPUT_KEYS = {"producer", "port", "name", "previous_cycle"}
_SCENARIO_KEYS = {"name", "car", "recording", "expect", "resim"}
_TOPOLOGY_KEYS = {"topology", "seed", "duration_ms", "sources", "components", "scenarios"}


@dataclass(frozen=True)
class InputDesc:
    producer: str
    port: str
    name: str
    previous_cycle: bool


@dataclass(frozen=True)
class SourceDesc:
    name: str
    period_ms: int
    phase_ms: int
    jitter_ms: int
    kind: str
    width: int | None = None
    height: int | None = None
    big_frame: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ComponentDesc:
    name: str
    period_ms: int | None
    trigger: str | None
    phase_ms: int | None
    jitter_ms: int
    inputs: tuple[InputDesc, ...]
    outputs: tuple[str, ...]
    memory: bool


@dataclass(frozen=True)
class Scenario:
    name: str
    expect: str
    car: Mapping[str, Any] = field(default_factory=dict)
    recording: Mapping[str, Any] = field(default_factory=dict)
    resim: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Topology:
    name: str
    path: str
    seed: int
    duration_ms: int
    sources: tuple[SourceDesc, ...]
    components: tuple[ComponentDesc, ...]
    scenarios: tuple[Scenario, ...]

    def source(self, name: str) -> SourceDesc | None:
        return next((s for s in self.sources if s.name == name), None)

    def component(self, name: str) -> ComponentDesc | None:
        return next((c for c in self.components if c.name == name), None)

    @property
    def refused_at_load(self) -> bool:
        return any(s.expect == "refused_at_load" for s in self.scenarios)


def _mapping(path: Path, key: str, value: Any, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TopologyError(path, key, "must be a mapping")
    for k in value:
        if k not in allowed:
            raise TopologyError(path, f"{key}.{k}", f"unknown key (allowed: {', '.join(sorted(allowed))})")
    return value


def _int(path: Path, key: str, value: Any, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TopologyError(path, key, f"must be an integer, got {value!r}")
    if value < minimum:
        raise TopologyError(path, key, f"must be at least {minimum}, got {value}")
    return value


def _name(path: Path, key: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TopologyError(path, key, "must be a non-empty string")
    return value


def _list(path: Path, key: str, value: Any) -> list:
    if not isinstance(value, list):
        raise TopologyError(path, key, "must be a list")
    return value


def _label(i: int, raw: Any) -> Any:
    """How a list entry is named in an error: by its name when it has one, else by position."""
    return raw["name"] if isinstance(raw, dict) and isinstance(raw.get("name"), str) and raw["name"] else i


def _source(path: Path, i: int, raw: Any) -> SourceDesc:
    key = f"sources[{_label(i, raw)}]"
    m = _mapping(path, key, raw, _SOURCE_KEYS)
    name = _name(path, f"{key}.name", m.get("name"))
    key = f"sources[{name}]"
    period = _int(path, f"{key}.period_ms", m.get("period_ms"), minimum=1)
    phase = _int(path, f"{key}.phase_ms", m.get("phase_ms", period), minimum=1)
    jitter = _int(path, f"{key}.jitter_ms", m.get("jitter_ms", 0), minimum=0)
    kind = m.get("kind", SAMPLE_KIND)
    if kind not in (SAMPLE_KIND, IMAGE_KIND):
        raise TopologyError(path, f"{key}.kind", f"must be 'sample' or 'image', got {kind!r}")
    width = height = big_frame = None
    if kind == IMAGE_KIND:
        width = _int(path, f"{key}.width", m.get("width"), minimum=1)
        height = _int(path, f"{key}.height", m.get("height"), minimum=1)
        big_frame = m.get("big_frame")
        if big_frame is not None:
            bf = _mapping(path, f"{key}.big_frame", big_frame, {"at_counter", "bytes"})
            _int(path, f"{key}.big_frame.at_counter", bf.get("at_counter"), minimum=1)
            _int(path, f"{key}.big_frame.bytes", bf.get("bytes"), minimum=1)
    else:
        for k in ("width", "height", "big_frame"):
            if k in m:
                raise TopologyError(path, f"{key}.{k}", "applies to kind 'image' only")
    return SourceDesc(name, period, phase, jitter, kind, width, height, big_frame)


def _input(path: Path, comp: str, i: int, raw: Any) -> InputDesc:
    key = f"components[{comp}].inputs[{i}]"
    m = _mapping(path, key, raw, _INPUT_KEYS)
    producer = _name(path, f"{key}.producer", m.get("producer"))
    port = _name(path, f"{key}.port", m.get("port", "out"))
    name = _name(path, f"{key}.name", m.get("name", f"in_{producer}"))
    previous = m.get("previous_cycle", False)
    if not isinstance(previous, bool):
        raise TopologyError(path, f"{key}.previous_cycle", f"must be true or false, got {previous!r}")
    return InputDesc(producer, port, name, previous)


def _component(path: Path, i: int, raw: Any) -> ComponentDesc:
    key = f"components[{_label(i, raw)}]"
    m = _mapping(path, key, raw, _COMPONENT_KEYS)
    name = _name(path, f"{key}.name", m.get("name"))
    key = f"components[{name}]"
    has_period, has_trigger = "period_ms" in m, "trigger" in m
    if has_period == has_trigger:
        raise TopologyError(path, key, "exactly one of 'period_ms' and 'trigger' is required, "
                                       + ("got both" if has_period else "got neither"))
    period = _int(path, f"{key}.period_ms", m["period_ms"], minimum=1) if has_period else None
    trigger = _name(path, f"{key}.trigger", m["trigger"]) if has_trigger else None
    phase = None
    if has_period:
        phase = _int(path, f"{key}.phase_ms", m.get("phase_ms", period), minimum=1)
    jitter = _int(path, f"{key}.jitter_ms", m.get("jitter_ms", 0), minimum=0)
    if "inputs" not in m:
        raise TopologyError(path, f"{key}.inputs", "required (may be an empty list)")
    inputs = tuple(_input(path, name, j, r) for j, r in enumerate(_list(path, f"{key}.inputs", m["inputs"])))
    seen: set[str] = set()
    for inp in inputs:
        if inp.name in seen:
            raise TopologyError(path, f"{key}.inputs", f"duplicate input name {inp.name!r}")
        seen.add(inp.name)
    outputs_raw = m.get("outputs", ["out"])
    outputs = tuple(_name(path, f"{key}.outputs[{j}]", o) for j, o in enumerate(_list(path, f"{key}.outputs", outputs_raw)))
    if len(set(outputs)) != len(outputs):
        raise TopologyError(path, f"{key}.outputs", "duplicate output name")
    memory = m.get("memory", False)
    if not isinstance(memory, bool):
        raise TopologyError(path, f"{key}.memory", f"must be true or false, got {memory!r}")
    return ComponentDesc(name, period, trigger, phase, jitter, inputs, outputs, memory)


def _scenario(path: Path, i: int, raw: Any) -> Scenario:
    key = f"scenarios[{i}]"
    m = _mapping(path, key, raw, _SCENARIO_KEYS)
    name = _name(path, f"{key}.name", m.get("name"))
    expect = m.get("expect", "exact")
    if expect not in EXPECT_VALUES:
        raise TopologyError(path, f"scenarios[{name}].expect", f"must be one of {', '.join(EXPECT_VALUES)}, got {expect!r}")
    resim = _list(path, f"scenarios[{name}].resim", m.get("resim", []))
    for j, case in enumerate(resim):
        if not isinstance(case, dict) or not isinstance(case.get("name"), str):
            raise TopologyError(path, f"scenarios[{name}].resim[{j}]", "must be a mapping with a 'name'")
    return Scenario(name, expect, dict(m.get("car") or {}), dict(m.get("recording") or {}), tuple(resim))


def _check_loop(path: Path, components: tuple[ComponentDesc, ...]) -> None:
    """Rule 2 of ARCH section 5: a loop through inputs not marked previous_cycle."""
    by_name = {c.name: c for c in components}
    edges = {c.name: [i.producer for i in c.inputs if not i.previous_cycle and i.producer in by_name]
             for c in components}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def visit(n: str, trail: list[str]) -> None:
        state[n] = 1
        for p in edges[n]:
            if state.get(p) == 1:
                cycle = trail[trail.index(p):] + [p] if p in trail else [n, p]
                raise TopologyError(path, f"components[{n}].inputs",
                                    "loop through inputs not marked previous_cycle: " + " -> ".join(cycle))
            if p not in state:
                visit(p, trail + [p])
        state[n] = 2

    for c in components:
        if c.name not in state:
            visit(c.name, [c.name])


def load_topology(path: Path | str) -> Topology:
    """Read ``path`` into a ``Topology``; raise ``TopologyError`` for each check of section 3."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as exc:
        raise TopologyError(path, "<file>", f"cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TopologyError(path, "<file>", f"is not valid YAML: {exc}") from exc

    m = _mapping(path, "<file>", raw, _TOPOLOGY_KEYS)
    name = _name(path, "topology", m.get("topology"))
    if name != path.stem:
        raise TopologyError(path, "topology", f"{name!r} must equal the file name {path.stem!r}")
    seed = _int(path, "seed", m.get("seed"), minimum=0)
    duration = _int(path, "duration_ms", m.get("duration_ms"), minimum=1)
    sources = tuple(_source(path, i, r) for i, r in enumerate(_list(path, "sources", m.get("sources", []))))
    components = tuple(_component(path, i, r) for i, r in enumerate(_list(path, "components", m.get("components", []))))
    scenarios = tuple(_scenario(path, i, r) for i, r in enumerate(_list(path, "scenarios", m.get("scenarios", []))))

    seen: set[str] = set()
    for kind, group in (("sources", sources), ("components", components)):
        for item in group:
            if item.name in seen:
                raise TopologyError(path, f"{kind}[{item.name}].name", f"duplicate name {item.name!r}")
            seen.add(item.name)
    scenario_names: set[str] = set()
    for s in scenarios:
        if s.name in scenario_names:
            raise TopologyError(path, f"scenarios[{s.name}].name", f"duplicate name {s.name!r}")
        scenario_names.add(s.name)

    by_source = {s.name: s for s in sources}
    by_component = {c.name: c for c in components}
    for c in components:
        for j, inp in enumerate(c.inputs):
            key = f"components[{c.name}].inputs[{j}]"
            if inp.producer in by_component:
                if inp.port not in by_component[inp.producer].outputs:
                    raise TopologyError(path, f"{key}.port",
                                        f"{inp.producer!r} has no output {inp.port!r} "
                                        f"(it has: {', '.join(by_component[inp.producer].outputs)})")
            elif inp.producer in by_source:
                if inp.port not in ("out", inp.producer):
                    raise TopologyError(path, f"{key}.port",
                                        f"source {inp.producer!r} has one output, 'out' or {inp.producer!r}")
            else:
                raise TopologyError(path, f"{key}.producer", f"{inp.producer!r} is neither a source nor a component")
        if c.trigger is not None and c.trigger not in {i.name for i in c.inputs}:
            raise TopologyError(path, f"components[{c.name}].trigger",
                                f"{c.trigger!r} is not a declared input of {c.name!r}")

    topology = Topology(name, str(path), seed, duration, sources, components, scenarios)
    if not topology.refused_at_load:
        _check_loop(path, components)
    return topology
