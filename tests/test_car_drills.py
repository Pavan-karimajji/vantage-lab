# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_car_drills.py
# @brief LAB-49..LAB-52, LAB-61, LAB-62: the camera recording with its big frame, the renumber and counter_jump verbs, the recording edits drop_sample and drop_ref_sync, and the drill scenarios of the running example.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Tests of the drills (``G4``; design sections 5.4, 5.5, 6.2, 6.3 ``F-CAM``): each drill's effect is in the
recording the fake car writes. The recordings are read back through the schemas they embed (R-SIM-11). Where
the lab can judge what a drill should do to a re-run, the reference re-run (``lab_tools.reference``, no Vantage)
does: ``missing_sample`` and ``missing_run`` are refused by it (it has no fallback; Vantage owns that), and the
longest gap of missing counters is compared with ``fallback_max_gap``. ``hang`` and ``crash`` only prove that the
bundle variant carries the key: running them needs the supervisor (``LAB-53``).

Serves ``RCT:SYS-0001`` (renumber), ``RCT:SYS-0016`` (the edits), ``RCT:SYS-0029`` and ``RCT:SYS-0031`` (the
camera). Every check is a plain function that is also run on a planted violation. Skipped, with the reason, when
the build tree is absent or holds libraries of another operating system.
"""
import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest
from mcap.reader import make_reader

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

from lab_tools.abi import LabLibrary, load_index  # noqa: E402
from lab_tools.bundles import bundle_sha256  # noqa: E402
from lab_tools.car import Car, CarError, image_descriptor_set, parse_car_script  # noqa: E402
from lab_tools.messages import MessageFactory  # noqa: E402
from lab_tools.recorder import McapRecorder, RecordingEdits, TopicSpec, apply_recording_edits, use_writer  # noqa: E402
from lab_tools.reference import Recording, ReferenceResim, ReferenceResimError  # noqa: E402
from lab_tools.setup_writer import load_provisional, provisional_values, write_run_setup  # noqa: E402
from lab_tools.topology import load_topology  # noqa: E402
from test_car_running_example import index_path, read_recording, stream, topology  # noqa: E402,F401
from test_catalogue import case_of, index_for  # noqa: E402

CATALOGUE = LAB_ROOT / "catalogue"
PROVISIONAL = CATALOGUE / "provisional.yaml"
LAB_IMAGE_PROTO = LAB_ROOT.parent / "interfaces" / "proto" / "lab" / "lab_image.proto"
COMPONENTS = ["A", "B", "C", "D", "E"]
PORTS = ("out", "refSync", "compState")
BIG_AT_COUNTER, BIG_BYTES = 20, 65536
FRAME_BYTES = 64 * 48


# ---- helpers ------------------------------------------------------------------------------------------------------
def scenario_of(topo, name):
    return next(s for s in topo.scenarios if s.name == name)


def record_scenario(topo, name, index, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"{topo.name}_{name}") / f"{name}.mcap"
    with Car(topo, scenario_of(topo, name), index) as car:
        car.record(out)
    return out


def file_topics(path):
    """The topics of a recording file, from its channels."""
    with open(path, "rb") as f:
        return sorted(c.topic for c in make_reader(f).get_summary().channels.values())


def file_metadata(path):
    with open(path, "rb") as f:
        return {m.name: dict(m.metadata) for m in make_reader(f).iter_metadata()}


def counters(messages, topic):
    return [m.sig_header.measurement_counter for m in stream(messages, topic)]


def named_counters(messages, port):
    """Every ``measurement_counter`` any refSync of any component names for an input called ``port``."""
    return {e.measurement_counter for comp in COMPONENTS for ref in stream(messages, f"{comp}.refSync")
            for e in ref.inputs if e.port == port}


def longest_gap(present):
    """The longest run of consecutive counters missing between the first and the last present one."""
    have, longest, run = set(present), 0, 0
    for c in range(min(present), max(present) + 1):
        run = 0 if c in have else run + 1
        longest = max(longest, run)
    return longest


def fallback_max_gap():
    return provisional_values(load_provisional(PROVISIONAL, "run_setup"))["fallback_max_gap"]


@pytest.fixture(scope="module")
def base_path(index_path, topology, tmp_path_factory):
    return record_scenario(topology, "base", index_path, tmp_path_factory)


@pytest.fixture(scope="module")
def base(base_path):
    return read_recording(base_path)


# ---- LAB-49: the camera ---------------------------------------------------------------------------------------------
def frame_defects(frames):
    """Defects in the camera's frames: a wrong length, a wrong pixel, a wrong header, other than the one big frame."""
    bad = []
    for i, frame in enumerate(frames):
        h = frame.sig_header
        counter = h.measurement_counter
        want = BIG_BYTES if counter == BIG_AT_COUNTER else FRAME_BYTES
        if counter != i + 1 or h.cycle_counter != counter or h.source_timestamp_us != 50_000 * counter:
            bad.append((i, "header"))
        if (frame.width, frame.height) != (64, 48):
            bad.append((i, "size"))
        if len(frame.pixels) != want:
            bad.append((i, "length"))
        elif any(p != (counter + j) % 256 for j, p in enumerate(frame.pixels)):
            bad.append((i, "pixels"))
    return bad


