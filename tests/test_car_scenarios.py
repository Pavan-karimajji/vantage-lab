# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_car_scenarios.py
# @brief LAB-35: SYT-CTL-07 and SYT-CST-01..05 on the fake car's recordings of the running example scenarios restart and fault, and on a calibration variant of a bundle.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Contract tests of fixtures ``F-LAB-FAULT``, ``F-LAB-RESTART`` and ``F-LAB-CAL`` (design section 6.3). Each
``SYT`` case ``.cN`` is one test named ``test_SYT_<AREA>_<nn>_c<N>_...``; its docstring cites the ``SYS``
requirement whose ``Accept`` decides it (R-REQ-4, R-REQ-9). Steps and PASS/FAIL sides: ``RCT_Test_Spec.md``.
The windows are in time, so jitter makes run counts differ by one (design section 6.2): the tests read the
first run of each window and the recovery, never a count. Every check is a plain function that is also run
on a planted violation. Skipped, with the reason, when the build tree is absent (see ``test_car_running_example``).
"""
import dataclasses
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

from lab_tools.abi import InputPort, LabLibrary, PortSet, load_index  # noqa: E402
from lab_tools.bundles import BundleError, bundle_sha256, bundle_variant  # noqa: E402
from lab_tools.car import Car, CarError, SIG_INVALID, SIG_OK  # noqa: E402
from lab_tools.messages import MessageFactory  # noqa: E402
from lab_tools.topology import load_topology  # noqa: E402
from test_car_running_example import index_path, read_recording, stream, topology  # noqa: E402,F401

COMPONENTS = ["A", "B", "C", "D", "E"]
ERROR, RUNNING = 2, 1                      # CompState.State
OP_RESTART, OP_RUN = 0, 1                  # CycleControl.OpMode
SILENT_FROM_MS, SILENT_FOR_MS = 400, 920   # section 6.2
INVALID_FROM_MS, INVALID_FOR_MS = 1320, 800
HEALTHY_FROM_US = (INVALID_FROM_MS + INVALID_FOR_MS) * 1000
Z_PORT = "in_Z"


def scenario_of(topology, name):
    return next(s for s in topology.scenarios if s.name == name)


def record(topology, scenario_name, index_path, tmp_path_factory):
    out = tmp_path_factory.mktemp(scenario_name) / f"{scenario_name}.mcap"
    with Car(topology, scenario_of(topology, scenario_name), index_path) as car:
        car.record(out)
    return read_recording(out)


@pytest.fixture(scope="module")
def base(index_path, topology, tmp_path_factory):
    return record(topology, "base", index_path, tmp_path_factory)


@pytest.fixture(scope="module")
def fault(index_path, topology, tmp_path_factory):
    return record(topology, "fault", index_path, tmp_path_factory)


@pytest.fixture(scope="module")
def restart(index_path, topology, tmp_path_factory):
    return record(topology, "restart", index_path, tmp_path_factory)


@pytest.fixture(scope="module")
def max_age_us(index_path):
    return config_value(index_path, "B", "LAB_B_MAX_AGE_US")


@pytest.fixture(scope="module")
def recovery_runs(index_path):
    return config_value(index_path, "B", "LAB_B_RECOVERY_RUNS")


def config_value(index_path, component, key):
    folder = Path(load_index(index_path)["components"][component]["config"])
    for line in (folder / "default.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + ":"):
            return int(line.split(":", 1)[1])
    raise AssertionError(f"{key} is not in {folder}")


# ---- checks (each also run on a planted violation) -------------------------------------------------------------
def op_modes(messages, component):
    """The ``op_mode`` of each of a component's controls, in run order, as names."""
    return [{OP_RESTART: "RESTART", OP_RUN: "RUN"}[m.op_mode] for m in stream(messages, f"{component}.control")]


def runs_of(messages, component):
    """(control time in us, control, compState, out, refSync) per run of ``component``, in run order."""
    times = [ts for t, ts, _ in messages if t == f"{component}.control"]
    return list(zip(times, *(stream(messages, f"{component}.{p}") for p in ("control", "compState", "out", "refSync"))))


