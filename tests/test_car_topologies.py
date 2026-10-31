# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_car_topologies.py
# @brief LAB-41..LAB-48, LAB-57, LAB-63: the car on the other catalogue topologies -- input-triggered starts, previous_cycle, a late consumer, rates with no common grid, one producer with two consumers, a join, a measured middle.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Each topology of ``catalogue/`` after ``running_example`` makes one corner case (design section 6.1); this
file shows in the car's recording that the case happened. The tests read counters, logging times and ``refSync``
entries, never a count that jitter moves. Each check is a plain function that is also run on a planted violation
(R-REQ-9). ``SYT-RSY-04`` on the same recordings is ``test_catalogue.py``. ``SYT-CTL-04`` ``.c2`` (data triggers
in the run setup) passes only on Vantage: here ``data_triggered`` is recorded and its run setups are written
(``test_catalogue.py``), not run. Skipped, with the reason, when the build tree is absent (``test_car_running_example``).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

from lab_tools.abi import load_index  # noqa: E402
from lab_tools.car import Car, find_index  # noqa: E402
from lab_tools.topology import load_topology  # noqa: E402
from test_car_running_example import read_recording  # noqa: E402

CATALOGUE = LAB_ROOT / "catalogue"
TOPOLOGIES = ["fan_out", "diamond", "rates_not_multiple", "feedback", "measured_middle", "late_consumer",
              "data_triggered", "coprime_periods"]


def index_for(name):
    path = find_index(name, LAB_ROOT)
    if path is None or not path.is_file():
        pytest.skip(f"no build tree: conan_workarea/build-sil-*/lab/{name}/index.json is absent "
                    "(run build.bat -P base -t sil -p <platform>, or set LAB_INDEX)")
    suffix = Path(next(iter(load_index(path)["components"].values()))["library"]).suffix
    if (suffix == ".dll") != (sys.platform == "win32"):
        pytest.skip(f"the build tree at {path} holds {suffix} libraries; this is {sys.platform}")
    return path


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """``run(topology)`` -> (topology, index, path of the base recording, its messages); one car run each."""
    done = {}

    def make(name):
        if name not in done:
            topology = load_topology(CATALOGUE / f"{name}.yaml")
            index = index_for(name)
            out = tmp_path_factory.mktemp(name) / "base.mcap"
            with Car(topology, next(s for s in topology.scenarios if s.name == "base"), index) as car:
                car.record(out)
            done[name] = (topology, index, out, read_recording(out))
        return done[name]

    return make


def stream(messages, topic):
    """The decoded messages of ``topic`` as ``(log_time_us, message)``, in file order."""
    return [(t, m) for topic_, t, m in messages if topic_ == topic]


def counter(message):
    return message.sig_header.measurement_counter


def read_counters(messages, comp, port):
    """The ``measurement_counter`` each run of ``comp`` read on input ``port``, from its refSync (0 = nothing read)."""
    return [next(e.measurement_counter for e in m.inputs if e.port == port) for _, m in stream(messages, f"{comp}.refSync")]


# ---- every new topology: same seed, same bytes -----------------------------------------------------------------------
@pytest.mark.parametrize("name", TOPOLOGIES)
def test_lab41_48_a_second_run_of_the_same_seed_gives_the_same_bytes(run, name, tmp_path):
    topology, index, first, _ = run(name)
    second = tmp_path / "second.mcap"
    with Car(topology, next(s for s in topology.scenarios if s.name == "base"), index) as car:
        car.record(second)
    assert second.read_bytes() == first.read_bytes()
    assert len(first.read_bytes()) > 0


@pytest.mark.parametrize("name", ["late_consumer", "data_triggered"])
def test_lab41_48_a_new_process_gives_the_same_bytes(run, name, tmp_path):
    topology, index, first, _ = run(name)
    second = tmp_path / "second.mcap"
    env = dict(os.environ, PYTHONPATH=str(LAB_ROOT / "src"), LAB_INDEX=str(index))
    done = subprocess.run([sys.executable, "-m", "lab_tools", "car", "--topology", str(CATALOGUE / f"{name}.yaml"),
                           "--scenario", "base", "--out", str(second)],
                          capture_output=True, text=True, env=env, cwd=str(LAB_ROOT), timeout=300)
    assert done.returncode == 0, done.stderr
    assert second.read_bytes() == first.read_bytes()