@pytest.fixture(scope="module")
def camera_index():
    return index_for("camera")


@pytest.fixture(scope="module")
def camera_run(camera_index, tmp_path_factory):
    topo = load_topology(CATALOGUE / "camera.yaml")
    path = record_scenario(topo, "base", camera_index, tmp_path_factory)
    return topo, path, read_recording(path)


def test_lab49_camera_records_labimage_frames_with_the_ramp_and_one_big_frame(camera_run):
    """SYS-0029, SYS-0031 serve: 64 x 48 every 50 ms, ``pixels[i] = (counter + i) mod 256``, one frame of 65536 bytes at counter 20."""
    topo, path, messages = camera_run
    frames = stream(messages, "source.Cam")
    assert len(frames) == topo.duration_ms // 50 and type(frames[0]).DESCRIPTOR.full_name == "adas.lab.LabImage"
    assert frame_defects(frames) == []
    assert [len(f.pixels) for f in frames].count(BIG_BYTES) == 1 and max(len(f.pixels) for f in frames) == BIG_BYTES
    assert BIG_BYTES > 16384, "the big frame is larger than the provisional MeasCycle slot"
    assert "source.Cam" in file_topics(path) and "source.X" in file_topics(path)
    assert file_metadata(path)["topic_provenance"]["source.Cam"] == "mocked"


def test_lab49_planted_wrong_frames_are_found(camera_run):
    """FAIL side: a changed pixel, a short frame and a second big frame are each found."""
    _, _, messages = camera_run
    frames = [m.__class__.FromString(m.SerializeToString()) for m in stream(messages, "source.Cam")]
    frames[3].pixels = bytes([frames[3].pixels[0] ^ 1]) + frames[3].pixels[1:]
    frames[5].pixels = frames[5].pixels[:-1]
    frames[7].pixels = frames[7].pixels + bytes(BIG_BYTES - FRAME_BYTES)
    assert frame_defects(frames) == [(3, "pixels"), (5, "length"), (7, "length")]


def test_lab49_camera_cases_are_the_bulk_case_and_the_oversize_case(camera_run):
    topo, _, _ = camera_run
    base = scenario_of(topo, "base")
    assert [c["name"] for c in base.resim] == ["bulk", "oversize"]
    assert case_of(base, "bulk")["bulk"] == ["source.Cam"] and "bulk" not in case_of(base, "oversize")
    assert topo.source("Cam").big_frame == {"at_counter": BIG_AT_COUNTER, "bytes": BIG_BYTES}