def z_entry(ref_sync):
    return next(e for e in ref_sync.inputs if e.port == Z_PORT)


def count_defects(messages):
    """Components whose number of compState messages differs from their number of control messages."""
    return [c for c in COMPONENTS
            if len(stream(messages, f"{c}.control")) != len(stream(messages, f"{c}.compState"))]


def bad_input_defects(comp_state, out, port):
    """What is wrong with a run that must report ERROR for ``port`` with INVALID outputs (SYS-0018)."""
    bad = []
    if comp_state.state != ERROR:
        bad.append("state")
    if port not in comp_state.reason:
        bad.append("reason")
    if out.sig_header.sig_status != SIG_INVALID:
        bad.append("sticker")
    return bad


def untrusted(ref_sync, control_time_us, max_age):
    """True when a declared input was not read, was not OK, or was older than ``max_age`` at the control time."""
    return any(e.measurement_counter == 0 or e.sig_status != SIG_OK
               or control_time_us - e.source_timestamp_us > max_age for e in ref_sync.inputs)


def sticker_defects(messages):
    return [(t, ts) for t, ts, m in messages if t.endswith(".compState") and m.sig_header.sig_status != SIG_OK]


@dataclasses.dataclass
class Fake:
    """A stand-in message for a planted violation: only the fields a check reads."""
    op_mode: int = OP_RUN


# ---- LAB-31 / LAB-32: what the scripts did ----------------------------------------------------------------------
def test_lab31_restart_is_on_the_first_run_and_on_the_run_the_scenario_lists(restart, base):
    modes = op_modes(restart, "D")
    assert [i + 1 for i, m in enumerate(modes) if m == "RESTART"] == [1, 30]
    assert all(set(op_modes(restart, c)) == {"RESTART", "RUN"} and op_modes(restart, c).count("RESTART") == 1
               for c in COMPONENTS if c != "D")
    assert op_modes(base, "D").count("RESTART") == 1


def test_lab32_silent_source_logs_nothing_in_its_window_and_its_counter_does_not_advance(fault):
    z = [(ts, m.sig_header) for t, ts, m in fault if t == "source.Z"]
    start, end = SILENT_FROM_MS * 1000, (SILENT_FROM_MS + SILENT_FOR_MS) * 1000
    assert not [h for _, h in z if start <= h.source_timestamp_us < end]
    assert [h.measurement_counter for _, h in z] == list(range(1, len(z) + 1)), "the counter steps by 1 across the gap"
    before = [h for _, h in z if h.source_timestamp_us < start]
    after = [h for _, h in z if h.source_timestamp_us >= end]
    assert before and after and after[0].measurement_counter == before[-1].measurement_counter + 1


def test_lab32_invalid_source_samples_carry_the_invalid_sticker_only_inside_the_window(fault):
    start = INVALID_FROM_MS * 1000
    for _, ts, m in [x for x in fault if x[0] == "source.Z"]:
        inside = start <= m.sig_header.source_timestamp_us < HEALTHY_FROM_US
        assert m.sig_header.sig_status == (SIG_INVALID if inside else SIG_OK), ts
    assert {m.sig_header.sig_status for t, _, m in fault if t in ("source.X", "source.Y")} == {SIG_OK}


def test_lab31_lab32_the_car_refuses_verbs_it_does_not_have(index_path, topology):
    base_scenario = scenario_of(topology, "base")
    for car, recording in (({"renumber": ["A"]}, {}), ({}, {"drop_ref_sync": "all"})):
        with pytest.raises(CarError):
            Car(topology, dataclasses.replace(base_scenario, car=car, recording=recording), index_path)
    with pytest.raises(CarError, match="not a component"):
        Car(topology, dataclasses.replace(base_scenario, car={"restart": [{"component": "Q", "at_run": 2}]}), index_path)


