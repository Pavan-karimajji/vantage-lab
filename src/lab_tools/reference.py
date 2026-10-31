# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/reference.py
# @brief LAB-39: ReferenceResim -- re-runs the lab libraries on a car recording by its refSyncs, for any cut and run order, and compares with equal_outputs.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""The reference re-run (``VANTAGE_Lab_Design.md`` section 6.4): the independent check Vantage's own re-run is
compared with. It shares no code with Vantage. For each simulated component it calls the library once per
recorded ``control`` and hands it the control and, for each declared data input, the sample the run's recorded
``refSync`` entry names (by ``measurement_counter`` only; 0 means nothing was read). The outputs are compared
with the recorded ones as *equal outputs* (``compare.equal_outputs``).

An input whose producer is a source or a ``measured`` stage is fed from the recording; an input whose producer is
*simulated* is fed from the output that producer has produced in this re-run (live), so a cut changes what a
consumer reads. A run whose refSync names a live sample not produced yet waits, in event order and never in wall
time; the runnable run first in ``order`` goes first (``interleave`` rotates the start after each run), so
``order`` decides nothing but which runnable run goes first. When no run can go on, that is a
``ReferenceResimError`` naming the component, the run and the missing sample, never a fallback.
With ``use_ref_sync=False`` the refSync is ignored and each input takes the newest sample, live or recorded, that
had arrived when the control was logged and whose source time is not after the control's (the time rule, ARCH
section 6, step 4); a run waits until every simulated producer run logged by then has been made. The runs whose
outputs then differ from the recording are *counted*, never asserted to be none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from mcap.reader import make_reader

from .abi import InputPort, LabLibrary, Manifest, PortSet, load_index
from .compare import OutputsComparison, equal_outputs, normalised
from .messages import MessageFactory
from .topology import ComponentDesc, Topology

CONTROL_PORT = "control"
REF_SYNC_PORT = "refSync"
COMP_STATE_PORT = "compState"
CONTROL_TYPE = "adas.common.CycleControl"
OUTPUT_CAP = 4096


class ReferenceResimError(RuntimeError):
    """The recording cannot be re-run: a missing topic, a count that disagrees, a sample no run can name."""


@dataclass(frozen=True)
class RecordedMessage:
    topic: str
    log_time_us: int
    payload: bytes