def test_lab49_the_image_descriptor_agrees_with_lab_image_proto(camera_index):
    """The car builds ``LabImage``'s descriptor itself (no library has a port of that type); it must list the fields of the .proto."""
    if not LAB_IMAGE_PROTO.is_file():
        pytest.skip(f"{LAB_IMAGE_PROTO} is absent: no sibling interfaces checkout to compare with")
    lib = LabLibrary.load(load_index(camera_index)["components"]["A"]["library"])
    try:
        factory = MessageFactory(image_descriptor_set(lib.manifest().descriptor_set))
    finally:
        lib.shutdown()
    descriptor = factory.message_class("adas.lab.LabImage").DESCRIPTOR
    text = re.sub(r"//[^\n]*", "", LAB_IMAGE_PROTO.read_text(encoding="utf-8"))
    declared = {name: (int(number), kind) for kind, name, number in
                re.findall(r"^\s*([\w.]+)\s+(\w+)\s*=\s*(\d+)\s*;", text, flags=re.M)}
    kinds = {descriptor.fields_by_name["width"].type: "uint32", descriptor.fields_by_name["pixels"].type: "bytes"}
    built = {f.name: (f.number, f.message_type.full_name if f.message_type else kinds[f.type]) for f in descriptor.fields}
    assert built == declared
    assert re.search(r"^package\s+adas\.lab\s*;", text, flags=re.M) and descriptor.file.package == "adas.lab"


# ---- LAB-50: renumber and counter_jump ---------------------------------------------------------------------------------
def identity_breaks(messages, comp):
    """Runs of ``comp`` whose output's ``measurement_counter`` is not its control's, as (run, port)."""
    controls = counters(messages, f"{comp}.control")
    return [(run, port) for port in PORTS for run, (c, o) in enumerate(zip(controls, counters(messages, f"{comp}.{port}")), 1)
            if c != o]


@pytest.fixture(scope="module")
def data_triggered_index():
    return index_for("data_triggered")


def record_drill(topo, scenario, index, path):
    with Car(topo, scenario, index) as car:
        car.record(path)
    return read_recording(path)


def test_lab50_the_renumber_drill_is_in_data_triggered():
    """Section 6.2: ``renumber`` lives in ``data_triggered`` (an input-triggered ``T``), after a counter jump of ``X`` at 10."""
    topo = load_topology(CATALOGUE / "data_triggered.yaml")
    drill = scenario_of(topo, "renumber")
    assert drill.car == {"counter_jump": [{"source": "X", "at_counter": 10, "by": 1000}], "renumber": ["T"]}
    assert drill.expect == "diverges" and [c["name"] for c in drill.resim] == ["by_data"]
    assert case_of(drill, "by_data")["trigger"] == {"T": "in_X"}
    assert "renumber" not in [s.name for s in load_topology(CATALOGUE / "running_example.yaml").scenarios]


def test_lab50_renumber_breaks_the_identity_of_every_run_after_the_jump(data_triggered_index, tmp_path, capsys):
    """SYS-0001 FAIL side made real: after ``X``'s counter jumps, ``T``'s control carries 1010, ... and a renumbering ``T`` writes its own run
    count on every output, so the identity breaks in every run from the jump on and in none before it. The re-run by refSync reports it as
    *equal outputs fail* on ``T`` (first differing run 10); the same run without ``renumber`` keeps the identity and re-runs equal."""
    topo = load_topology(CATALOGUE / "data_triggered.yaml")
    drill = scenario_of(topo, "renumber")
    jump_only = dataclasses.replace(drill, name="jump_only", car={"counter_jump": drill.car["counter_jump"]}, expect="exact")
    plain = record_drill(topo, jump_only, data_triggered_index, tmp_path / "jump_only.mcap")
    messages = record_drill(topo, drill, data_triggered_index, tmp_path / "renumber.mcap")
    assert identity_breaks(plain, "T") == []
    controls = counters(messages, "T.control")
    assert controls[:9] == list(range(1, 10)) and controls[9:12] == [1010, 1011, 1012]
    assert counters(messages, "T.control") == counters(plain, "T.control"), "the jump is the same; only the outputs differ"
    breaks = identity_breaks(messages, "T")
    assert {run for run, _ in breaks} == set(range(10, len(controls) + 1)), "every run after the jump, none before"
    assert {(run, port) for run, port in breaks} == {(r, p) for r in range(10, len(controls) + 1) for p in PORTS}
    assert counters(messages, "T.out") == list(range(1, len(controls) + 1))
    reference = ReferenceResim(topo, tmp_path / "renumber.mcap", data_triggered_index).run()
    assert not reference.equal and reference.components["T"].first_difference_run == 10
    assert not reference.components["T"].ports["out"] and reference.components["T"].mismatched_runs == len(controls) - 9
    assert ReferenceResim(topo, tmp_path / "jump_only.mcap", data_triggered_index).run().equal
    with capsys.disabled():
        print(f"\nrenumber [T]: identity broken in {len(controls) - 9} of {len(controls)} runs (all from run 10); "
              f"the re-run by refSync: equal outputs fail, {reference.components['T'].mismatched_runs} runs differ")