# ---- SYT-CTL-07 ----------------------------------------------------------------------------------------------
def test_SYT_CTL_07_c1_error_leaves_the_op_mode_alone(fault, base):
    """SYS-0012 Accept: B's op_mode sequence in the fault run equals that of the same run without the fault."""
    errors = [c for _, _, c, _, _ in runs_of(fault, "B") if c.state == ERROR]
    assert len(errors) >= 20, "the fault must make B report ERROR for 20 runs or more, or this case proves nothing"
    assert op_modes(fault, "B") == op_modes(base, "B")


def test_SYT_CTL_07_c1_planted_difference_is_found(fault, base):
    changed = op_modes(base, "B")
    changed[10] = "RESTART"
    assert op_modes(fault, "B") != changed


# ---- SYT-CST-01 ----------------------------------------------------------------------------------------------
def test_SYT_CST_01_c1_compstate_every_run_with_faults_and_a_restart(fault, restart, base):
    """SYS-0017 Accept: the number of compState messages equals the number of control messages."""
    for messages in (fault, restart, base):
        assert count_defects(messages) == []
    assert len(stream(fault, "B.control")) > 50


def test_SYT_CST_01_c1_planted_missing_compstate_is_found(fault):
    victim = [i for i, x in enumerate(fault) if x[0] == "B.compState"][10]
    dropped = fault[:victim] + fault[victim + 1:]
    assert len(dropped) == len(fault) - 1
    assert count_defects(dropped) == ["B"]


# ---- SYT-CST-02 ----------------------------------------------------------------------------------------------
def test_SYT_CST_02_c1_first_stale_run_is_error_naming_the_port_with_invalid_outputs(fault, max_age_us):
    """SYS-0018 Accept: the first run after the input goes bad has state 2, the port in reason, outputs INVALID."""
    runs = runs_of(fault, "B")
    first = next(i for i, (ts, _, _, _, rs) in enumerate(runs) if untrusted(rs, ts, max_age_us))
    ts, _, comp_state, out, ref_sync = runs[first]
    assert SILENT_FROM_MS * 1000 < ts < (SILENT_FROM_MS + SILENT_FOR_MS) * 1000, "inside the silent window"
    assert z_entry(ref_sync).sig_status == SIG_OK and ts - z_entry(ref_sync).source_timestamp_us > max_age_us, "stale"
    assert bad_input_defects(comp_state, out, Z_PORT) == [], (ts, comp_state.reason)
    assert all(c.state == RUNNING for _, _, c, _, _ in runs[:first][-3:]), "the run before was healthy"


def test_SYT_CST_02_c2_first_invalid_run_is_error_naming_the_port_with_invalid_outputs(fault):
    """SYS-0018 Accept: the first run that read an INVALID sample has state 2, the port in reason, outputs INVALID."""
    runs = runs_of(fault, "B")
    ts, _, comp_state, out, ref_sync = next(r for r in runs if z_entry(r[4]).sig_status == SIG_INVALID)
    assert INVALID_FROM_MS * 1000 <= ts < HEALTHY_FROM_US + 100_000
    assert bad_input_defects(comp_state, out, Z_PORT) == [], (ts, comp_state.reason)


def test_SYT_CST_02_planted_wrong_state_reason_and_sticker_are_found(fault):
    runs = runs_of(fault, "B")
    _, _, comp_state, out, _ = next(r for r in runs if r[2].state == ERROR)
    assert bad_input_defects(comp_state, out, Z_PORT) == []
    _, _, running, running_out, _ = next(r for r in runs if r[2].state == RUNNING)
    assert bad_input_defects(running, running_out, Z_PORT) == ["state", "reason", "sticker"]
    assert bad_input_defects(comp_state, out, "in_Q") == ["reason"]


