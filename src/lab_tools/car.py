# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/car.py
# @brief LAB-23..LAB-28: the fake car -- task clocks, sources, bus, event loop and control writer (VANTAGE_Lab_Design.md section 5).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""The fake car runs a topology's generated libraries through the generic ABI on their own task
clocks and records every port. It is the independent check Vantage is compared with, so it shares no
code with the engine: it imports nothing from the engine, uses no generated message bindings (R-SIM-11), and has its own control
writer.

Times are integer microseconds throughout. Tick k of a task has the nominal time
``phase + k * period``; its start is the nominal time plus a jitter drawn by
``random.Random("<seed>/<task name>")``, one draw per tick in tick order (a task with jitter 0 draws
nothing). A tick is made when its start is not after ``duration_ms``. Events at one logging time go in
the order: source samples, activations, starts; within a class in declaration order. A component
triggered by an input starts, and logs its control, right after the sample that triggers it is logged
(after the whole run, when a component's own output triggers it).

Only what the scenario ``base`` needs exists here: a scenario that carries ``car:`` or ``recording:``
keys is refused (``car.restart`` and the other verbs belong to LAB-WP-05 and LAB-WP-08).
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

from .abi import InputPort, LabLibrary, Manifest, PortSet, load_index
from .messages import MessageFactory
from .recorder import McapRecorder, TopicSpec
from .topology import ComponentDesc, IMAGE_KIND, Scenario, SourceDesc, Topology

CONTROL_TYPE = "adas.common.CycleControl"
SAMPLE_TYPE = "adas.lab.LabSample"
SIG_OK = 1
CONTROL_PORT = "control"
OUTPUT_CAP = 4096

SAMPLE, ACTIVATION, START = 0, 1, 2  # event classes, in the order they go at one logging time

TOPIC_CLASSES = ("source", "control", "data out", "refSync", "compState")


class CarError(RuntimeError):
    """The car cannot run what it was given, or a library broke the run."""


@dataclass(frozen=True)
class Tick:
    k: int
    nominal_us: int
    start_us: int


class Task:
    """A clock: nominal tick time and drawn jitter, integer µs (LAB-23)."""

    def __init__(self, name: str, period_us: int, phase_us: int, jitter_us: int, seed: int):
        self.name = name
        self.period_us = period_us
        self.phase_us = phase_us
        self.jitter_us = jitter_us
        self._rng = random.Random(f"{seed}/{name}") if jitter_us > 0 else None
        self._jitter: list[int] = []

    def nominal_us(self, k: int) -> int:
        return self.phase_us + k * self.period_us

    def jitter_of(self, k: int) -> int:
        """The jitter of tick k; draws are made in tick order, one per tick, and kept."""
        if self._rng is None:
            return 0
        while len(self._jitter) <= k:
            self._jitter.append(self._rng.randint(0, self.jitter_us))
        return self._jitter[k]

    def ticks(self, until_us: int) -> Iterator[Tick]:
        """Every tick whose start is not after ``until_us``, in tick order."""
        k = 0
        while self.nominal_us(k) <= until_us:
            nominal = self.nominal_us(k)
            start = nominal + self.jitter_of(k)
            if start <= until_us:
                yield Tick(k, nominal, start)
            k += 1


class SourceTask(Task):
    """A sample source: each new sample gets the previous counter plus 1 (the first is 1) and the
    non-zero nominal time; ``value`` is the counter; ``cycle_counter`` equals the counter (LAB-24)."""

    def __init__(self, desc: SourceDesc, seed: int, factory: MessageFactory):
        if desc.kind == IMAGE_KIND:
            raise CarError(f"source {desc.name}: image sources are LAB-49 (LAB-WP-08), not built yet")
        super().__init__(desc.name, desc.period_ms * 1000, desc.phase_ms * 1000, desc.jitter_ms * 1000, seed)
        self.desc = desc
        self._factory = factory
        self.counter = 0

    def next_sample(self, nominal_us: int) -> bytes:
        self.counter += 1
        if nominal_us <= 0:
            raise CarError(f"source {self.name}: nominal time {nominal_us} us is not positive")
        message = self._factory.new(SAMPLE_TYPE, value=self.counter)
        header = message.sig_header
        header.measurement_counter = self.counter
        header.source_timestamp_us = nominal_us
        header.cycle_counter = self.counter
        header.sig_status = SIG_OK
        return message.SerializeToString(deterministic=True)


@dataclass(frozen=True)
class BusEntry:
    log_time_us: int
    payload: bytes


class Bus:
    """Per port history of what was logged, in event order (LAB-25)."""

    def __init__(self) -> None:
        self._history: dict[str, list[BusEntry]] = {}

    def put(self, port: str, log_time_us: int, payload: bytes) -> None:
        history = self._history.setdefault(port, [])
        if history and log_time_us < history[-1].log_time_us:
            raise CarError(f"{port}: logging time {log_time_us} us is before {history[-1].log_time_us} us on the bus")
        history.append(BusEntry(log_time_us, payload))

    def newest_not_after(self, port: str, now_us: int) -> BusEntry | None:
        """The newest entry logged at or before ``now_us``; one logged earlier in the same instant counts."""
        for entry in reversed(self._history.get(port, ())):
            if entry.log_time_us <= now_us:
                return entry
        return None

    def newest_before(self, port: str, now_us: int) -> BusEntry | None:
        """The newest entry logged strictly before ``now_us`` (a ``previous_cycle`` input)."""
        for entry in reversed(self._history.get(port, ())):
            if entry.log_time_us < now_us:
                return entry
        return None


class ControlWriter:
    """Writes the ``control`` message of a run (design section 5.3). Its own code, not Vantage's.

    ``measurement_counter`` and ``source_timestamp_us`` come from what started the run; the
    ``cycle_counter`` is the car's count of runs of that component from 1; the sticker is OK;
    ``op_mode`` is RESTART on the first run, otherwise RUN. It never looks at any ``compState``."""

    def __init__(self, factories: Mapping[str, MessageFactory]):
        self._factories = factories

    def op_mode(self, component: str, run: int) -> str:
        return "RESTART" if run == 1 else "RUN"

    def write(self, component: str, run: int, measurement_counter: int, source_timestamp_us: int) -> bytes:
        factory = self._factories[component]
        cls = factory.message_class(CONTROL_TYPE)
        message = factory.new(CONTROL_TYPE, op_mode=cls.OpMode.Value(self.op_mode(component, run)))
        header = message.sig_header
        header.measurement_counter = measurement_counter
        header.source_timestamp_us = source_timestamp_us
        header.cycle_counter = run
        header.sig_status = SIG_OK
        return message.SerializeToString(deterministic=True)


def topic_class(topic: str) -> str:
    """Which of the five topic classes a recorded topic belongs to."""
    if topic.startswith("source."):
        return "source"
    port = topic.split(".", 1)[1] if "." in topic else topic
    return port if port in ("control", "refSync", "compState") else "data out"


@dataclass
class RunSummary:
    counts: dict[str, int] = field(default_factory=dict)  # messages per topic
    file_size: int = 0

    def by_class(self) -> dict[str, int]:
        result = {name: 0 for name in TOPIC_CLASSES}
        for topic, n in self.counts.items():
            result[topic_class(topic)] += n
        return result


def find_index(topology: str, root: Path | None = None) -> Path | None:
    """The build tree's ``index.json``: ``LAB_INDEX`` if set, else the newest ``conan_workarea/build-sil-*``."""
    explicit = os.environ.get("LAB_INDEX")
    if explicit:
        return Path(explicit)
    base = root if root is not None else Path(__file__).resolve().parents[2]
    found = sorted((base / "conan_workarea").glob(f"build-sil-*/lab/{topology}/index.json"))
    return found[-1] if found else None


class Car:
    """One run of one scenario of one topology on its built libraries (LAB-26)."""

    def __init__(self, topology: Topology, scenario: Scenario, index_path: str | Path):
        if topology.refused_at_load:
            raise CarError(f"{topology.name}: expects refusal at load; the car does not run it")
        if scenario.car or scenario.recording:
            raise CarError(f"scenario {scenario.name}: car/recording keys "
                           f"({', '.join(sorted(list(scenario.car) + list(scenario.recording)))}) are LAB-WP-05/08")
        self.topology = topology
        self.scenario = scenario
        self.index_path = Path(index_path)
        index = load_index(self.index_path)
        self._libs: dict[str, LabLibrary] = {}
        self._manifests: dict[str, Manifest] = {}
        self._factories: dict[str, MessageFactory] = {}
        self._build_ids: dict[str, str] = {}
        try:
            for comp in topology.components:
                entry = index["components"].get(comp.name)
                if entry is None:
                    raise CarError(f"{self.index_path}: no library for component {comp.name}")
                lib = LabLibrary.load(entry["library"])
                self._libs[comp.name] = lib
                self._manifests[comp.name] = lib.manifest()
                self._factories[comp.name] = MessageFactory(self._manifests[comp.name].descriptor_set)
                self._build_ids[comp.name] = entry.get("build_id", self._manifests[comp.name].build_id)
                lib.init(None)  # the config bundle beside the library
        except Exception:
            self.close()
            raise
        self._descriptor_for_sample = self._descriptor_of(SAMPLE_TYPE)
        self._sample_factory = MessageFactory(self._descriptor_for_sample)
        self.control_writer = ControlWriter(self._factories)
        self.bus = Bus()
        self._runs = {c.name: 0 for c in topology.components}
        self._recorder: McapRecorder | None = None
        self._pending: dict[tuple[str, int], tuple[bytes, int]] = {}
        self._triggers: dict[str, list[ComponentDesc]] = {}
        for comp in topology.components:
            if comp.trigger is not None:
                inp = next(i for i in comp.inputs if i.name == comp.trigger)
                self._triggers.setdefault(self.bus_port(inp.producer, inp.port), []).append(comp)

    # ---- set-up ---------------------------------------------------------------------------------
    def _descriptor_of(self, type_name: str) -> bytes:
        for comp in self.topology.components:
            manifest = self._manifests[comp.name]
            if any(p.proto_type == type_name for p in manifest.inputs + manifest.outputs):
                return manifest.descriptor_set
        raise CarError(f"no component declares a port of type {type_name}")

    def bus_port(self, producer: str, port: str) -> str:
        """The recorded topic of what ``producer`` (a source or a component) publishes on ``port``."""
        return f"source.{producer}" if self.topology.source(producer) is not None else f"{producer}.{port}"

    def topics(self) -> dict[str, TopicSpec]:
        """Every recorded topic: the sources (mocked) and every port of every component (simulated)."""
        result: dict[str, TopicSpec] = {}
        for source in self.topology.sources:
            result[f"source.{source.name}"] = TopicSpec(SAMPLE_TYPE, self._descriptor_for_sample, "mocked")
        for comp in self.topology.components:
            manifest = self._manifests[comp.name]
            for decl in manifest.inputs:
                if decl.name == CONTROL_PORT:
                    result[f"{comp.name}.{decl.name}"] = TopicSpec(decl.proto_type, manifest.descriptor_set, "simulated")
            for decl in manifest.outputs:
                result[f"{comp.name}.{decl.name}"] = TopicSpec(decl.proto_type, manifest.descriptor_set, "simulated")
        return result

    def recording_manifest(self) -> dict[str, object]:
        """The ``manifest`` metadata: no wall-clock time, so one seed gives one file."""
        return {
            "run_id": f"{self.topology.name}/{self.scenario.name}/{self.topology.seed}",
            "run_kind": "sim",
            "source_kind": "synthetic",
            "scenario": self.scenario.name,
            "seed": self.topology.seed,
            "components": [
                {"name": c.name, "build_id": self._build_ids[c.name], "abi_version": self._manifests[c.name].abi_version,
                 "period_ns": (c.period_ms or 0) * 1_000_000}
                for c in self.topology.components],
        }

    # ---- the event loop ---------------------------------------------------------------------------
    def run(self, recorder: McapRecorder) -> RunSummary:
        """Run the topology for its ``duration_ms``; every message goes to ``recorder`` and the bus."""
        topology = self.topology
        until = topology.duration_ms * 1000
        sources = [SourceTask(s, topology.seed, self._sample_factory) for s in topology.sources]
        tasks = {c.name: Task(c.name, (c.period_ms or 0) * 1000, (c.phase_ms or 0) * 1000, c.jitter_ms * 1000, topology.seed)
                 for c in topology.components if c.period_ms is not None}
        events: list[tuple[int, int, int, int, str]] = []  # (time, class, declaration order, tick, task name)
        for order, source in enumerate(sources):
            for tick in source.ticks(until):
                events.append((tick.start_us, SAMPLE, order, tick.k, source.name))
        for order, comp in enumerate(topology.components):
            if comp.name in tasks:
                for tick in tasks[comp.name].ticks(until):
                    events.append((tick.nominal_us, ACTIVATION, order, tick.k, comp.name))
                    events.append((tick.start_us, START, order, tick.k, comp.name))
        events.sort(key=lambda e: e[:4])

        self._recorder = recorder
        by_name = {s.name: s for s in sources}
        for time_us, kind, _order, k, name in events:
            if kind == SAMPLE:
                source = by_name[name]
                port = f"source.{name}"
                payload = source.next_sample(source.nominal_us(k))
                self._log(port, time_us, payload)
                self._fire_triggers([port], time_us)
            elif kind == ACTIVATION:
                self._activate(name, time_us, k + 1, time_us, k)
            else:
                control, logged_us = self._pending.pop((name, k))
                self.startComponent(name, time_us, control, logged_us, dt_s=tasks[name].period_us / 1e6)
        if self._pending:
            raise CarError(f"controls logged but never started: {sorted(self._pending)}")
        return RunSummary(counts=recorder.counts)

    def record(self, path: str | Path) -> RunSummary:
        """``run`` into a new ``.mcap`` file at ``path``."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with McapRecorder(path, self.topics(), self.recording_manifest()) as recorder:
            summary = self.run(recorder)
        summary.file_size = path.stat().st_size
        return summary

    # ---- logging, activation, start ----------------------------------------------------------------
    def _log(self, topic: str, time_us: int, payload: bytes) -> None:
        assert self._recorder is not None
        self._recorder.write(topic, time_us, payload)
        self.bus.put(topic, time_us, payload)

    def _activate(self, name: str, now_us: int, measurement_counter: int, source_timestamp_us: int,
                  tick: int) -> tuple[bytes, int]:
        """Count a run, write its control and log it at ``now_us`` (the nominal time, or the trigger's)."""
        self._runs[name] += 1
        control = self.control_writer.write(name, self._runs[name], measurement_counter, source_timestamp_us)
        assert self._recorder is not None
        self._recorder.write(f"{name}.{CONTROL_PORT}", now_us, control)
        self._pending[(name, tick)] = (control, now_us)
        return control, now_us

    def _fire_triggers(self, ports: list[str], now_us: int) -> None:
        """Start, in declaration order, every component whose trigger input is one of ``ports``."""
        triggered = [c for p in ports for c in self._triggers.get(p, ())]
        for comp in sorted(set(triggered), key=lambda c: self.topology.components.index(c)):
            inp = next(i for i in comp.inputs if i.name == comp.trigger)
            entry = self.bus.newest_not_after(self.bus_port(inp.producer, inp.port), now_us)
            assert entry is not None
            decl = next(p for p in self._manifests[comp.name].inputs if p.name == inp.name)
            header = self._factories[comp.name].parse(decl.proto_type, entry.payload).sig_header
            tick = self._runs[comp.name]
            control, logged_us = self._activate(comp.name, now_us, header.measurement_counter,
                                                header.source_timestamp_us, tick)
            del self._pending[(comp.name, tick)]
            self.startComponent(comp.name, now_us, control, logged_us)

    def startComponent(self, name: str, now_us: int, control: bytes, control_logged_us: int, dt_s: float = 0.0) -> None:
        """One component start (design section 5.3): pick every declared input from the bus, call
        ``adasExec`` once, write every output at ``now_us``. The ``compState`` it writes is recorded
        and forwarded like any output; it is never read."""
        comp = self.topology.component(name)
        assert comp is not None
        manifest = self._manifests[name]
        by_input = {i.name: i for i in comp.inputs}
        offered = [InputPort(CONTROL_PORT, control, (now_us - control_logged_us) / 1e6)]
        for decl in manifest.inputs:
            if decl.name == CONTROL_PORT:
                continue
            inp = by_input.get(decl.name)
            if inp is None:
                raise CarError(f"{name}: manifest input {decl.name} is not declared in the topology")
            port = self.bus_port(inp.producer, inp.port)
            entry = (self.bus.newest_before(port, now_us) if inp.previous_cycle
                     else self.bus.newest_not_after(port, now_us))
            if entry is None:
                offered.append(InputPort(decl.name, None))  # nothing yet: not offered, valid 0
            else:
                offered.append(InputPort(decl.name, entry.payload, (now_us - entry.log_time_us) / 1e6))
        inputs = PortSet.for_inputs(offered)
        outputs = PortSet.for_outputs([p.name for p in manifest.outputs], OUTPUT_CAP)
        if not self._libs[name].exec(inputs, outputs, dt_s):
            raise CarError(f"{name}: adasExec failed at {now_us} us")
        written = outputs.outputs()
        for decl in manifest.outputs:
            payload = written.get(decl.name)
            if payload is None:
                raise CarError(f"{name}: adasExec wrote no {decl.name} at {now_us} us")
            self._log(f"{name}.{decl.name}", now_us, payload)
        self._fire_triggers([f"{name}.{d.name}" for d in manifest.outputs], now_us)

    # ---- lifetime -----------------------------------------------------------------------------------
    def close(self) -> None:
        for lib in self._libs.values():
            lib.shutdown()

    def __enter__(self) -> "Car":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
