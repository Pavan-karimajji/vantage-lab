# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_catalogue.py
# @brief LAB-40: SYT-RSY-04 on every catalogue file and resim case present, with the reference re-run (LAB-39), equal_outputs (LAB-38) and the run setups (LAB-36, LAB-37).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Contract test of fixture ``F-CATALOGUE`` (design section 6.3): for every file in ``catalogue/`` and every resim case of
every scenario, the car's recording is re-run through the reference re-run (``lab_tools.reference``, no Vantage) by its
refSyncs and the outputs must be equal (``RCT_System_Requirements.md``, *Equal outputs*). ``SYT-RSY-04`` has the one case
``.c1``; it is one test per catalogue case, the case in the test's id. Steps and PASS/FAIL sides: ``RCT_Test_Spec.md``.

The same file holds the checks of the rows that make the re-run possible: ``LAB-36`` (the run setup writer), ``LAB-37`` (the
provisional values and their one reader) and ``LAB-38`` (``equal_outputs``). Every check is a plain function that is also run
on a planted violation. Skipped, with the reason, when the build tree is absent or holds libraries of another operating system.
"""
import copy
import dataclasses
import itertools
import json
import random
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

from lab_tools import generate, setup_writer  # noqa: E402
from lab_tools.abi import load_index  # noqa: E402
from lab_tools.bundles import bundle_sha256, bundle_variant  # noqa: E402
from lab_tools.car import Car, find_index  # noqa: E402
from lab_tools.compare import equal_outputs  # noqa: E402
from lab_tools.messages import MessageFactory  # noqa: E402
from lab_tools.reference import Recording, ReferenceResim, ReferenceResimError  # noqa: E402
from lab_tools.setup_writer import (ProvisionalError, RunSetupError, load_provisional, provisional_values,  # noqa: E402
                                    write_run_setup)
from lab_tools.topology import IMAGE_KIND, TopologyError, load_topology  # noqa: E402

CATALOGUE = LAB_ROOT / "catalogue"
PROVISIONAL = CATALOGUE / "provisional.yaml"
SAMPLE_TYPE = "adas.lab.LabSample"

# ARCH section 2.3, the only keys a run setup may carry (impl.kind and impl.path from section 4's mapping table).
TOP_KEYS = {"schema", "run_id", "spec_sha256", "sources", "stages", "mutations", "outputs", "readiness_limit_steps",
            "fallback_max_gap", "checkpoint", "limits", "coherence_verdict"}
SOURCE_KEYS = {"path", "sha256", "codec", "ingest", "absent"}
STAGE_KEYS = {"name", "mode", "impl", "abi_version", "config_bundle_sha256", "trigger", "clock_period_ns", "ports",
              "inputs"}
IMPL_KEYS = {"kind", "path"}
PORT_KEYS = {"name", "dir", "kind", "proto_type", "record", "fixed_slots", "bulk", "buffer_depth"}
INPUT_KEYS = {"port", "producer", "producer_port", "previous_cycle"}
OUTPUT_KEYS = {"path", "codec", "livemeas"}
LIVEMEAS_KEYS = {"name", "cycle_slot_count", "cycle_slot_size", "bulk_slot_count", "bulk_slot_size"}
LIMITS_KEYS = {"exec_timeout_ms", "severity_exit"}
GONE_KEYS = {"sampling", "on_missing", "delay_steps", "grid_step_ns", "order", "period_ns"}


# ---- the catalogue, as parameters -----------------------------------------------------------------------------------
def catalogue_files():
    return sorted(p for p in CATALOGUE.glob("*.yaml") if p.name != "provisional.yaml")


def catalogue_cases():
    """(topology file stem, scenario, case) for every resim case of every catalogue file, with a readable id."""
    cases = []
    for file in catalogue_files():
        topology = load_topology(file)
        if topology.refused_at_load:
            continue  # no recording to re-run; test_lab48_... asserts the loader's refusal instead
        for scenario in topology.scenarios:
            for case in scenario.resim:
                cases.append(pytest.param(file.stem, scenario.name, case["name"],
                                          id=f"{file.stem}-{scenario.name}-{case['name']}"))
    return cases


def index_for(topology_name):
    path = find_index(topology_name, LAB_ROOT)
    if path is None or not path.is_file():
        pytest.skip(f"no build tree: conan_workarea/build-sil-*/lab/{topology_name}/index.json is absent "
                    "(run build.bat -P base -t sil -p <platform>, or set LAB_INDEX)")
    suffix = Path(next(iter(load_index(path)["components"].values()))["library"]).suffix
    if (suffix == ".dll") != (sys.platform == "win32"):
        pytest.skip(f"the build tree at {path} holds {suffix} libraries; this is {sys.platform}")
    return path


@pytest.fixture(scope="module")
def record(tmp_path_factory):
    """``record(topology_name, scenario_name)`` -> (topology, scenario, recording path, index path); one car run each."""
    done = {}

    def make(topology_name, scenario_name):
        key = (topology_name, scenario_name)
        if key not in done:
            topology = load_topology(CATALOGUE / f"{topology_name}.yaml")
            if topology.refused_at_load:
                pytest.skip(f"{topology_name} expects refusal at load; there is no recording to re-run")
            if any(s.kind == IMAGE_KIND for s in topology.sources):
                pytest.skip(f"{topology_name} has an image source; the car makes those in LAB-WP-08")
            index = index_for(topology_name)
            scenario = next(s for s in topology.scenarios if s.name == scenario_name)
            out = tmp_path_factory.mktemp(f"{topology_name}_{scenario_name}") / f"{scenario_name}.mcap"
            with Car(topology, scenario, index) as car:
                car.record(out)
            done[key] = (topology, scenario, out, index)
        return done[key]

    return make


@pytest.fixture(scope="module")
def provisional_run_setup():
    return provisional_values(load_provisional(PROVISIONAL, "run_setup"))


def case_of(scenario, name):
    return next(c for c in scenario.resim if c["name"] == name)


# ---- LAB-48: a file that expects refusal at load ----------------------------------------------------------------------
def refused_files():
    return [pytest.param(f, id=f.stem) for f in catalogue_files() if load_topology(f).refused_at_load]


@pytest.mark.parametrize("file", refused_files())
def test_lab48_a_file_expecting_refusal_is_skipped_by_the_re_run_and_refused_without_the_word(file, tmp_path, capsys):
    """The loader accepts the file only with ``expect: refused_at_load``; without that word it raises, naming the loop."""
    with capsys.disabled():
        print(f"\nSYT-RSY-04 {file.stem}: skipped, it expects refusal at load; there is no recording to re-run")
    topology = load_topology(file)
    assert all(s.expect == "refused_at_load" for s in topology.scenarios) and not any(s.resim for s in topology.scenarios)
    text = file.read_text(encoding="utf-8")
    assert "expect: refused_at_load" in text
    plain = tmp_path / file.name
    plain.write_text(text.replace("expect: refused_at_load", "expect: exact"), encoding="utf-8")
    with pytest.raises(TopologyError, match="loop through inputs not marked previous_cycle"):
        load_topology(plain)


# ---- SYT-RSY-04 ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("topology_name, scenario_name, case_name", catalogue_cases())
def test_SYT_RSY_04_c1_inputs_by_refsync_reproduce_the_car(record, topology_name, scenario_name, case_name):
    """SYS-0016 Accept: re-running each lab component on its recording by refSync gives dataOut equal to the recorded dataOut from the first RUN run on."""
    topology, scenario, recording, index = record(topology_name, scenario_name)
    case = case_of(scenario, case_name)
    if "jump_to_ms" in case:
        reason = "a jump is a Vantage host call (simEngineSeek); the lab's reference re-run has no jump"
        print(f"SYT-RSY-04.c1 {topology_name}/{scenario_name}/{case_name}: skipped, {reason}")
        pytest.skip(reason)
    result = ReferenceResim(topology, recording, index).run(case.get("measured", []))
    assert result.components, "no component was re-run, so nothing was proved"
    assert result.equal, "\n".join(result.describe())
    simulated = {c.name for c in topology.components} - set(case.get("measured", []))
    assert set(result.components) == simulated
    assert all(c.runs > 0 and c.mismatched_runs == 0 for c in result.components.values())


def test_SYT_RSY_04_c1_planted_mutated_sample_breaks_equality(record):
    """FAIL side of SYS-0016: one changed recorded sample must be found, naming topology, component and run."""
    topology, scenario, path, index = record("running_example", "base")
    recording = Recording.read(path)
    factory = MessageFactory(recording.schemas["source.X"][1])
    ref_sync = recording.topic("A.refSync")[10]  # the X sample A read in its 11th run
    counter = next(e.measurement_counter for e in factory.parse("adas.common.RefSync", ref_sync.payload).inputs
                   if e.port == "in_X")
    victim = next(i for i, m in enumerate(recording.messages) if m.topic == "source.X"
                  and factory.parse(SAMPLE_TYPE, m.payload).sig_header.measurement_counter == counter)
    sample = factory.parse(SAMPLE_TYPE, recording.messages[victim].payload)
    sample.value += 1
    recording.messages[victim] = dataclasses.replace(recording.messages[victim],
                                                     payload=sample.SerializeToString(deterministic=True))
    result = ReferenceResim(topology, recording, index).run()
    assert not result.equal
    lines = result.describe()
    assert lines and all(line.startswith("topology running_example component ") for line in lines)
    assert any(" run " in line for line in lines), lines
    assert {c for c, r in result.components.items() if not r.equal} >= {"A", "C"}, "A and C read X directly"
    # the untouched recording passes the same run, so the difference is the mutation
    assert ReferenceResim(topology, path, index).run().equal


def test_SYT_RSY_04_c1_planted_missing_output_is_refused(record):
    """A recording whose output count disagrees with its control count cannot be re-run: refused, naming component and port."""
    topology, scenario, path, index = record("running_example", "base")
    recording = Recording.read(path)
    victim = [i for i, m in enumerate(recording.messages) if m.topic == "B.compState"][10]
    del recording.messages[victim]
    with pytest.raises(ReferenceResimError, match="component B"):
        ReferenceResim(topology, recording, index).run()


def test_SYT_RSY_04_c1_planted_changed_producer_output_reaches_its_consumer(record, tmp_path):
    """FAIL side of SYS-0016, live feed: A re-run without one of its keys reproduces other outputs, and B, which reads them, differs too."""
    topology, scenario, path, index = record("running_example", "base")
    source = Path(load_index(index)["components"]["A"]["config"])
    bundle_variant(source, tmp_path / "A", remove=["LAB_A_MAX_AGE_US"])  # A then reports ERROR in every run
    resim = ReferenceResim(topology, path, index)
    changed = resim.run(config_dirs={"A": tmp_path / "A"})
    assert not changed.components["A"].equal
    assert not changed.components["B"].equal, "B reads A's reproduced output; fed the recorded one it would have passed"
    assert changed.components["C"].equal and changed.components["E"].equal, "C and E do not read A"
    # the same changed A, measured: it is not re-run, B reads the recorded A and is equal
    measured = resim.run(["A"], config_dirs={"A": tmp_path / "A"})
    assert measured.equal
    # and the unchanged A, simulated, passes
    assert resim.run().equal


def test_SYT_RSY_04_c1_planted_missing_producer_run_is_reported_not_passed(record):
    """FAIL side of SYS-0016, readiness: a run of simulated A that B's refSync needs is gone; B waits and the re-run reports it."""
    topology, scenario, path, index = record("running_example", "base")
    recording = Recording.read(path)
    factory = MessageFactory(recording.schemas["source.X"][1])
    ref_sync = factory.parse("adas.common.RefSync", recording.topic("B.refSync")[10].payload)
    counter = next(e.measurement_counter for e in ref_sync.inputs if e.port == "in_A")
    run = next(i for i, m in enumerate(recording.topic("A.out"))
               if factory.parse(SAMPLE_TYPE, m.payload).sig_header.measurement_counter == counter)
    for port in ("control", "out", "refSync", "compState"):  # the whole run, so A's own counts still agree
        seen = -1
        for i, m in enumerate(recording.messages):
            if m.topic == f"A.{port}":
                seen += 1
                if seen == run:
                    del recording.messages[i]
                    break
    resim = ReferenceResim(topology, recording, index)
    for order, interleave in ((None, False), (["E", "D", "C", "B", "A"], False), (["B", "A", "C", "D", "E"], True)):
        with pytest.raises(ReferenceResimError, match=rf"component B run 11 waits: input in_A needs counter {counter} of A.out"):
            resim.run(order=order, interleave=interleave)