# ---- SYT-CST-04 ----------------------------------------------------------------------------------------------
def test_SYT_CST_04_c1_error_clears_within_the_recovery_runs_without_restart(fault, max_age_us, recovery_runs):
    """SYS-0020 Accept: once the input is trusted, RUNNING and OK within the declared recovery runs, op_mode RUN."""
    runs = runs_of(fault, "B")
    after = [r for r in runs if r[0] >= HEALTHY_FROM_US]
    first_trusted = next(i for i, (ts, _, _, _, rs) in enumerate(after) if not untrusted(rs, ts, max_age_us))
    window = after[first_trusted:first_trusted + recovery_runs]
    assert len(window) == recovery_runs
    assert recovery_defects(window) == []
    assert window[-1][2].state == RUNNING and window[-1][3].sig_header.sig_status == SIG_OK
    assert all(c.state == RUNNING and o.sig_header.sig_status == SIG_OK for _, _, c, o, _ in after[first_trusted + recovery_runs - 1:])
    first_error = next(r for r in runs if r[2].state == ERROR)
    resumed = next(r for r in after[first_trusted:] if r[2].state == RUNNING)
    between = [r for r in runs if first_error[0] < r[0] <= resumed[0] and not untrusted(r[4], r[0], max_age_us)]
    print(f"\nfault run, B: first ERROR at {first_error[0] / 1000:g} ms (reason {first_error[2].reason!r}); first RUNNING "
          f"after recovery at {resumed[0] / 1000:g} ms; trusted runs from the first trusted one to it, inclusive: {len(between)}")


def recovery_defects(window):
    """Defects in the runs from the first trusted run to the last allowed recovery run: not RUNNING-and-OK at
    the end, or an op_mode other than RUN anywhere."""
    bad = [i for i, (_, control, _, _, _) in enumerate(window) if control.op_mode != OP_RUN]
    last = window[-1]
    if last[2].state != RUNNING or last[3].sig_header.sig_status != SIG_OK:
        bad.append("not recovered")
    return bad


def test_SYT_CST_04_c1_planted_late_recovery_and_restart_are_found(fault, recovery_runs):
    runs = runs_of(fault, "B")
    error_runs = [r for r in runs if r[2].state == ERROR][:recovery_runs]
    assert recovery_defects(error_runs) == ["not recovered"]
    restarted = [(ts, Fake(op_mode=OP_RESTART), c, o, rs) for ts, _, c, o, rs in runs[-recovery_runs:]]
    assert recovery_defects(restarted)


# ---- SYT-CST-05 ----------------------------------------------------------------------------------------------
def test_SYT_CST_05_c1_every_compstate_sticker_is_ok(fault, restart, base):
    """SYS-0021 Accept: no compState header has sig_status other than 1, over RESTART, ERROR and recovery."""
    for messages in (fault, restart, base):
        assert sticker_defects(messages) == []
    assert {c.state for _, _, c, _, _ in runs_of(fault, "B")} >= {ERROR, RUNNING}


def test_SYT_CST_05_c1_planted_sticker_is_found(fault):
    victim = [i for i, x in enumerate(fault) if x[0] == "B.compState"][10]
    planted = list(fault)
    planted[victim] = (fault[victim][0], fault[victim][1], _with_sticker(fault[victim][2], SIG_INVALID))
    assert sticker_defects(planted) == [fault[victim][:2]]


def _with_sticker(message, sticker):
    copy = type(message)()
    copy.CopyFrom(message)
    copy.sig_header.sig_status = sticker
    return copy


# ---- SYT-CST-03 (F-LAB-CAL) ----------------------------------------------------------------------------------
def drive(library, factory, run, op_mode):
    """One run of a library with a hand-made control and no data input offered; its compState and out."""
    cls = factory.message_class("adas.common.CycleControl")
    control = factory.new("adas.common.CycleControl", op_mode=cls.OpMode.Value(op_mode))
    control.sig_header.measurement_counter = run
    control.sig_header.source_timestamp_us = 1000 * run
    control.sig_header.cycle_counter = run
    control.sig_header.sig_status = SIG_OK
    manifest = library.manifest()
    inputs = PortSet.for_inputs([InputPort("control", control.SerializeToString())])
    outputs = PortSet.for_outputs([p.name for p in manifest.outputs], 4096)
    assert library.exec(inputs, outputs, 0.0)
    written = outputs.outputs()
    return (factory.parse("adas.df.CompState", written["compState"]), factory.parse("adas.lab.LabSample", written["out"]))