def test_lab50_counter_jump_is_in_the_event_log(index_path, topology, tmp_path_factory, base, capsys):
    """The counter of ``X`` goes 49 -> 1050 and on; times, other sources and the number of samples are unchanged; the refSyncs name the jumped counters."""
    path = record_scenario(topology, "counter_jump", index_path, tmp_path_factory)
    jumped = read_recording(path)
    was, now = counters(base, "source.X"), counters(jumped, "source.X")
    assert len(now) == len(was) and now[:49] == was[:49] and now[48:51] == [49, 1050, 1051]
    assert now[49:] == [c + 1000 for c in was[49:]]
    assert [m.sig_header.cycle_counter for m in stream(jumped, "source.X")] == now
    assert [m.sig_header.source_timestamp_us for m in stream(jumped, "source.X")] == \
        [m.sig_header.source_timestamp_us for m in stream(base, "source.X")]
    assert counters(jumped, "source.Y") == counters(base, "source.Y") and counters(jumped, "source.Z") == counters(base, "source.Z")
    assert max(named_counters(jumped, "in_X")) > 1000 and 50 not in named_counters(jumped, "in_X")
    result = ReferenceResim(topology, path, index_path).run()
    assert result.equal, "\n".join(result.describe())
    with capsys.disabled():
        print(f"\ncounter_jump: X goes {now[48]} -> {now[49]}; re-run by refSync equal outputs")


def test_lab50_car_script_refuses_what_it_cannot_do(topology):
    """A verb outside the design's list, a name that is not in the topology and a number out of range are each refused, naming the key."""
    base = scenario_of(topology, "base")

    def refused(match, **blocks):
        with pytest.raises(CarError, match=match):
            parse_car_script(topology, dataclasses.replace(base, **blocks))

    refused("car verb\\(s\\) teleport", car={"teleport": []})
    refused("car.renumber names 'Q'", car={"renumber": ["Q"]})
    refused("car.counter_jump names 'Q'", car={"counter_jump": [{"source": "Q", "at_counter": 5, "by": 1}]})
    refused("car.counter_jump.by must be an integer of at least 1", car={"counter_jump": [{"source": "X", "at_counter": 5, "by": 0}]})
    refused("must have exactly source, at_counter, by", car={"counter_jump": [{"source": "X", "at_counter": 5}]})


# ---- LAB-51: the recording edits -------------------------------------------------------------------------------------------
def test_lab51_apply_recording_edits_drops_only_what_the_edit_names():
    edits = RecordingEdits(frozenset({("source.Y", 20)}), drop_ref_sync=False)
    assert not apply_recording_edits(edits, "source.Y", 20)
    assert apply_recording_edits(edits, "source.Y", 21) and apply_recording_edits(edits, "source.X", 20)
    assert apply_recording_edits(edits, "B.out", 20) and apply_recording_edits(edits, "B.refSync", None)
    no_sync = RecordingEdits(drop_ref_sync=True)
    assert not apply_recording_edits(no_sync, "B.refSync") and not apply_recording_edits(no_sync, "A.refSync", 3)
    assert all(apply_recording_edits(no_sync, t) for t in ("B.out", "B.control", "B.compState", "source.refSync"))
    assert not RecordingEdits() and edits and no_sync