def test_SYT_RSY_04_c1_any_cut_gives_equal_outputs(record):
    """Every subset of the five stages measured (the others re-run, each fed by the simulated producers) reproduces the recording."""
    topology, scenario, path, index = record("running_example", "base")
    resim = ReferenceResim(topology, path, index)
    names = [c.name for c in topology.components]
    for size in range(len(names) + 1):
        for measured in itertools.combinations(names, size):
            result = resim.run(measured)
            assert result.equal, f"measured {measured}: " + "; ".join(result.describe())
            assert set(result.components) == set(names) - set(measured)


@pytest.mark.parametrize("scenario_name", ["base", "restart", "fault"])
def test_SYT_RSY_04_c1_any_run_order_gives_equal_outputs(record, scenario_name):
    """Reversed, shuffled and interleaved run orders (a consumer waits for its simulated producer) give the same equal outputs."""
    topology, scenario, path, index = record("running_example", scenario_name)
    resim = ReferenceResim(topology, path, index)
    names = [c.name for c in topology.components]
    shuffled = list(names)
    random.Random(7).shuffle(shuffled)
    for order, interleave in ((names[::-1], False), (shuffled, False), (names, True), (shuffled, True)):
        result = resim.run(order=order, interleave=interleave)
        assert result.equal, f"order {order} interleave {interleave}: " + "; ".join(result.describe())


