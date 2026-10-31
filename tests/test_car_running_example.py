# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_car_running_example.py
# @brief LAB-30: SYT-HDR-01, SYT-HDR-02, SYT-HDR-04, SYT-CTL-05, SYT-RSY-01 on the fake car's recording of the running example, and the byte-identical double run.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Contract tests of fixture ``F-LAB``: the recording the fake car writes for ``running_example``,
scenario ``base``. Each ``SYT`` case ``.cN`` is one test named ``test_SYT_<AREA>_<nn>_c<N>_...``; its
docstring cites the ``SYS`` requirement whose ``Accept`` decides it (R-REQ-4, R-REQ-9). Steps and PASS/FAIL
sides: ``RCT_Test_Spec.md``. The recording is read back through the schemas it embeds (R-SIM-11). Every
check is a plain function that is also run on a planted violation, so each case is seen to fail.
Skipped, with the reason, when the build tree is absent or holds libraries of another operating system.
"""
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcap.reader import make_reader

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))

from lab_tools.abi import LabLibrary, load_index  # noqa: E402
from lab_tools.car import Car, find_index  # noqa: E402
from lab_tools.messages import MessageFactory  # noqa: E402
from lab_tools.topology import load_topology  # noqa: E402

TOPOLOGY_FILE = LAB_ROOT / "catalogue" / "running_example.yaml"
COMPONENTS = ["A", "B", "C", "D", "E"]
SOURCES = ["X", "Y", "Z"]
SIG_OK = 1


@pytest.fixture(scope="module")
def index_path():
    path = find_index("running_example", LAB_ROOT)
    if path is None or not path.is_file():
        pytest.skip("no build tree: conan_workarea/build-sil-*/lab/running_example/index.json is absent "
                    "(run build.bat -P base -t sil -p <platform>, or set LAB_INDEX)")
    suffix = Path(next(iter(load_index(path)["components"].values()))["library"]).suffix
    if (suffix == ".dll") != (sys.platform == "win32"):
        pytest.skip(f"the build tree at {path} holds {suffix} libraries; this is {sys.platform}")
    return path


@pytest.fixture(scope="module")
def topology():
    return load_topology(TOPOLOGY_FILE)


@pytest.fixture(scope="module")
def base_run(index_path, topology, tmp_path_factory):
    out = tmp_path_factory.mktemp("car") / "base.mcap"
    scenario = next(s for s in topology.scenarios if s.name == "base")
    with Car(topology, scenario, index_path) as car:
        summary = car.record(out)
    return out, summary


def read_recording(path):
    """Every message in file order as ``(topic, log_time_us, decoded message)``, through the embedded schemas."""
    factories = {}
    result = []
    with open(path, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            if schema.id not in factories:
                factories[schema.id] = MessageFactory(schema.data)
            result.append((channel.topic, message.log_time // 1000,
                           factories[schema.id].parse(schema.name, message.data)))
    return result


@pytest.fixture(scope="module")
def messages(base_run):
    return read_recording(base_run[0])


def stream(messages, topic):
    return [m for t, _, m in messages if t == topic]


# ---- checks (each also run on a planted violation) -------------------------------------------------------
def header_mismatches(messages, field):
    """Pairs (component, run, port) where an output's header ``field`` differs from its run's control."""
    bad = []
    for comp in COMPONENTS:
        controls = stream(messages, f"{comp}.control")
        for port in ("out", "refSync", "compState"):
            outputs = stream(messages, f"{comp}.{port}")
            assert len(outputs) == len(controls), f"{comp}.{port}: {len(outputs)} outputs for {len(controls)} controls"
            for run, (c, o) in enumerate(zip(controls, outputs), 1):
                if getattr(c.sig_header, field) != getattr(o.sig_header, field):
                    bad.append((comp, run, port))
    return bad


def source_defects(headers):
    """Defects in one source's output stream: a counter step other than 1, a time of 0 or a step back."""
    bad = []
    for i, h in enumerate(headers):
        if h.source_timestamp_us == 0:
            bad.append((i, "zero time"))
        if i > 0:
            if h.measurement_counter - headers[i - 1].measurement_counter != 1:
                bad.append((i, "counter step"))
            if h.source_timestamp_us < headers[i - 1].source_timestamp_us:
                bad.append((i, "time step back"))
    return bad


def control_defects(headers):
    """Defects in one component's control stream: a sticker other than OK, a run count step other than 1."""
    bad = []
    for i, h in enumerate(headers):
        if h.sig_status != SIG_OK:
            bad.append((i, "sticker"))
        if h.cycle_counter - (headers[i - 1].cycle_counter if i else 0) != 1:
            bad.append((i, "counter step"))
    return bad


def ref_sync_defects(ref_syncs, declared):
    return [i for i, r in enumerate(ref_syncs) if [e.port for e in r.inputs] != declared]


@dataclasses.dataclass
class H:
    measurement_counter: int = 0
    source_timestamp_us: int = 0
    cycle_counter: int = 0
    sig_status: int = SIG_OK


@dataclasses.dataclass
class Msg:
    sig_header: H


# ---- SYT-HDR-01 / SYT-HDR-02 ------------------------------------------------------------------------------
def test_SYT_HDR_01_c1_output_measurement_counters_equal_the_controls(messages):
    """SYS-0001 Accept: every dataOut, refSync and compState header equals its run's control header."""
    assert len(stream(messages, "B.control")) > 50
    assert header_mismatches(messages, "measurement_counter") == []


def plant(messages, topic, field, delta):
    """A copy of the stream in which every message of ``topic`` has header ``field`` raised by ``delta``."""
    result = []
    for t, ts, m in messages:
        if t == topic:
            h = H(m.sig_header.measurement_counter, m.sig_header.source_timestamp_us)
            setattr(h, field, getattr(h, field) + delta)
            m = Msg(h)
        result.append((t, ts, m))
    return result