def test_lab51_the_recorder_leaves_edited_messages_and_topics_out(tmp_path, index_path):
    """The file has no ``refSync`` channel and no message of the dropped sample; the metadata states the edit's topics only."""
    use_writer(load_index(index_path)["interfaces_python"])
    topics = {"source.Y": TopicSpec("x.Y", b"d", "mocked"), "A.refSync": TopicSpec("x.R", b"d", "simulated"),
              "A.out": TopicSpec("x.O", b"d", "simulated")}
    path = tmp_path / "edited.mcap"
    manifest = {"scenario": "t", "source_kind": "synthetic", "run_kind": "sim", "components": []}  # the writer has no defaults
    with McapRecorder(path, topics, manifest, RecordingEdits(frozenset({("source.Y", 2)}), True)) as recorder:
        for counter in (1, 2, 3):
            recorder.write("source.Y", counter * 100, bytes([counter]), counter)
            recorder.write("A.refSync", counter * 100, b"r")  # the car still writes it; the recorder leaves it out
            recorder.write("A.out", counter * 100, b"o")
        counts = recorder.counts
    assert counts == {"A.out": 3, "source.Y": 2}
    assert file_topics(path) == ["A.out", "source.Y"] and "A.refSync" not in file_metadata(path)["topic_provenance"]
    with open(path, "rb") as f:
        assert [m.data for _, _, m in make_reader(f).iter_messages(topics=["source.Y"])] == [bytes([1]), bytes([3])]
    with pytest.raises(Exception, match="was not declared"):
        with McapRecorder(tmp_path / "x.mcap", topics, manifest) as recorder:
            recorder.write("nothing", 1, b"")


def test_lab51_recording_keys_are_checked(topology):
    base = scenario_of(topology, "base")

    def refused(match, recording):
        with pytest.raises(CarError, match=match):
            parse_car_script(topology, dataclasses.replace(base, recording=recording))

    refused("recording key\\(s\\) drop_run", {"drop_run": []})
    refused("must be source.<name>", {"drop_sample": [{"port": "A.out", "counter": 3}]})
    refused("must be source.<name>", {"drop_sample": [{"port": "source.Q", "counter": 3}]})
    refused("exactly port, counter", {"drop_sample": [{"port": "source.Y"}]})
    refused("counter must be an integer of at least 1", {"drop_sample": [{"port": "source.Y", "counter": 0}]})
    refused("drop_ref_sync must be 'all'", {"drop_ref_sync": "some"})
    script = parse_car_script(topology, dataclasses.replace(base, recording={
        "drop_sample": [{"port": "source.Y", "counter": 7}], "drop_ref_sync": "all"}))
    assert script.edits == RecordingEdits(frozenset({("source.Y", 7)}), True)


# ---- LAB-52: the drill scenarios -----------------------------------------------------------------------------------------------
def test_lab52_the_running_example_has_the_drills_of_section_6_2(topology):
    assert [s.name for s in topology.scenarios] == ["base", "restart", "fault", "missing_sample", "missing_run",
                                                    "no_ref_sync", "counter_jump", "counter_wrap", "renumber_lib"]
    assert [c["name"] for c in scenario_of(topology, "base").resim] == ["cut_c", "all_simulated", "jump", "hang", "crash"]
    assert {s.name: s.expect for s in topology.scenarios if s.expect != "exact"} == {"no_ref_sync": "diverges",
                                                                                      "renumber_lib": "diverges"}
    dropped = lambda name: sorted(c for _, c in parse_car_script(topology, scenario_of(topology, name)).edits.drop_samples)
    assert dropped("missing_sample") == [20, 21, 22] and dropped("missing_run") == list(range(30, 40))
    assert all(s.resim == ({"name": "all_simulated"},) for s in topology.scenarios[3:-1])
    assert len(dropped("missing_sample")) <= fallback_max_gap() < len(dropped("missing_run")), "both sides of the limit exist"