@pytest.mark.parametrize("topology_name, scenario_name, case_name", catalogue_cases())
def test_SYT_RSY_04_without_ref_sync_the_mismatched_runs_are_counted(record, capsys, topology_name, scenario_name, case_name):
    """use_ref_sync=False hands the time rule's sample; the runs that then differ are counted, never asserted to be none."""
    topology, scenario, recording, index = record(topology_name, scenario_name)
    case = case_of(scenario, case_name)
    if "jump_to_ms" in case:
        pytest.skip("a jump is a Vantage host call (simEngineSeek)")
    resim = ReferenceResim(topology, recording, index)
    with_sync = resim.run(case.get("measured", []))
    without = resim.run(case.get("measured", []), use_ref_sync=False)
    counts = {name: c.mismatched_runs for name, c in without.components.items()}
    with capsys.disabled():
        print(f"\nSYT-RSY-04 use_ref_sync=False {topology_name}/{scenario_name}/{case_name}: "
              f"{without.mismatched_runs} differing runs {counts}")
    assert not without.use_ref_sync and with_sync.use_ref_sync
    assert set(without.components) == set(with_sync.components)
    assert without.mismatched_runs == sum(counts.values())
    assert all(0 <= n <= without.components[name].runs for name, n in counts.items())
    assert with_sync.mismatched_runs == 0
    if (topology_name, scenario_name) == ("running_example", "base"):
        # the model's second check: B's inputs are picked late (jitter 4 ms against 10 ms samples), so the time rule misses
        assert counts.get("B", 0) > 0, "the time rule matched every run of B; the refSync would then be no evidence"