def test_SYT_HDR_01_c1_planted_difference_is_found(messages):
    assert ("C", 1, "out") in header_mismatches(plant(messages, "C.out", "measurement_counter", 1),
                                                "measurement_counter")


def test_SYT_HDR_02_c1_output_timestamps_equal_the_controls(messages):
    """SYS-0002 Accept: every dataOut, refSync and compState header equals its run's control header."""
    assert header_mismatches(messages, "source_timestamp_us") == []


def test_SYT_HDR_02_c1_planted_difference_is_found(messages):
    assert ("A", 1, "out") in header_mismatches(plant(messages, "A.out", "source_timestamp_us", 1),
                                                "source_timestamp_us")


# ---- SYT-HDR-04 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_SYT_HDR_04_c1_sources_number_and_stamp_measurements(messages, source):
    """SYS-0004 Accept: counters step by 1, times non-zero and not earlier than the previous one."""
    headers = [m.sig_header for m in stream(messages, f"source.{source}")]
    assert len(headers) > 50
    assert headers[0].measurement_counter == 1
    assert source_defects(headers) == []


def test_SYT_HDR_04_c1_planted_gap_repeat_zero_and_step_back_are_found():
    good = [H(i, 1000 * i) for i in range(1, 6)]
    assert source_defects(good) == []
    assert source_defects(good[:2] + good[3:])                      # a gap
    assert source_defects(good[:3] + good[2:3] + good[3:])          # a repeat
    assert source_defects([H(1, 0)] + good[1:])                     # a 0
    assert source_defects(good[:3] + [H(4, 500)])                   # a step back


# ---- SYT-CTL-05 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("component", COMPONENTS)
def test_SYT_CTL_05_c1_controls_are_ok_and_counted(messages, component):
    """SYS-0010 Accept: every control has sticker 1 and a cycle_counter one more than the previous control."""
    headers = [m.sig_header for m in stream(messages, f"{component}.control")]
    assert len(headers) > 20
    assert control_defects(headers) == []


def test_SYT_CTL_05_c1_planted_sticker_and_counter_step_are_found():
    good = [H(cycle_counter=i) for i in range(1, 6)]
    assert control_defects(good) == []
    assert control_defects([H(cycle_counter=1), H(cycle_counter=2, sig_status=0), H(cycle_counter=3)])
    assert control_defects(good[:2] + good[3:])


# ---- SYT-RSY-01 ---------------------------------------------------------------------------------------------
def test_SYT_RSY_01_c1_ref_sync_entries_match_the_declared_inputs(messages, index_path):
    """SYS-0013 Accept: as many entries as declared data inputs, entry k naming declared input k."""
    entries = load_index(index_path)["components"]
    for comp in COMPONENTS:
        declared = [p.name for p in LabLibrary.load(entries[comp]["library"]).manifest().inputs if p.name != "control"]
        ref_syncs = stream(messages, f"{comp}.refSync")
        assert len(ref_syncs) > 20
        assert ref_sync_defects(ref_syncs, declared) == [], comp


def test_SYT_RSY_01_c1_planted_missing_extra_and_reordered_entries_are_found():
    class E:
        def __init__(self, port): self.port = port

    class R:
        def __init__(self, *ports): self.inputs = [E(p) for p in ports]

    declared = ["in_A", "in_X"]
    assert ref_sync_defects([R("in_A", "in_X")], declared) == []
    assert ref_sync_defects([R("in_A")], declared) == [0]
    assert ref_sync_defects([R("in_A", "in_X", "in_Y")], declared) == [0]
    assert ref_sync_defects([R("in_X", "in_A")], declared) == [0]


# ---- the recording as a whole ----------------------------------------------------------------------------------
def test_lab29_every_declared_port_is_recorded_and_times_never_go_back(messages):
    topics = {t for t, _, _ in messages}
    expected = {f"source.{s}" for s in SOURCES} | {f"{c}.{p}" for c in COMPONENTS
                                                   for p in ("control", "out", "refSync", "compState")}
    assert topics == expected
    times = [ts for _, ts, _ in messages]
    assert times == sorted(times)


def test_lab29_summary_counts_the_five_topic_classes(base_run):
    out, summary = base_run
    classes = summary.by_class()
    assert set(classes) == {"source", "control", "data out", "refSync", "compState"}
    assert classes["control"] == classes["data out"] == classes["refSync"] == classes["compState"] > 0
    assert sum(classes.values()) == sum(summary.counts.values())
    assert summary.file_size == out.stat().st_size


# ---- same seed, same bytes -------------------------------------------------------------------------------------
def test_lab29_same_seed_gives_byte_identical_files_across_processes(base_run, index_path, tmp_path):
    """The second file is made by the command line in a new process (design section 5.6)."""
    second = tmp_path / "second.mcap"
    env = dict(os.environ, PYTHONPATH=str(LAB_ROOT / "src"), LAB_INDEX=str(index_path))
    done = subprocess.run([sys.executable, "-m", "lab_tools", "car", "--topology", str(TOPOLOGY_FILE),
                           "--scenario", "base", "--out", str(second)],
                          capture_output=True, text=True, env=env, cwd=str(LAB_ROOT), timeout=300)
    assert done.returncode == 0, done.stderr
    assert second.read_bytes() == base_run[0].read_bytes()


def test_lab23_another_seed_gives_another_file(base_run, index_path, topology, tmp_path):
    other = tmp_path / "seed2.mcap"
    scenario = next(s for s in topology.scenarios if s.name == "base")
    with Car(dataclasses.replace(topology, seed=2), scenario, index_path) as car:
        car.record(other)
    assert other.read_bytes() != base_run[0].read_bytes()