@pytest.mark.parametrize("name, missing", [("missing_sample", [20, 21, 22]), ("missing_run", list(range(30, 40)))])
def test_lab52_a_dropped_sample_is_absent_though_a_refsync_names_it(index_path, topology, tmp_path_factory, base_path, base,
                                                                      capsys, name, missing):
    """SYS-0016 serves: the samples are not in ``source.Y``, every other topic is byte-for-byte the base recording's, a refSync names
    the dropped counters, and the strict re-run refuses (the fallback is Vantage's). The gap is compared with ``fallback_max_gap``."""
    path = record_scenario(topology, name, index_path, tmp_path_factory)
    edited = read_recording(path)
    was = counters(base, "source.Y")
    assert counters(edited, "source.Y") == [c for c in was if c not in missing]
    assert file_topics(path) == file_topics(base_path), "the topics are the same; only the sample is gone"
    before, after = Recording.read(base_path), Recording.read(path)
    for topic in file_topics(base_path):
        if topic != "source.Y":
            assert [(m.log_time_us, m.payload) for m in after.topic(topic)] == \
                [(m.log_time_us, m.payload) for m in before.topic(topic)], topic
    named = named_counters(edited, "in_Y")
    still_named = sorted(set(missing) & named)
    assert still_named, "a refSync must still name a dropped sample, or the drill tests nothing"
    gap = longest_gap(counters(edited, "source.Y"))
    assert gap == len(missing)
    assert file_metadata(path)["manifest"]["yaml"].count("drop_sample") == 1
    with pytest.raises(ReferenceResimError, match=rf"refSync names counter ({'|'.join(map(str, still_named))}) of input in_Y, "
                                                   r"which source\.Y does not hold"):
        ReferenceResim(topology, path, index_path).run()
    limit = fallback_max_gap()
    with capsys.disabled():
        print(f"\n{name}: {len(missing)} dropped, {len(still_named)} still named by a refSync {still_named}; "
              f"longest gap {gap} {'<=' if gap <= limit else '>'} fallback_max_gap {limit}")
    assert (gap <= limit) == (name == "missing_sample")


def test_lab52_no_ref_sync_recording_has_no_refsync_topic(index_path, topology, tmp_path_factory, base_path, base, capsys):
    """SYS-0016 serves: no ``refSync`` topic in the file, no entry for it in the provenance, every other topic equal to the base recording's.
    By refSync the reference re-run refuses such a file; with ``use_ref_sync=False`` it counts the runs the time rule gets wrong (above 0)."""
    path = record_scenario(topology, "no_ref_sync", index_path, tmp_path_factory)
    wanted = [t for t in file_topics(base_path) if not t.endswith(".refSync")]
    assert file_topics(path) == wanted and len(wanted) == len(file_topics(base_path)) - len(COMPONENTS)
    assert not any(t.endswith(".refSync") for t in file_metadata(path)["topic_provenance"])
    assert "drop_ref_sync: all" in file_metadata(path)["manifest"]["yaml"]
    before, after = Recording.read(base_path), Recording.read(path)
    for topic in wanted:
        assert [(m.log_time_us, m.payload) for m in after.topic(topic)] == \
            [(m.log_time_us, m.payload) for m in before.topic(topic)], topic
    assert all(len(stream(read_recording(path), f"{c}.control")) > 0 for c in COMPONENTS)
    assert scenario_of(topology, "no_ref_sync").expect == "diverges"
    resim = ReferenceResim(topology, path, index_path)
    with pytest.raises(ReferenceResimError, match="recorded controls but 0 on refSync"):
        resim.run()  # by refSync the recording cannot be re-run
    counted = resim.run(use_ref_sync=False)  # the time rule: the runs that differ are counted
    assert counted.mismatched_runs > 0 and not counted.equal, "expect: diverges -- the time rule must miss some runs"
    assert all("refSync" not in c.ports for c in counted.components.values()), "the dropped topic is not compared"
    assert set(counted.components) == set(COMPONENTS)
    with capsys.disabled():
        print(f"\nno_ref_sync: {len(wanted)} topics, none a refSync; the {sum(len(before.topic(f'{c}.refSync')) for c in COMPONENTS)} "
              f"refSync messages of the base recording are absent; time rule: {counted.mismatched_runs} differing runs "
              f"{ {n: c.mismatched_runs for n, c in counted.components.items()} }")