# ---- LAB-38 ----------------------------------------------------------------------------------------------------------
def sample_bytes(factory, value, cycle_counter=1, counter=1):
    message = factory.new(SAMPLE_TYPE, value=value)
    message.sig_header.measurement_counter = counter
    message.sig_header.cycle_counter = cycle_counter
    return message.SerializeToString(deterministic=True)


@pytest.fixture(scope="module")
def sample_factory(record):
    _, _, path, _ = record("running_example", "base")
    return MessageFactory(Recording.read(path).schemas["source.X"][1])


def test_lab38_equal_outputs_ignores_only_the_cycle_counter(sample_factory):
    a = [sample_bytes(sample_factory, 5, cycle_counter=1), sample_bytes(sample_factory, 6, cycle_counter=2)]
    b = [sample_bytes(sample_factory, 5, cycle_counter=9), sample_bytes(sample_factory, 6, cycle_counter=77)]
    assert equal_outputs(a, b, sample_factory, SAMPLE_TYPE)
    assert equal_outputs([], [], sample_factory, SAMPLE_TYPE)


def test_lab38_planted_differences_are_found(sample_factory):
    a = [sample_bytes(sample_factory, 5), sample_bytes(sample_factory, 6)]
    shorter = equal_outputs(a, a[:1], sample_factory, SAMPLE_TYPE)
    assert not shorter and shorter.first_difference is None and (shorter.recorded_count, shorter.reproduced_count) == (2, 1)
    other_value = equal_outputs(a, [a[0], sample_bytes(sample_factory, 7)], sample_factory, SAMPLE_TYPE)
    assert not other_value and other_value.first_difference == 1
    other_counter = equal_outputs(a, [a[0], sample_bytes(sample_factory, 6, counter=2)], sample_factory, SAMPLE_TYPE)
    assert not other_counter and other_counter.first_difference == 1


