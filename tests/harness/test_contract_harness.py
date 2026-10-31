# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/harness/test_contract_harness.py
# @brief LAB-22: SYT-HDR-03, SYT-CTL-01, SYT-CTL-02, SYT-RSY-02, SYT-RSY-03, SYT-CST-06 on the running example's libraries.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Contract tests of fixture ``F-HARN``. Each ``SYT`` case ``.cN`` is one test named
``test_SYT_<AREA>_<nn>_c<N>_...``; its docstring cites the ``SYS`` requirement whose ``Accept`` decides
it (R-REQ-4, R-REQ-9). Steps and PASS/FAIL sides: ``RCT_Test_Spec.md``. ``B`` has four inputs and memory;
``A`` serves where one input is enough. Skipped, with the reason, when the build tree is absent or holds
libraries of another operating system.
"""
import sys
from pathlib import Path

import pytest

from lab_harness import (SIG_INVALID, SIG_OK, Control, LabHarness, Sample, Step, find_index)
from lab_tools.abi import load_index
from lab_tools.bundles import bundle_variant

B_INPUTS = ["in_A", "in_X", "in_Y", "in_Z"]
MAX_AGE_B_US = 118000
UINT32 = 1 << 32


@pytest.fixture(scope="module")
def index_path():
    path = find_index()
    if path is None or not path.is_file():
        pytest.skip("no build tree: conan_workarea/build-sil-*/lab/running_example/index.json is absent "
                    "(run build.bat -P base -t sil -p <platform>, or set LAB_INDEX)")
    suffix = Path(next(iter(load_index(path)["components"].values()))["library"]).suffix
    if (suffix == ".dll") != (sys.platform == "win32"):
        pytest.skip(f"the build tree at {path} holds {suffix} libraries; this is {sys.platform}")
    return path


@pytest.fixture
def harness_b(index_path):
    with LabHarness(index_path, "B") as harness:
        yield harness


@pytest.fixture
def harness_a(index_path):
    with LabHarness(index_path, "A") as harness:
        yield harness


def good_step(i, inputs, *, op_mode="RUN", offset=0):
    """Run ``i``: a control and one trusted sample per input, all inside the declared age limit."""
    time_us = 2_000_000 + 40_000 * i
    samples = {name: Sample(SIG_OK, counter=500 * (k + 1) + i + offset, time_us=time_us - 1_000 * (k + 1),
                            value=3 * i + k + 1 + offset, cycle_counter=100 * (k + 1) + i)
               for k, name in enumerate(inputs)}
    return Step(Control(counter=10_000 + i, time_us=time_us, op_mode=op_mode, cycle_counter=7 * i % 5), samples)


def header_cycle_counters(result):
    return {name: msg.sig_header.cycle_counter for name, msg in result.outputs.items()}


def assert_steps_of_one(values):
    for previous, current in zip(values, values[1:]):
        assert (current - previous) % UINT32 == 1, f"cycle_counter went {previous} -> {current} in {values}"


# ---- SYT-HDR-03 ----------------------------------------------------------------------------------

def test_SYT_HDR_03_c1_cycle_counter_rises_by_one_per_run_whatever_the_control_says(harness_b):
    """Serves SYS-0003 (Accept: consecutive runs differ by exactly 1 modulo 2^32, whatever the control's counter was)."""
    control_counters = [7, 7, 900, 3, 3, 3, 0xFFFFFFFF, 12, 12, 5]
    results = []
    for i, control_counter in enumerate(control_counters):
        step = good_step(i, B_INPUTS)
        step.control.cycle_counter = control_counter
        results.append(harness_b.run(step))
    assert all(r.ok for r in results)
    for port in ("out", "refSync", "compState"):  # every output header, per the Shall
        assert_steps_of_one([r.outputs[port].sig_header.cycle_counter for r in results])


def test_SYT_HDR_03_c1_planted_violation_a_copied_or_repeated_count_is_rejected():
    """The comparison of SYT-HDR-03.c1 fails on a count copied from the control or repeated (R-REQ-5)."""
    assert_steps_of_one([1, 2, 3])
    assert_steps_of_one([UINT32 - 1, 0, 1])
    with pytest.raises(AssertionError):
        assert_steps_of_one([1, 2, 2])          # a repeat
    with pytest.raises(AssertionError):
        assert_steps_of_one([1, 7, 7, 900, 3])  # the control's counters copied


def test_SYT_HDR_03_c2_count_wraps_to_zero_after_0xFFFFFFFF(index_path, tmp_path):
    """Serves SYS-0003 (Accept: differs by exactly 1 modulo 2^32); B starts at 0xFFFFFFFF through its LAB_B_START_COUNT key, in a bundle variant."""
    key = "LAB_B_START_COUNT"
    source = Path(load_index(index_path)["components"]["B"]["config"])
    assert f"{key}: 0" in (source / "default.yaml").read_text(encoding="utf-8").splitlines(), "the built bundle starts at 0"
    bundle_variant(source, tmp_path / "start_max", set_values={key: 0xFFFFFFFF})
    with LabHarness(index_path, "B", bundle=tmp_path / "start_max") as harness:
        after = [harness.run(good_step(i, B_INPUTS)) for i in range(3)]
    assert all(r.ok for r in after)
    assert [r.outputs["out"].sig_header.cycle_counter for r in after] == [0, 1, 2]
    assert [r.outputs["refSync"].sig_header.cycle_counter for r in after] == [0, 1, 2]
    assert [r.outputs["compState"].sig_header.cycle_counter for r in after] == [0, 1, 2]


# ---- SYT-CTL-01 ----------------------------------------------------------------------------------

def test_SYT_CTL_01_c1_fifty_controls_give_fifty_refsyncs_one_per_counter(harness_b):
    """Serves SYS-0006 (Accept: N controls give exactly N refSync, each carrying a different control's measurement_counter)."""
    counters = [4000 + 13 * i for i in range(50)]
    results = []
    for i, counter in enumerate(counters):
        step = good_step(i, B_INPUTS)
        step.control.counter = counter
        results.append(harness_b.run(step))
    assert all(r.ok for r in results)
    ref_syncs = [r.outputs["refSync"] for r in results if "refSync" in r.outputs]
    assert len(ref_syncs) == 50
    seen = [m.sig_header.measurement_counter for m in ref_syncs]
    assert len(set(seen)) == 50, "a counter repeated"
    assert seen == counters, "a counter missing or out of place"
    assert all(set(r.outputs) == {"out", "refSync", "compState"} for r in results), "one output set per control"


# ---- SYT-CTL-02 ----------------------------------------------------------------------------------

def _tail_s(offset=0):
    """Sequence S: 8 runs with memory-sensitive values; counters and times the same wherever it is run."""
    return [good_step(100 + i, B_INPUTS, offset=offset) for i in range(8)]


def _outs_without_cycle_counter(results):
    texts = []
    for r in results:
        out = type(r.outputs["out"])()
        out.CopyFrom(r.outputs["out"])
        out.sig_header.cycle_counter = 0
        texts.append(out.SerializeToString())
    return texts


def test_SYT_CTL_02_c1_restart_run_equals_fresh_process_from_the_next_run_on(harness_b):
    """Serves SYS-0007 (Accept: after a mid-run RESTART and in a fresh process after its first RESTART, equal dataOut from the next run on)."""
    other = [good_step(i, B_INPUTS, offset=900) for i in range(30)]
    restart = good_step(99, B_INPUTS, op_mode="RESTART")
    tail = _tail_s()

    mid_run = harness_b.run_all(other + [restart] + tail)
    fresh = harness_b.run_fresh_process([restart] + tail)

    assert all(r.ok for r in mid_run) and all(r.ok for r in fresh)
    after_restart = _outs_without_cycle_counter(mid_run[len(other) + 1:])
    from_fresh = _outs_without_cycle_counter(fresh[1:])
    assert len(after_restart) == len(from_fresh) == len(tail)
    assert after_restart == from_fresh


def test_SYT_CTL_02_c1_planted_violation_without_restart_the_memory_shows(harness_b):
    """The comparison of SYT-CTL-02.c1 can fail: the same 30 runs and S without the RESTART differ from a fresh process (R-REQ-5)."""
    other = [good_step(i, B_INPUTS, offset=900) for i in range(30)]
    tail = _tail_s()
    no_restart = harness_b.run_all(other + tail)
    fresh = harness_b.run_fresh_process([good_step(99, B_INPUTS, op_mode="RESTART")] + tail)
    assert _outs_without_cycle_counter(no_restart[len(other):]) != _outs_without_cycle_counter(fresh[1:])


# ---- SYT-RSY-02 ----------------------------------------------------------------------------------

def test_SYT_RSY_02_c1_every_entry_equals_the_header_of_the_sample_read(harness_b):
    """Serves SYS-0014 (Accept: with known samples, one INVALID and one after a skipped producer run, every entry equals that sample's header in all four fields)."""
    first = good_step(0, B_INPUTS)
    second = good_step(1, B_INPUTS)
    second.samples["in_Y"].sticker = SIG_INVALID                    # an INVALID sample
    second.samples["in_Z"].cycle_counter = first.samples["in_Z"].cycle_counter + 2   # producer run skipped
    for step in (first, second):
        result = harness_b.run(step)
        assert result.ok
        entries = list(result.outputs["refSync"].inputs)
        assert [e.port for e in entries] == B_INPUTS
        for entry in entries:
            sample = step.samples[entry.port]
            assert (entry.measurement_counter, entry.source_timestamp_us, entry.cycle_counter, entry.sig_status) == \
                   (sample.counter, sample.time_us, sample.cycle_counter, sample.sticker), entry.port


# ---- SYT-RSY-03 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("withheld_as", ["valid_zero", "port_absent"])
def test_SYT_RSY_03_c1_a_withheld_input_reads_zero_and_the_others_do_not(harness_b, withheld_as):
    """Serves SYS-0015 (Accept: the withheld input's entry has measurement_counter 0 and every other entry is non-zero)."""
    healthy = good_step(0, B_INPUTS)
    assert harness_b.run(healthy).ok
    step = good_step(1, B_INPUTS)
    if withheld_as == "valid_zero":
        step.samples["in_X"] = None
    else:
        del step.samples["in_X"]
    result = harness_b.run(step)
    assert result.ok
    entries = {e.port: e for e in result.outputs["refSync"].inputs}
    assert list(entries) == B_INPUTS
    assert entries["in_X"].measurement_counter == 0
    for name in ("in_A", "in_Y", "in_Z"):
        assert entries[name].measurement_counter != 0, name


# ---- SYT-CST-06 ----------------------------------------------------------------------------------

def test_SYT_CST_06_c1_the_sticker_decides_and_the_producers_compstate_does_not(harness_a):
    """Serves SYS-0022 (Accept: producer ERROR with sticker OK is used; producer RUNNING with sticker INVALID is not).
    The producer's compState is offered beside the sample as port ``in_X.compState``; the component declares no such
    input and ignores it (IMP-0024), so only the sticker can decide."""
    used = good_step(0, ["in_X"])
    used.producer_comp_state = {"in_X": "ERROR"}
    not_used = good_step(1, ["in_X"])
    not_used.samples["in_X"].sticker = SIG_INVALID
    not_used.producer_comp_state = {"in_X": "RUNNING"}

    first = harness_a.run(used)
    second = harness_a.run(not_used)

    assert first.ok and second.ok
    assert first.outputs["out"].sig_header.sig_status == SIG_OK
    assert first.outputs["out"].value == used.samples["in_X"].value
    assert first.outputs["compState"].state == first.outputs["compState"].RUNNING
    assert second.outputs["out"].sig_header.sig_status == SIG_INVALID
    assert second.outputs["compState"].state == second.outputs["compState"].ERROR
    assert "in_X" in second.outputs["compState"].reason