# ---- LAB-46 late_consumer: rule 6 ------------------------------------------------------------------------------------
def late_reads(messages):
    """Runs of Q whose control was logged before a P sample that Q then read: (run, control us, sample us, start us)."""
    samples = {counter(m): t for t, m in stream(messages, "P.out")}
    controls = [t for t, _ in stream(messages, "Q.control")]
    starts = [t for t, _ in stream(messages, "Q.out")]
    found = []
    for run, read in enumerate(read_counters(messages, "Q", "in_P")):
        if read and samples[read] > controls[run]:
            found.append((run + 1, controls[run], samples[read], starts[run]))
    return found


def test_lab46_q_reads_a_p_sample_logged_after_its_own_control(run):
    """The case of design section 6.1: Q's control logged at 100 ms, P's sample at 102 ms, Q started later and reads it."""
    _, _, _, messages = run("late_consumer")
    late = late_reads(messages)
    assert (5, 100_000, 102_000) == late[[r[0] for r in late].index(5)][:3], late
    run5 = next(r for r in late if r[0] == 5)
    assert 102_000 <= run5[3] <= 108_000, "the run starts after P's sample and no later than the largest jitter of 8 ms"
    # the control is older than the sample it reads: that is what the car's separate activation and start make real
    q_controls = stream(messages, "Q.control")
    assert q_controls[4][1].sig_header.source_timestamp_us == 100_000
    p_samples = {counter(m): m for _, m in stream(messages, "P.out")}
    assert p_samples[5].sig_header.source_timestamp_us == 102_000 > 100_000
    assert len(late) > 5, "late reads are not a single event of the run"


def test_lab46_q_also_has_runs_that_start_before_the_sample(run):
    """The jitter draws are both sides of the 2 ms gap, so Q reads the older P sample in some runs."""
    _, _, _, messages = run("late_consumer")
    samples = {counter(m): t for t, m in stream(messages, "P.out")}
    reads = read_counters(messages, "Q", "in_P")
    controls = [t for t, _ in stream(messages, "Q.control")]
    early = [r for r, c in zip(reads, controls) if r and samples[r] <= c]
    assert early and len(early) < len(reads)


def test_lab46_planted_controls_logged_at_the_start_find_no_late_read(run):
    """FAIL side: a recording in which Q's control is logged when Q starts has no control older than the sample it reads."""
    _, _, _, messages = run("late_consumer")
    starts = iter(t for t, _ in stream(messages, "Q.out"))
    mutated = [(topic, next(starts) if topic == "Q.control" else t, m) for topic, t, m in messages]
    assert late_reads(messages) and late_reads(mutated) == []


# ---- LAB-44 feedback: previous_cycle ----------------------------------------------------------------------------------
def feedback_defects(messages):
    """Runs of A whose ``in_B`` is not B's output of the previous cycle (strictly before A's start)."""
    b_out = stream(messages, "B.out")
    a_start = [t for t, _ in stream(messages, "A.out")]
    bad = []
    for run, read in enumerate(read_counters(messages, "A", "in_B")):
        older = [counter(m) for t, m in b_out if t < a_start[run]]
        expected = older[-1] if older else 0
        if read != expected:
            bad.append((run + 1, read, expected))
    return bad


def test_lab44_a_reads_the_output_b_made_in_the_previous_cycle(run):
    topology, _, _, messages = run("feedback")
    assert [i.previous_cycle for i in topology.component("A").inputs] == [False, True]
    assert feedback_defects(messages) == []
    reads = read_counters(messages, "A", "in_B")
    assert reads[0] == 0, "nothing earlier than the first instant: the input is not offered"
    assert reads[1:] == list(range(1, len(reads))), "run k reads B's run k-1"


def test_lab44_b_ran_at_the_same_instant_before_a_and_a_did_not_read_it(run):
    """B is declared first, so at 40 ms B's output is already on the bus when A starts; previous_cycle skips it."""
    _, _, _, messages = run("feedback")
    b_out = stream(messages, "B.out")
    a_out = stream(messages, "A.out")
    assert [t for t, _ in b_out] == [t for t, _ in a_out], "same instants, jitter 0"
    a_reads = read_counters(messages, "A", "in_B")
    for run_index, (t, _) in enumerate(a_out[1:], 1):
        same_instant = next(counter(m) for tt, m in b_out if tt == t)
        assert same_instant == run_index + 1 and a_reads[run_index] == run_index != same_instant


def test_lab44_a_planted_current_instant_read_is_found(run):
    _, _, _, messages = run("feedback")
    mutated = []
    for topic, t, m in messages:
        if topic == "A.refSync":
            for e in m.inputs:
                if e.port == "in_B" and e.measurement_counter > 0:
                    e.measurement_counter += 1  # the B of this instant
        mutated.append((topic, t, m))
    assert feedback_defects(mutated)