# ---- LAB-37 ----------------------------------------------------------------------------------------------------------
def test_lab37_run_setup_values_are_those_of_section_7_and_marked_provisional():
    entries = load_provisional(PROVISIONAL, "run_setup")
    leaves = {key: leaf for key, leaf in setup_writer._leaves(entries)}
    assert leaves and all(leaf["status"] == "provisional" for leaf in leaves.values())
    values = provisional_values(entries)
    assert values["readiness_limit_steps"] == 8 and values["fallback_max_gap"] == 6 and values["buffer_depth"] == 16
    assert values["livemeas"] == {"cycle_slot_count": 8, "cycle_slot_size": 16384, "bulk_slot_count": 4,
                                  "bulk_slot_size": 8192}


def test_lab37_one_file_one_reader():
    assert generate.load_provisional is setup_writer.load_provisional
    assert "calibration" in setup_writer.PROVISIONAL_PARTS
    calibration = load_provisional(PROVISIONAL)
    assert calibration["running_example"]["B"]["LAB_B_MAX_AGE_US"]["status"] == "provisional"


def test_lab37_planted_unmarked_or_missing_values_are_refused(tmp_path):
    text = PROVISIONAL.read_text(encoding="utf-8")
    unmarked = tmp_path / "unmarked.yaml"
    unmarked.write_text(text.replace("fallback_max_gap: {value: 6, status: provisional}", "fallback_max_gap: {value: 6}"),
                        encoding="utf-8")
    with pytest.raises(ProvisionalError, match="fallback_max_gap"):
        load_provisional(unmarked, "run_setup")
    absent = tmp_path / "absent.yaml"
    absent.write_text(text.split("run_setup:")[0], encoding="utf-8")
    with pytest.raises(ProvisionalError, match="run_setup"):
        load_provisional(absent, "run_setup")
    with pytest.raises(ProvisionalError, match="not a part"):
        load_provisional(PROVISIONAL, "nothing")


# ---- LAB-36 ----------------------------------------------------------------------------------------------------------
def foreign_keys(setup):
    """Keys of a run setup that ARCH section 2.3 does not list (and the gone ones), as dotted paths."""
    bad = [k for k in setup if k not in TOP_KEYS]
    bad += [f"sources.{k}" for s in setup["sources"] for k in s if k not in SOURCE_KEYS]
    for stage in setup["stages"]:
        where = f"stages[{stage['name']}]"
        bad += [f"{where}.{k}" for k in stage if k not in STAGE_KEYS]
        bad += [f"{where}.impl.{k}" for k in stage["impl"] if k not in IMPL_KEYS]
        bad += [f"{where}.ports.{k}" for p in stage["ports"] for k in p if k not in PORT_KEYS]
        bad += [f"{where}.inputs.{k}" for i in stage["inputs"] for k in i if k not in INPUT_KEYS]
    bad += [f"outputs.{k}" for k in setup["outputs"] if k not in OUTPUT_KEYS]
    bad += [f"outputs.livemeas.{k}" for k in setup["outputs"]["livemeas"] if k not in LIVEMEAS_KEYS]
    bad += [f"limits.{k}" for k in setup["limits"] if k not in LIMITS_KEYS]
    return bad


