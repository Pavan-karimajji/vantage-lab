# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/harness/lab_harness.py
# @brief LAB-21: F-HARN, drives one lab component through the generic ABI with scripted controls and hand-made samples.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""``LabHarness``: loads one component's library from ``index.json`` and runs it, one ``adasExec`` per
scripted ``CycleControl``, returning every output decoded (design section 6.3, fixture ``F-HARN``).

A step is a control (counter, time, op mode, the scheduler's cycle counter) and one hand-made
``LabSample`` per declared input: a sample, ``None`` (offered with ``valid`` 0, a withheld input) or
a missing key (the port is absent from the set). A fresh process per run is ``run_fresh_process``,
which re-executes this file with the steps as JSON.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[2]
if str(LAB_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(LAB_ROOT / "src"))

from lab_tools.abi import InputPort, LabAbiError, LabLibrary, PortSet, load_index  # noqa: E402
from lab_tools.messages import MessageFactory  # noqa: E402

CONTROL_TYPE = "adas.common.CycleControl"
SAMPLE_TYPE = "adas.lab.LabSample"
COMP_STATE_TYPE = "adas.df.CompState"
SIG_INIT, SIG_OK, SIG_INVALID = 0, 1, 2
TOPOLOGY = "running_example"


def find_index(topology: str = TOPOLOGY) -> Path | None:
    """The build tree's index.json: ``LAB_INDEX`` if set, else the newest ``conan_workarea/build-sil-*``."""
    explicit = os.environ.get("LAB_INDEX")
    if explicit:
        return Path(explicit)
    found = sorted((LAB_ROOT / "conan_workarea").glob(f"build-sil-*/lab/{topology}/index.json"))
    return found[-1] if found else None


@dataclass
class Control:
    counter: int
    time_us: int
    op_mode: str = "RUN"
    cycle_counter: int = 0


@dataclass
class Sample:
    sticker: int
    counter: int
    time_us: int
    value: int
    cycle_counter: int = 0


@dataclass
class Step:
    control: Control
    samples: dict[str, Sample | None] = field(default_factory=dict)
    producer_comp_state: dict[str, str] = field(default_factory=dict)  # input -> CompState name, offered beside it

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Step":
        return cls(Control(**data["control"]),
                   {k: (None if v is None else Sample(**v)) for k, v in data["samples"].items()},
                   dict(data.get("producer_comp_state", {})))


@dataclass
class RunResult:
    ok: bool
    raw: dict[str, bytes | None]
    outputs: dict[str, object]   # decoded message per port the library wrote; absent where it wrote nothing


class LabHarness:
    """One lab component, loaded from ``index.json``, ready to run."""

    def __init__(self, index_path: str | Path, component: str, output_cap: int = 4096):
        self.index_path = Path(index_path)
        self.component = component
        self.output_cap = output_cap
        self.runs = 0  # successful adasExec calls on this instance
        entry = load_index(self.index_path)["components"][component]
        self.library = LabLibrary.load(entry["library"])
        self.manifest = self.library.manifest()
        self.factory = MessageFactory(self.manifest.descriptor_set)
        self.library.init(None)  # the bundle beside the library
        self._types = {p.name: p.proto_type for p in self.manifest.inputs + self.manifest.outputs}
        self.input_names = [p.name for p in self.manifest.inputs if p.name != "control"]

    def close(self) -> None:
        self.library.shutdown()

    def __enter__(self) -> "LabHarness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- hand-made messages --------------------------------------------------------------------
    def control_message(self, control: Control):
        message = self.factory.new(CONTROL_TYPE, op_mode=self.factory.message_class(CONTROL_TYPE).OpMode.Value(control.op_mode))
        header = message.sig_header
        header.measurement_counter = control.counter
        header.source_timestamp_us = control.time_us
        header.cycle_counter = control.cycle_counter
        header.sig_status = SIG_OK
        return message

    def sample_message(self, sample: Sample):
        message = self.factory.new(SAMPLE_TYPE, value=sample.value)
        header = message.sig_header
        header.measurement_counter = sample.counter
        header.source_timestamp_us = sample.time_us
        header.cycle_counter = sample.cycle_counter
        header.sig_status = sample.sticker
        return message

    # ---- one run -------------------------------------------------------------------------------
    def run(self, step: Step) -> RunResult:
        """One ``adasExec`` for one control; every output decoded."""
        ports = [InputPort("control", self.control_message(step.control).SerializeToString())]
        for name, sample in step.samples.items():
            ports.append(InputPort(name, None if sample is None else self.sample_message(sample).SerializeToString()))
        for name, state in step.producer_comp_state.items():
            comp_state = self.factory.new(COMP_STATE_TYPE, function=name)
            comp_state.state = self.factory.message_class(COMP_STATE_TYPE).State.Value(state)
            ports.append(InputPort(f"{name}.compState", comp_state.SerializeToString()))
        inputs = PortSet.for_inputs(ports)
        outputs = PortSet.for_outputs([p.name for p in self.manifest.outputs], self.output_cap)
        ok = self.library.exec(inputs, outputs)
        self.runs += 1 if ok else 0
        return self.decode(ok, outputs.outputs())

    def decode(self, ok: bool, raw: dict[str, bytes | None]) -> RunResult:
        decoded = {name: self.factory.parse(self._types[name], data) for name, data in raw.items() if data is not None}
        return RunResult(ok, raw, decoded)

    def run_all(self, steps: list[Step]) -> list[RunResult]:
        return [self.run(step) for step in steps]

    def run_fresh_process(self, steps: list[Step]) -> list[RunResult]:
        """The same steps in a new process on a new instance of this component (``SYT-CTL-02``)."""
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--index", str(self.index_path), "--component", self.component],
            input=json.dumps([s.to_json() for s in steps]), capture_output=True, text=True, timeout=120)
        if completed.returncode != 0:
            raise LabAbiError(f"fresh process failed ({completed.returncode}): {completed.stderr.strip()}")
        return [self.decode(r["ok"], {k: (None if v is None else bytes.fromhex(v)) for k, v in r["raw"].items()})
                for r in json.loads(completed.stdout)]

    # ---- white-box seeding of the component's own run count -------------------------------------
    def seed_cycle_count(self, value: int, probe_counter: int = 0x5A5A1234, probe_time_us: int = 0x1122334455667788) -> int:
        """Sets the component's own run count to ``value`` (the ABI has no way to). The count sits in
        ``CycleStamp`` as ``{u32 ownCount, u32 measurementCounter, u64 sourceTimestampUs}``; a run with a
        probe control puts a recognisable ``(ownCount, probe_counter, probe_time_us)`` in the instance, the
        one place that triple occurs is found and its first word overwritten. Exactly one match is
        required, else ``LabAbiError``. Returns the address written. Costs three probe runs (they count in the
        run count the seed replaces)."""
        for i in range(3):
            self.run(Step(Control(probe_counter, probe_time_us, "RUN", 0xC0DE0000 + i)))
        own = self.runs
        window = 1024
        base = self.library.handle
        data = ctypes.string_at(base, window)
        needle = struct.pack("<IIQ", own & 0xFFFFFFFF, probe_counter, probe_time_us)
        hits = [i for i in range(0, window - len(needle) + 1, 4) if data[i:i + len(needle)] == needle]
        if len(hits) != 1:
            raise LabAbiError(f"cannot locate the run count in the instance: {len(hits)} matches of {needle.hex()}")
        ctypes.memmove(base + hits[0], struct.pack("<I", value), 4)
        return base + hits[0]


def _child_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--component", required=True)
    args = parser.parse_args()
    steps = [Step.from_json(s) for s in json.loads(sys.stdin.read())]
    with LabHarness(args.index, args.component) as harness:
        results = harness.run_all(steps)
    json.dump([{"ok": r.ok, "raw": {k: (None if v is None else v.hex()) for k, v in r.raw.items()}} for r in results],
              sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