# ---- LAB-47 data_triggered: input-triggered starts --------------------------------------------------------------------
def trigger_defects(messages):
    """Runs of T that do not follow an in_X sample one to one, with its time, counter and stamp."""
    x = stream(messages, "source.X")
    controls = stream(messages, "T.control")
    outs = stream(messages, "T.out")
    bad = []
    if not len(x) == len(controls) == len(outs):
        bad.append(("count", len(x), len(controls), len(outs)))
    for k, ((tx, sx), (tc, c), (to, _)) in enumerate(zip(x, controls, outs), 1):
        if not (tx == tc == to):
            bad.append((k, "logged at", tx, tc, to))
        if (counter(c), c.sig_header.source_timestamp_us) != (counter(sx), sx.sig_header.source_timestamp_us):
            bad.append((k, "control fields"))
        if c.sig_header.cycle_counter != k:
            bad.append((k, "cycle_counter"))
    if read_counters(messages, "T", "in_X") != [counter(s) for _, s in x]:
        bad.append(("refSync in_X",))
    return bad


def test_lab47_t_runs_follow_the_in_x_samples_one_to_one(run):
    topology, _, _, messages = run("data_triggered")
    t = topology.component("T")
    assert t.period_ms is None and t.trigger == "in_X"
    assert trigger_defects(messages) == []
    assert len(stream(messages, "T.control")) == 66, "X has a sample every 30 ms from 30 to 1980 ms"


def test_lab47_t_reads_y_as_the_event_order_leaves_it(run):
    """At 60, 120, ... X and Y share an instant and X is declared first: T's run sees the Y sample of 40, not 60."""
    _, _, _, messages = run("data_triggered")
    y = {t: counter(m) for t, m in stream(messages, "source.Y")}
    reads = read_counters(messages, "T", "in_Y")
    times = [t for t, _ in stream(messages, "T.control")]
    for t, read in zip(times, reads):
        expected = max((c for ty, c in y.items() if ty <= t and not (ty == t)), default=0)
        assert read == expected, (t, read, expected)
    assert times[1] == 60_000 and reads[1] == 2 and y[60_000] == 3


def test_lab47_planted_a_second_run_per_sample_is_found(run):
    _, _, _, messages = run("data_triggered")
    mutated = [x for x in messages]
    first_control = next(x for x in mutated if x[0] == "T.control")
    mutated.append(first_control)
    assert trigger_defects(mutated)


def test_lab47_the_resim_cases_make_both_triggers():
    """The file's two cases: the recorded control, and the input the car started T by."""
    topology = load_topology(CATALOGUE / "data_triggered.yaml")
    cases = {c["name"]: c for s in topology.scenarios for c in s.resim}
    assert cases["by_control"]["trigger"] == {"T": "control"} and cases["by_data"]["trigger"] == {"T": "in_X"}