def write(record, provisional_run_setup, tmp_path, scenario_name="base", case_name="cut_c", case=None):
    topology, scenario, recording, index = record("running_example", scenario_name)
    case = case if case is not None else case_of(scenario, case_name)
    path = write_run_setup(topology, scenario, case, recording=recording, index=load_index(index), out_dir=tmp_path,
                           run_setup_values=provisional_run_setup)
    return topology, json.loads(path.read_text(encoding="utf-8")), path


@pytest.mark.parametrize("topology_name, scenario_name, case_name", catalogue_cases())
def test_lab36_writes_one_run_setup_per_resim_case_with_the_keys_of_arch_2_3_only(
        record, provisional_run_setup, tmp_path, topology_name, scenario_name, case_name):
    topology, scenario, recording, index = record(topology_name, scenario_name)
    case = case_of(scenario, case_name)
    path = write_run_setup(topology, scenario, case, recording=recording, index=load_index(index), out_dir=tmp_path,
                           run_setup_values=provisional_run_setup)
    setup = json.loads(path.read_text(encoding="utf-8"))
    assert foreign_keys(setup) == []
    assert not (GONE_KEYS & set(setup)) and not any(GONE_KEYS & set(p) for s in setup["stages"] for p in s["ports"])
    assert setup["outputs"]["livemeas"]["name"] == f"lab_{topology_name}_{scenario_name}_{case_name}"
    assert [s["name"] for s in setup["stages"]] == [c.name for c in topology.components]
    measured = set(case.get("measured", []))
    assert {s["name"] for s in setup["stages"] if s["mode"] == "measured"} == measured
    assert "jump_to_ms" not in json.dumps(setup), "a jump is a host call, not a run setup key"
    assert setup["sources"][0]["path"].endswith(recording.name) and len(setup["sources"][0]["sha256"]) == 64


def test_lab36_planted_foreign_keys_are_found(record, provisional_run_setup, tmp_path):
    _, setup, _ = write(record, provisional_run_setup, tmp_path)
    assert foreign_keys(setup) == []
    broken = copy.deepcopy(setup)
    broken["stages"][0]["ports"][0]["sampling"] = "strict"
    broken["stages"][0]["period_ns"] = 20_000_000
    broken["grid_step_ns"] = 1
    assert set(foreign_keys(broken)) == {"stages[A].ports.sampling", "stages[A].period_ns", "grid_step_ns"}


def test_lab36_the_provisional_values_are_written_and_listed_as_provisional(record, provisional_run_setup, tmp_path):
    _, setup, path = write(record, provisional_run_setup, tmp_path)
    assert (setup["readiness_limit_steps"], setup["fallback_max_gap"]) == (8, 6)
    assert {p["buffer_depth"] for s in setup["stages"] for p in s["ports"]} == {16}
    live = setup["outputs"]["livemeas"]
    assert (live["cycle_slot_count"], live["cycle_slot_size"], live["bulk_slot_count"], live["bulk_slot_size"]) == \
        (8, 16384, 4, 8192)
    sidecar = json.loads(path.with_name(path.name.replace(".run.json", ".run.provisional.json")).read_text(encoding="utf-8"))
    assert sidecar["status"] == "provisional" and sidecar["run_setup"] == path.name
    assert {"readiness_limit_steps", "fallback_max_gap", "stages[].ports[].buffer_depth",
            "outputs.livemeas.cycle_slot_size", "outputs.livemeas.bulk_slot_size"} <= set(sidecar["values"])


def test_lab36_the_cut_trigger_and_wiring_follow_the_case(record, provisional_run_setup, tmp_path):
    topology, setup, _ = write(record, provisional_run_setup, tmp_path)
    stages = {s["name"]: s for s in setup["stages"]}
    assert stages["C"]["mode"] == "measured" and stages["A"]["mode"] == "simulate"
    assert all(s["trigger"] == "control" and s["clock_period_ns"] == 0 for s in stages.values())
    assert [(i["port"], i["producer"], i["producer_port"]) for i in stages["B"]["inputs"]] == [
        ("in_A", "A", "out"), ("in_X", "source", "X"), ("in_Y", "source", "Y"), ("in_Z", "source", "Z")]
    assert [p["name"] for p in stages["B"]["ports"] if p["dir"] == "req"] == ["control", "in_A", "in_X", "in_Y", "in_Z"]
    triggered = {"name": "t", "trigger": {"B": "in_Z"}}
    _, other, _ = write(record, provisional_run_setup, tmp_path, case=triggered)
    assert {s["name"]: s["trigger"] for s in other["stages"]}["B"] == "in_Z"