@dataclass
class Recording:
    """A recording read into memory: its messages in file order and the schema each topic carries."""

    messages: list[RecordedMessage]
    schemas: dict[str, tuple[str, bytes]]  # topic -> (message full name, descriptor set)

    @classmethod
    def read(cls, path: str | Path) -> "Recording":
        messages: list[RecordedMessage] = []
        schemas: dict[str, tuple[str, bytes]] = {}
        with open(path, "rb") as f:
            for schema, channel, message in make_reader(f).iter_messages(log_time_order=False):
                schemas.setdefault(channel.topic, (schema.name, schema.data))
                messages.append(RecordedMessage(channel.topic, message.log_time // 1000, bytes(message.data)))
        return cls(messages, schemas)

    def topic(self, name: str) -> list[RecordedMessage]:
        return [m for m in self.messages if m.topic == name]


@dataclass(frozen=True)
class ComponentResult:
    component: str
    runs: int
    ports: Mapping[str, OutputsComparison]  # per output port, the whole sequence
    mismatched_runs: int                    # runs where any output port differs from the recorded one

    @property
    def equal(self) -> bool:
        return all(self.ports.values())

    @property
    def first_difference_run(self) -> int | None:
        """The first run (counting from 1) whose outputs differ on any port; None when equal or the counts differ."""
        runs = [c.first_difference + 1 for c in self.ports.values() if c.first_difference is not None]
        return min(runs) if runs else None


@dataclass(frozen=True)
class ReferenceResult:
    topology: str
    use_ref_sync: bool
    components: Mapping[str, ComponentResult] = field(default_factory=dict)

    @property
    def equal(self) -> bool:
        return all(c.equal for c in self.components.values())

    @property
    def mismatched_runs(self) -> int:
        return sum(c.mismatched_runs for c in self.components.values())

    def describe(self) -> list[str]:
        """One line per difference, naming topology, component, run and port (SYT-RSY-04 FAIL side)."""
        lines = []
        for comp in self.components.values():
            for port, cmp in comp.ports.items():
                if not cmp:
                    run = "" if cmp.first_difference is None else f" run {cmp.first_difference + 1}"
                    lines.append(f"topology {self.topology} component {comp.component}{run} port {port}: {cmp.reason}")
        return lines


@dataclass(frozen=True)
class _Sample:
    log_time_us: int
    measurement_counter: int
    source_timestamp_us: int
    payload: bytes


@dataclass
class _Plan:
    comp: ComponentDesc
    manifest: Manifest
    factory: MessageFactory
    controls: list[RecordedMessage]
    starts: list[int]               # the logging time of each run's outputs: when the run started
    ref_syncs: list[RecordedMessage]
    recorded: dict[str, list[bytes]]
    types: dict[str, str]

    @property
    def runs(self) -> int:
        return len(self.controls)


class _NotReady(Exception):
    """A run needs a sample a simulated producer has not made yet; the run waits (never wall time)."""


class ReferenceResim:
    """Re-runs a topology's lab libraries on ``recording`` (a ``Recording`` or a path) without Vantage."""

    def __init__(self, topology: Topology, recording: Recording | str | Path, index: str | Path | Mapping):
        self.topology = topology
        self.recording = recording if isinstance(recording, Recording) else Recording.read(recording)
        self.index = load_index(index) if isinstance(index, (str, Path)) else index

    # ---- helpers ------------------------------------------------------------------------------------
    def _input_topic(self, comp: ComponentDesc, port: str) -> str:
        inp = next(i for i in comp.inputs if i.name == port)
        return f"source.{inp.producer}" if self.topology.source(inp.producer) is not None else f"{inp.producer}.{inp.port}"

    def _fail(self, comp: str, message: str) -> ReferenceResimError:
        return ReferenceResimError(f"topology {self.topology.name} component {comp}: {message}")

    def _samples(self, topic: str, factory: MessageFactory, type_name: str, cache: dict) -> list[_Sample]:
        if topic not in cache:
            found = []
            for m in self.recording.topic(topic):
                header = factory.parse(type_name, m.payload).sig_header
                found.append(_Sample(m.log_time_us, header.measurement_counter, header.source_timestamp_us, m.payload))
            cache[topic] = found
        return cache[topic]

    # ---- the re-run ---------------------------------------------------------------------------------
    def run(self, measured: Iterable[str] = (), order: Sequence[str] | None = None, *, use_ref_sync: bool = True,
            interleave: bool = False, config_dirs: Mapping[str, str | Path] | None = None) -> ReferenceResult:
        """Re-run every component not in ``measured``. ``order`` (a permutation of the component names; default:
        declared order) and ``interleave`` decide only which runnable run goes first: the first component in
        ``order`` with a runnable run goes next, and with ``interleave`` the search starts after the component that
        ran last. A run that must wait for a simulated producer's output waits; if no run can go on the re-run
        stops with ``ReferenceResimError``. ``config_dirs`` names a calibration bundle folder for a component
        (default: the bundle beside its library)."""
        topology = self.topology
        names = [c.name for c in topology.components]
        cut = set(measured)
        if not cut <= set(names):
            raise ReferenceResimError(f"measured names {sorted(cut - set(names))}, not components of {topology.name}")
        order = list(order) if order is not None else names
        if sorted(order) != sorted(names):
            raise ReferenceResimError(f"order must be a permutation of {names}, got {order}")
        simulated = [n for n in order if n not in cut]
        libraries: dict[str, LabLibrary] = {}
        try:
            plans = {}
            for name in simulated:
                entry = self.index["components"].get(name)
                if entry is None:
                    raise ReferenceResimError(f"index.json: no library for component {name}")
                lib = LabLibrary.load(entry["library"])
                libraries[name] = lib
                manifest = lib.manifest()
                plans[name] = self._plan(topology.component(name), manifest, MessageFactory(manifest.descriptor_set))  # type: ignore[arg-type]
                lib.init(None if config_dirs is None or name not in config_dirs else config_dirs[name])
            cache: dict[str, list[_Sample]] = {}
            produced = {n: {p.name: [] for p in plans[n].manifest.outputs} for n in simulated}
            live: dict[tuple[str, str], list[_Sample]] = {}  # (producer, port) -> what this re-run has made so far
            consumed = {(i.producer, i.port) for n in simulated for i in plans[n].comp.inputs if i.producer in plans}
            for key in consumed:
                live[key] = []
            next_run = {n: 0 for n in simulated}
            last = len(simulated) - 1
            while any(next_run[n] < plans[n].runs for n in simulated):
                start = (last + 1) % len(simulated) if interleave else 0
                waiting: list[str] = []
                for n in simulated[start:] + simulated[:start]:
                    run = next_run[n]
                    if run >= plans[n].runs:
                        continue
                    try:
                        offered = self._offer(plans[n], run, use_ref_sync, cache, live, next_run, plans)
                    except _NotReady as wait:
                        waiting.append(f"component {n} run {run + 1} waits: {wait}")
                        continue
                    self._run_once(libraries[n], plans[n], run, offered, produced[n])
                    for port in (p for (c, p) in consumed if c == n):
                        header = plans[n].factory.parse(plans[n].types[port], produced[n][port][run]).sig_header
                        live[(n, port)].append(_Sample(plans[n].starts[run], header.measurement_counter,
                                                       header.source_timestamp_us, produced[n][port][run]))
                    next_run[n] += 1
                    last = simulated.index(n)
                    break
                else:
                    raise ReferenceResimError(f"topology {topology.name}: no run can go on; " + "; ".join(waiting))
        finally:
            for lib in libraries.values():
                lib.shutdown()
        return ReferenceResult(topology.name, use_ref_sync,
                               {n: self._compare(plans[n], produced[n]) for n in simulated})

    def _plan(self, comp: ComponentDesc, manifest: Manifest, factory: MessageFactory) -> _Plan:
        rec = self.recording
        controls = rec.topic(f"{comp.name}.{CONTROL_PORT}")
        recorded_msgs = {p.name: rec.topic(f"{comp.name}.{p.name}") for p in manifest.outputs}
        for port, msgs in recorded_msgs.items():
            if len(msgs) != len(controls):
                raise self._fail(comp.name, f"{len(controls)} recorded controls but {len(msgs)} on {port}")
        starts = [m.log_time_us for m in recorded_msgs[COMP_STATE_PORT]]
        return _Plan(comp, manifest, factory, controls, starts, recorded_msgs[REF_SYNC_PORT],
                     {p: [m.payload for m in msgs] for p, msgs in recorded_msgs.items()},
                     {p.name: p.proto_type for p in manifest.outputs})

    def _pick(self, plan: _Plan, port: str, run: int, use_ref_sync: bool, cache: dict, live: Mapping,
              next_run: Mapping[str, int], plans: Mapping[str, _Plan]) -> _Sample | None:
        """The sample input ``port`` gets in ``run``; ``_NotReady`` when a simulated producer has not made it yet."""
        comp = plan.comp
        inp = next(i for i in comp.inputs if i.name == port)
        simulated = inp.producer in plans
        topic = self._input_topic(comp, port)
        decl = next(d for d in plan.manifest.inputs if d.name == port)
        samples = live[(inp.producer, inp.port)] if simulated else self._samples(topic, plan.factory, decl.proto_type, cache)
        start_us = plan.starts[run]
        if use_ref_sync:
            ref = plan.factory.parse(plan.types[REF_SYNC_PORT], plan.ref_syncs[run].payload)
            entry = next((e for e in ref.inputs if e.port == port), None)
            if entry is None:
                raise self._fail(comp.name, f"run {run + 1}: the refSync has no entry for input {port}")
            if entry.measurement_counter == 0:
                return None  # the car read nothing for this input
            named = [s for s in samples if s.measurement_counter == entry.measurement_counter]
            if not named:
                if simulated:
                    raise _NotReady(f"input {port} needs counter {entry.measurement_counter} of {topic}, "
                                    f"not produced yet ({len(samples)} of {plans[inp.producer].runs} runs made)")
                raise self._fail(comp.name, f"run {run + 1}: refSync names counter {entry.measurement_counter} "
                                            f"of input {port}, which {topic} does not hold")
            arrived = [s for s in named if s.log_time_us <= start_us]
            return (arrived or named)[-1]
        control = plan.factory.parse(CONTROL_TYPE, plan.controls[run].payload)
        trigger_us = plan.controls[run].log_time_us
        if simulated:  # every producer run logged by the trigger time must have been made
            due = sum(1 for t in plans[inp.producer].starts if (t < trigger_us if inp.previous_cycle else t <= trigger_us))
            if next_run[inp.producer] < due:
                raise _NotReady(f"input {port} needs {topic} runs up to {trigger_us} us, "
                                f"{next_run[inp.producer]} of {due} made")
        found = [s for s in samples
                 if (s.log_time_us < trigger_us if inp.previous_cycle else s.log_time_us <= trigger_us)
                 and (control.sig_header.source_timestamp_us == 0
                      or s.source_timestamp_us <= control.sig_header.source_timestamp_us)]
        return found[-1] if found else None

    def _offer(self, plan: _Plan, run: int, use_ref_sync: bool, cache: dict, live: Mapping,
               next_run: Mapping[str, int], plans: Mapping[str, _Plan]) -> list[InputPort]:
        start_us = plan.starts[run]
        offered = [InputPort(CONTROL_PORT, plan.controls[run].payload,
                             (start_us - plan.controls[run].log_time_us) / 1e6)]
        for decl in plan.manifest.inputs:
            if decl.name == CONTROL_PORT:
                continue
            sample = self._pick(plan, decl.name, run, use_ref_sync, cache, live, next_run, plans)
            offered.append(InputPort(decl.name, None) if sample is None
                           else InputPort(decl.name, sample.payload, (start_us - sample.log_time_us) / 1e6))
        return offered

    def _run_once(self, lib: LabLibrary, plan: _Plan, run: int, offered: list[InputPort],
                  produced: dict[str, list[bytes]]) -> None:
        outputs = PortSet.for_outputs([p.name for p in plan.manifest.outputs], OUTPUT_CAP)
        dt_s = (plan.comp.period_ms or 0) / 1e3
        if not lib.exec(PortSet.for_inputs(offered), outputs, dt_s):
            raise self._fail(plan.comp.name, f"adasExec failed in run {run + 1}")
        written = outputs.outputs()
        for decl in plan.manifest.outputs:
            payload = written.get(decl.name)
            if payload is None:
                raise self._fail(plan.comp.name, f"adasExec wrote no {decl.name} in run {run + 1}")
            produced[decl.name].append(payload)

    def _compare(self, plan: _Plan, produced: Mapping[str, list[bytes]]) -> ComponentResult:
        ports = {p: equal_outputs(plan.recorded[p], produced[p], plan.factory, plan.types[p]) for p in produced}
        bad = 0
        for run in range(plan.runs):
            if any(normalised(plan.factory, plan.types[p], plan.recorded[p][run])
                   != normalised(plan.factory, plan.types[p], produced[p][run]) for p in produced):
                bad += 1
        return ComponentResult(plan.comp.name, plan.runs, ports, bad)