# ---- LAB-43 rates_not_multiple ----------------------------------------------------------------------------------------
def test_lab43_the_runs_are_at_their_own_periods_and_read_at_no_fixed_step(run):
    topology, _, _, messages = run("rates_not_multiple")
    periods = {c.name: c.period_ms for c in topology.components}
    assert periods == {"A": 20, "B": 30, "C": 70}
    assert not any(a % b == 0 for a in periods.values() for b in periods.values() if a > b), "no period is a multiple of another"
    for name, period in periods.items():
        times = [t for t, _ in stream(messages, f"{name}.control")]
        assert times == [period * 1000 * k for k in range(1, topology.duration_ms // period + 1)]
    reads = read_counters(messages, "B", "in_A")
    assert {b - a for a, b in zip(reads, reads[1:])} == {1, 2}, "B reads A at steps of one and of two runs"
    assert read_counters(messages, "C", "in_A")[:3] == [3, 7, 10], "C at 70, 140, 210 ms reads A of 60, 140, 200 ms"


# ---- LAB-41 fan_out, LAB-42 diamond, LAB-45 measured_middle -----------------------------------------------------------
def test_lab41_two_consumers_of_one_producer_read_the_same_instant(run):
    _, _, _, messages = run("fan_out")
    a = {t: counter(m) for t, m in stream(messages, "A.out")}
    for consumer in ("B", "C"):
        times = [t for t, _ in stream(messages, f"{consumer}.control")]
        reads = read_counters(messages, consumer, "in_A")
        assert reads == [a[t] for t in times], consumer
    assert len(stream(messages, "B.control")) == 2 * len(stream(messages, "C.control"))


def test_lab42_d_joins_b_and_c_of_the_same_instant(run):
    _, _, _, messages = run("diamond")
    b = {t: counter(m) for t, m in stream(messages, "B.out")}
    c = {t: counter(m) for t, m in stream(messages, "C.out")}
    times = [t for t, _ in stream(messages, "D.control")]
    assert read_counters(messages, "D", "in_B") == [b[t] for t in times]
    assert read_counters(messages, "D", "in_C") == [c[t] for t in times]
    assert [b[t] for t in times] == [c[t] for t in times] and len(times) > 0


def test_lab45_the_case_marks_m_measured_and_a_b_simulated(run):
    topology, _, _, messages = run("measured_middle")
    (case,) = [c for s in topology.scenarios for c in s.resim]
    assert case["measured"] == ["M"]
    assert {c.name for c in topology.components} - set(case["measured"]) == {"A", "B"}
    assert [i.producer for i in topology.component("M").inputs] == ["A"]
    assert [i.producer for i in topology.component("B").inputs] == ["M"]
    m = {t: counter(x) for t, x in stream(messages, "M.out")}
    assert read_counters(messages, "B", "in_M") == [m[t] for t, _ in stream(messages, "B.control")]


# ---- LAB-48 loop_no_edge ------------------------------------------------------------------------------------------------
def test_lab48_the_car_does_not_run_a_file_that_expects_refusal():
    from lab_tools.car import CarError
    topology = load_topology(CATALOGUE / "loop_no_edge.yaml")
    assert topology.refused_at_load
    with pytest.raises(CarError, match="expects refusal at load"):
        Car(topology, topology.scenarios[0], index_for("loop_no_edge"))


# ---- LAB-57 coprime_periods ---------------------------------------------------------------------------------------------
def shared_grid(periods):
    """The largest whole-ms step every period is a multiple of (their greatest common divisor)."""
    from math import gcd
    from functools import reduce
    return reduce(gcd, periods)


def coprime_defects(topology, messages):
    """Why ``coprime_periods`` is not the case: a pair of periods sharing a divisor above 1 ms, a stage whose controls are not at
    its own period, or a stage that read an input at a fixed step instead of the newest one."""
    from math import gcd
    bad = []
    periods = {s.name: s.period_ms for s in topology.sources} | {c.name: c.period_ms for c in topology.components}
    names = sorted(periods)
    bad += [("shared divisor", a, b) for i, a in enumerate(names) for b in names[i + 1:] if gcd(periods[a], periods[b]) > 1]
    for comp in topology.components:
        times = [t for t, _ in stream(messages, f"{comp.name}.control")]
        if times != [comp.period_ms * 1000 * k for k in range(1, topology.duration_ms // comp.period_ms + 1)]:
            bad.append(("not at its own period", comp.name))
    return bad


def test_lab57_every_stage_runs_on_its_own_period_with_no_common_divisor(run):
    topology, _, _, messages = run("coprime_periods")
    assert {s.name: s.period_ms for s in topology.sources} == {"X": 7}
    assert {c.name: c.period_ms for c in topology.components} == {"A": 11, "B": 13}
    assert all(s.jitter_ms == 0 for s in topology.sources) and all(c.jitter_ms == 0 for c in topology.components), "no jitter"
    assert [(i.producer) for i in topology.component("A").inputs] == ["X"]
    assert [(i.producer) for i in topology.component("B").inputs] == ["A", "X"]
    assert shared_grid([7, 11, 13]) == 1
    assert coprime_defects(topology, messages) == []
    # B (13 ms) reads A (11 ms) and X (7 ms) as the newest sample at its own start: the steps between reads are not constant.
    reads_a, reads_x = read_counters(messages, "B", "in_A"), read_counters(messages, "B", "in_X")
    assert len({b - a for a, b in zip(reads_a, reads_a[1:])}) > 1 or len({b - a for a, b in zip(reads_x, reads_x[1:])}) > 1
    assert reads_x[:3] == [1, 3, 5], "X samples at 7, 14, ... ms: B at 13, 26, 39 ms reads 1, 3, 5"
    assert reads_a[:3] == [1, 2, 3], "A runs at 11, 22, 33 ms: B at 13, 26, 39 ms reads 1, 2, 3"


def test_lab57_the_resim_cases_are_cut_a_and_all_simulated():
    topology = load_topology(CATALOGUE / "coprime_periods.yaml")
    (base,) = topology.scenarios
    assert [c["name"] for c in base.resim] == ["cut_a", "all_simulated"]
    assert base.resim[0]["measured"] == ["A"] and not base.resim[1].get("measured")


def test_lab57_planted_periods_on_one_grid_are_found(run):
    import dataclasses
    topology, _, _, messages = run("coprime_periods")
    on_grid = dataclasses.replace(topology, components=tuple(
        dataclasses.replace(c, period_ms=c.period_ms + 1 if c.name == "A" else c.period_ms) for c in topology.components))
    assert coprime_defects(on_grid, messages) == [("not at its own period", "A")], "A declared at 12 ms, recorded at 11"
    same = dataclasses.replace(topology, components=tuple(
        dataclasses.replace(c, period_ms=14) if c.name == "A" else c for c in topology.components))
    assert ("shared divisor", "A", "X") in coprime_defects(same, messages)


# ---- LAB-63 repeat_trigger --------------------------------------------------------------------------------------------------
def repeat_defects(messages, port, counter_value, controls_of):
    """Why a recording is not ``repeat_trigger``: the sample of ``counter_value`` on ``port`` is not written twice with the same
    bytes 1 ms apart, or ``controls_of`` (the triggered stage) has a control that is not one per distinct counter."""
    bad = []
    copies = [(t, m) for t, m in stream(messages, port) if counter(m) == counter_value]
    if len(copies) != 2:
        bad.append(("copies", len(copies)))
    else:
        (t1, m1), (t2, m2) = copies
        if m1.SerializeToString(deterministic=True) != m2.SerializeToString(deterministic=True):
            bad.append("not byte-identical")
        if t2 - t1 != 1000:
            bad.append(("not 1 ms later", t2 - t1))
    distinct = {counter(m) for _, m in stream(messages, port)}
    if len(stream(messages, f"{controls_of}.control")) != len(distinct):
        bad.append(("controls", len(stream(messages, f"{controls_of}.control")), len(distinct)))
    return bad


@pytest.fixture(scope="module")
def repeat_run(run, tmp_path_factory):
    topology, index, _, _ = run("data_triggered")
    scenario = next(s for s in topology.scenarios if s.name == "repeat_trigger")
    out = tmp_path_factory.mktemp("repeat_trigger") / "repeat_trigger.mcap"
    with Car(topology, scenario, index) as car:
        car.record(out)
    return topology, scenario, out, read_recording(out)


def test_lab63_the_recording_holds_the_repeated_x_sample_twice_and_t_runs_once_for_it(run, repeat_run):
    _, _, _, base = run("data_triggered")
    topology, scenario, path, messages = repeat_run
    assert scenario.recording == {"repeat_sample": [{"port": "source.X", "counter": 10}]} and scenario.expect == "exact"
    assert [c["name"] for c in scenario.resim] == ["by_data"] and scenario.resim[0]["trigger"] == {"T": "in_X"}
    assert repeat_defects(messages, "source.X", 10, "T") == []
    assert len(stream(messages, "source.X")) == len(stream(base, "source.X")) + 1
    assert len(stream(messages, "T.control")) == len(stream(base, "T.control")) == 66, "the repeat starts no run"
    first = next(t for t, m in stream(messages, "source.X") if counter(m) == 10)
    assert first == 300_000 and not [t for t, _ in stream(messages, "T.control") if t == first + 1000]
    others = [(t, m.SerializeToString(deterministic=True)) for t, m in stream(messages, "source.X") if counter(m) != 10]
    assert others == [(t, m.SerializeToString(deterministic=True)) for t, m in stream(base, "source.X") if counter(m) != 10]
    for topic in ("T.control", "T.out", "T.refSync", "T.compState", "source.Y"):
        assert [(t, m.SerializeToString(deterministic=True)) for t, m in stream(messages, topic)] == \
            [(t, m.SerializeToString(deterministic=True)) for t, m in stream(base, topic)], topic
    assert trigger_defects([x for x in messages if not (x[0] == "source.X" and x[1] == first + 1000)]) == []
    from mcap.reader import make_reader
    with open(path, "rb") as f:
        manifest = next(dict(m.metadata) for m in make_reader(f).iter_metadata() if m.name == "manifest")
    import yaml
    assert yaml.safe_load(manifest["yaml"])["recording_edits"] == {"repeat_sample": [{"port": "source.X", "counter": 10}]}, "the manifest states the edit"


def test_lab63_planted_a_recording_without_the_repeat_or_with_an_extra_run_is_found(run, repeat_run):
    _, _, _, base = run("data_triggered")
    _, _, _, messages = repeat_run
    assert repeat_defects(base, "source.X", 10, "T") == [("copies", 1)]
    extra = list(messages) + [next(x for x in messages if x[0] == "T.control")]
    assert ("controls", 67, 66) in repeat_defects(extra, "source.X", 10, "T")