def test_lab36_a_stress_case_overrides_the_livemeas_geometry(record, provisional_run_setup, tmp_path):
    case = {"name": "stress", "livemeas": {"cycle_slot_count": 2}}
    _, setup, _ = write(record, provisional_run_setup, tmp_path, case=case)
    live = setup["outputs"]["livemeas"]
    assert live["cycle_slot_count"] == 2 and live["cycle_slot_size"] == 16384
    assert live["name"] == "lab_running_example_base_stress"


def test_lab36_a_config_case_points_at_a_repacked_bundle(record, provisional_run_setup, tmp_path):
    case = {"name": "cal", "config": {"B": {"remove": ["LAB_B_MAX_AGE_US"]}}}
    topology, setup, path = write(record, provisional_run_setup, tmp_path, case=case)
    stages = {s["name"]: s for s in setup["stages"]}
    folder = tmp_path / "base.cal.bundles" / "B"
    assert "LAB_B_MAX_AGE_US" not in (folder / "default.yaml").read_text(encoding="utf-8")
    assert (folder / "bundle.sha256").read_text(encoding="utf-8").strip() == bundle_sha256(folder) \
        == stages["B"]["config_bundle_sha256"]
    _, plain, _ = write(record, provisional_run_setup, tmp_path / "plain", case={"name": "plain"})
    plain_stages = {s["name"]: s for s in plain["stages"]}
    assert plain_stages["B"]["config_bundle_sha256"] != stages["B"]["config_bundle_sha256"]
    assert all(plain_stages[n]["config_bundle_sha256"] == stages[n]["config_bundle_sha256"] for n in "ACDE")
    write(record, provisional_run_setup, tmp_path, case=case)  # a second write replaces its own bundle copy


def test_lab36_a_bulk_case_flags_the_ports_it_names(record, provisional_run_setup, tmp_path):
    _, setup, _ = write(record, provisional_run_setup, tmp_path, case={"name": "bulk", "bulk": ["source.X", "A.out"]})
    flagged = {(s["name"], p["name"]) for s in setup["stages"] for p in s["ports"] if p["bulk"]}
    assert flagged == {("A", "in_X"), ("B", "in_X"), ("C", "in_X"), ("A", "out"), ("B", "in_A")}


@pytest.mark.parametrize("case, text", [
    ({"name": "x", "measured": ["Q"]}, "measured names 'Q'"),
    ({"name": "x", "trigger": {"Q": "control"}}, "trigger names 'Q'"),
    ({"name": "x", "trigger": {"B": "in_Q"}}, "must be 'control' or one of its inputs"),
    ({"name": "x", "config": {"Q": {"set": {"K": 1}}}}, "config names 'Q'"),
    ({"name": "x", "config": {"B": {"sampling": "strict"}}}, "config of B"),
    ({"name": "x", "bulk": ["source.Q"]}, "no source 'Q'"),
    ({"name": "x", "bulk": ["B.nothing"]}, "not a port"),
    ({"name": "x", "livemeas": {"ring": 3}}, "may override only"),
    ({"name": "x", "sampling": "strict"}, "unknown key"),
    ({"name": "x", "on_missing": "hold"}, "unknown key"),
    ({"name": "x", "jump_to_ms": 99999}, "jump_to_ms"),
    ({"measured": []}, "needs a name"),
])
def test_lab36_a_case_naming_something_unknown_is_refused(record, provisional_run_setup, tmp_path, case, text):
    with pytest.raises(RunSetupError, match=text):
        write(record, provisional_run_setup, tmp_path, case=case)
    assert not list(tmp_path.glob("*.run.json")), "nothing is written for a refused case"


def test_lab36_a_missing_provisional_value_is_refused_not_defaulted(record, provisional_run_setup, tmp_path):
    values = {k: v for k, v in provisional_run_setup.items() if k != "fallback_max_gap"}
    with pytest.raises(RunSetupError, match="fallback_max_gap"):
        write(record, values, tmp_path)