@pytest.mark.parametrize("case, key", [("hang", "LAB_B_HANG_AFTER_RUNS"), ("crash", "LAB_B_CRASH_AFTER_RUNS")])
def test_lab52_hang_and_crash_cases_carry_the_key_in_a_bundle_variant(index_path, topology, tmp_path, base_path, case, key):
    """The run setup of the case points ``B`` at a repacked bundle whose ``default.yaml`` has the key at 5; the library's own bundle keeps 0.
    Running the case needs the supervisor (LAB-53), so nothing is run here."""
    scenario = scenario_of(topology, "base")
    index = load_index(index_path)
    values = provisional_values(load_provisional(PROVISIONAL, "run_setup"))
    path = write_run_setup(topology, scenario, case_of(scenario, case), recording=base_path, index=index, out_dir=tmp_path,
                           run_setup_values=values)
    plain = write_run_setup(topology, scenario, {"name": "plain"}, recording=base_path, index=index,
                            out_dir=tmp_path / "plain", run_setup_values=values)
    stages = {s["name"]: s for s in json.loads(path.read_text(encoding="utf-8"))["stages"]}
    ordinary = {s["name"]: s for s in json.loads(plain.read_text(encoding="utf-8"))["stages"]}
    folder = tmp_path / f"base.{case}.bundles" / "B"
    assert f"{key}: 5" in (folder / "default.yaml").read_text(encoding="utf-8").splitlines()
    assert (folder / "bundle.sha256").read_text(encoding="utf-8").strip() == bundle_sha256(folder) == stages["B"]["config_bundle_sha256"]
    assert stages["B"]["config_bundle_sha256"] != ordinary["B"]["config_bundle_sha256"]
    assert all(stages[n]["config_bundle_sha256"] == ordinary[n]["config_bundle_sha256"] for n in "ACDE")
    own = Path(index["components"]["B"]["config"]) / "default.yaml"
    assert f"{key}: 0" in own.read_text(encoding="utf-8").splitlines(), "the bundle beside the library is never written"


# ---- LAB-61: renumber_lib / shifted -----------------------------------------------------------------------------------------
def test_lab61_renumber_lib_is_a_case_not_a_car_verb(topology, tmp_path):
    """Serves SYS-0013: ``A``'s own code shifts, so the scenario has no ``car:`` block and the case sets ``LAB_A_RENUMBER`` in A's bundle."""
    drill = scenario_of(topology, "renumber_lib")
    assert drill.expect == "diverges" and drill.car == {} and drill.recording == {}
    assert [c["name"] for c in drill.resim] == ["shifted"]
    assert case_of(drill, "shifted")["config"] == {"A": {"set": {"LAB_A_RENUMBER": 1}}}
    assert parse_car_script(topology, drill).edits == RecordingEdits() and not parse_car_script(topology, drill).renumber


def test_lab61_the_run_setup_of_shifted_points_at_a_bundle_with_the_key(index_path, topology, tmp_path_factory, base_path):
    drill = scenario_of(topology, "renumber_lib")
    out = tmp_path_factory.mktemp("renumber_lib")
    path = write_run_setup(topology, drill, case_of(drill, "shifted"), recording=base_path, index=load_index(index_path),
                           out_dir=out, run_setup_values=provisional_values(load_provisional(PROVISIONAL, "run_setup")))
    stages = {s["name"]: s for s in json.loads(path.read_text(encoding="utf-8"))["stages"]}
    folder = out / "renumber_lib.shifted.bundles" / "A"
    assert "LAB_A_RENUMBER: 1" in (folder / "default.yaml").read_text(encoding="utf-8").splitlines()
    assert stages["A"]["config_bundle_sha256"] == bundle_sha256(folder)
    built = Path(load_index(index_path)["components"]["B"]["config"])
    assert stages["B"]["config_bundle_sha256"] == bundle_sha256(built), "the other stages keep the built bundle"


# ---- LAB-62: the recording verb repeat_sample --------------------------------------------------------------------------------
def test_lab62_apply_recording_edits_repeats_only_what_the_edit_names():
    edits = RecordingEdits(frozenset({("source.Y", 20)}), False, frozenset({("source.X", 10)}))
    assert apply_recording_edits(edits, "source.X", 10) == 2
    assert apply_recording_edits(edits, "source.X", 11) == 1 and apply_recording_edits(edits, "source.Y", 10) == 1
    assert apply_recording_edits(edits, "A.out", 10) == 1 and apply_recording_edits(edits, "source.X") == 1
    assert apply_recording_edits(edits, "source.Y", 20) == 0
    assert edits.as_manifest() == {"drop_sample": [{"port": "source.Y", "counter": 20}],
                                   "repeat_sample": [{"port": "source.X", "counter": 10}]}
    assert RecordingEdits(repeat_samples=frozenset({("source.X", 1)})) and not RecordingEdits()