def run_with_bundle(index_path, bundle_dir, runs):
    entry = load_index(index_path)["components"]["B"]
    with LabLibrary.load(entry["library"]) as library:
        factory = MessageFactory(library.manifest().descriptor_set)
        library.init(bundle_dir)
        return [drive(library, factory, run, "RESTART" if run == 1 else "RUN") for run in range(1, runs + 1)]


def calibration_defects(comp_state, out, key):
    bad = []
    if comp_state.state != ERROR:
        bad.append("state")
    if key not in comp_state.reason:
        bad.append("reason")
    if out.sig_header.sig_status != SIG_INVALID:
        bad.append("sticker")
    return bad


def test_SYT_CST_03_c1_a_removed_calibration_key_gives_error_naming_it_with_invalid_outputs(index_path, tmp_path):
    """SYS-0019 Accept: state 2, reason naming the key, dataOut sig_status 2 in the first RUN run (and after it)."""
    source = Path(load_index(index_path)["components"]["B"]["config"])
    key = "LAB_B_MAX_AGE_US"
    bundle_variant(source, tmp_path / "variant", remove=[key])
    results = run_with_bundle(index_path, tmp_path / "variant", 4)
    assert results[0][0].state != ERROR, "the RESTART run is INIT, not the first RUN run"
    for comp_state, out in results[1:]:
        assert calibration_defects(comp_state, out, key) == [], comp_state.reason
    healthy = run_with_bundle(index_path, source, 2)[1]
    assert calibration_defects(*healthy, key) == ["reason"], "planted: the built bundle gives ERROR, but not for the key"
    assert calibration_defects(healthy[0], healthy[1], "input") == [], "planted: the same run passes when asked for another reason"


# ---- LAB-34: bundle_variant ----------------------------------------------------------------------------------
def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def test_lab34_variant_repacks_with_a_new_hash_and_leaves_the_built_bundle_alone(index_path, tmp_path):
    source = Path(load_index(index_path)["components"]["B"]["config"])
    before = tree(source)
    same = bundle_variant(source, tmp_path / "same")
    assert same == (source / "bundle.sha256").read_text(encoding="utf-8").strip(), "the build's hash algorithm"
    digest = bundle_variant(source, tmp_path / "v", set_values={"LAB_B_RECOVERY_RUNS": 5, "LAB_B_NEW_US": 7},
                            remove=["LAB_B_HANG_AFTER_RUNS"])
    assert digest != same and digest == bundle_sha256(tmp_path / "v")
    assert (tmp_path / "v" / "bundle.sha256").read_text(encoding="utf-8") == digest + "\n"
    text = (tmp_path / "v" / "default.yaml").read_text(encoding="utf-8")
    assert "LAB_B_RECOVERY_RUNS: 5\n" in text and "LAB_B_NEW_US: 7\n" in text and "LAB_B_HANG_AFTER_RUNS" not in text
    assert "LAB_B_MAX_AGE_US: 118000\n" in text
    assert tree(source) == before


def test_lab34_variant_refuses_what_would_touch_or_hide_something(index_path, tmp_path):
    source = Path(load_index(index_path)["components"]["B"]["config"])
    for destination in (source, source / "inside", source.parent):
        with pytest.raises(BundleError, match="separate folder"):
            bundle_variant(source, destination)
    with pytest.raises(BundleError, match="not present"):
        bundle_variant(source, tmp_path / "a", remove=["LAB_B_NOT_A_KEY"])
    with pytest.raises(BundleError, match="both set and removed"):
        bundle_variant(source, tmp_path / "b", set_values={"LAB_B_MAX_AGE_US": 1}, remove=["LAB_B_MAX_AGE_US"])
    with pytest.raises(BundleError, match="ALL_CAPS"):
        bundle_variant(source, tmp_path / "c", set_values={"lab_b": 1})