def test_lab62_the_recorder_writes_the_repeat_byte_identical_one_ms_later_and_in_time_order(tmp_path, index_path):
    use_writer(load_index(index_path)["interfaces_python"])
    topics = {"source.X": TopicSpec("x.X", b"d", "mocked"), "A.out": TopicSpec("x.O", b"d", "simulated")}
    manifest = {"scenario": "t", "source_kind": "synthetic", "run_kind": "sim", "components": []}
    path = tmp_path / "repeat.mcap"
    with McapRecorder(path, topics, manifest, RecordingEdits(repeat_samples=frozenset({("source.X", 2)}))) as recorder:
        recorder.write("source.X", 1_000, b"one", 1)
        recorder.write("source.X", 2_000, b"two", 2)
        recorder.write("A.out", 2_500, b"mid")             # between the original and its repeat
        recorder.write("A.out", 3_000, b"late")            # after the repeat is due
        recorder.write("source.X", 4_000, b"three", 3)
        counts = recorder.counts
        assert recorder.repeated == {("source.X", 2)}
    assert counts == {"source.X": 4, "A.out": 2}
    with open(path, "rb") as f:
        written = [(c.topic, m.log_time // 1000, m.data) for _, c, m in make_reader(f).iter_messages()]
    assert written == [("source.X", 1000, b"one"), ("source.X", 2000, b"two"), ("A.out", 2500, b"mid"),
                       ("source.X", 3000, b"two"), ("A.out", 3000, b"late"), ("source.X", 4000, b"three")]


def test_lab62_a_repeat_with_nothing_after_it_is_still_written_at_close(tmp_path, index_path):
    use_writer(load_index(index_path)["interfaces_python"])
    topics = {"source.X": TopicSpec("x.X", b"d", "mocked")}
    manifest = {"scenario": "t", "source_kind": "synthetic", "run_kind": "sim", "components": []}
    with McapRecorder(tmp_path / "last.mcap", topics, manifest, RecordingEdits(repeat_samples=frozenset({("source.X", 1)}))) as recorder:
        recorder.write("source.X", 500, b"only", 1)
    with open(tmp_path / "last.mcap", "rb") as f:
        assert [(m.log_time // 1000, m.data) for _, _, m in make_reader(f).iter_messages()] == [(500, b"only"), (1500, b"only")]


def test_lab62_repeat_sample_is_checked(topology, index_path, tmp_path):
    base = scenario_of(topology, "base")

    def refused(match, recording):
        with pytest.raises(CarError, match=match):
            parse_car_script(topology, dataclasses.replace(base, recording=recording))

    refused("must be source.<name>", {"repeat_sample": [{"port": "A.out", "counter": 3}]})
    refused("must be source.<name>", {"repeat_sample": [{"port": "source.Q", "counter": 3}]})
    refused("exactly port, counter", {"repeat_sample": [{"port": "source.Y"}]})
    refused("repeat_sample.counter must be an integer of at least 1", {"repeat_sample": [{"port": "source.Y", "counter": 0}]})
    refused("drop_sample leaves out", {"drop_sample": [{"port": "source.Y", "counter": 7}],
                                       "repeat_sample": [{"port": "source.Y", "counter": 7}]})
    script = parse_car_script(topology, dataclasses.replace(base, recording={"repeat_sample": [{"port": "source.Y", "counter": 7}]}))
    assert script.edits == RecordingEdits(repeat_samples=frozenset({("source.Y", 7)}))
    never = dataclasses.replace(base, recording={"repeat_sample": [{"port": "source.Y", "counter": 99999}]})
    with Car(topology, never, index_path) as car, pytest.raises(CarError, match="never wrote"):
        car.record(tmp_path / "never.mcap")
