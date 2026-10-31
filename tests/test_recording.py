# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_recording.py
# @brief LAB-56: SYT-REC-01, SYT-REC-02, SYT-REC-03 and ANL-06 on the fake car's recording, written through the interfaces writer.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Tests of the lab's recorder (``LAB-55``, ``LAB-56``): the recording of ``running_example`` / ``base`` follows
``RCT_Architecture.md`` section 6a. Each ``SYT`` case ``.cN`` is one test named ``test_SYT_REC_<nn>_c<N>_...``; the
test cites the requirement whose ``Accept`` decides it by uid only (R-REQ-4) and its own id verbatim (R-REQ-9).
Steps and PASS/FAIL sides: ``RCT_Test_Spec.md`` section 6a. The conformance check and the one-writer check are
the scripts of the ``interfaces`` package, found beside the ``python/`` folder that ``index.json`` names.
Every check is a plain function that is also run on a planted violation, so each case is seen to fail.
Skipped, with the reason, when the build tree is absent or holds libraries of another operating system.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from mcap.reader import make_reader

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

from lab_tools.abi import load_index  # noqa: E402
from lab_tools.car import Car  # noqa: E402
from lab_tools.recorder import McapRecorder, TopicSpec, apply_recording_edits  # noqa: E402
from test_car_running_example import index_path, read_recording, topology  # noqa: E402,F401

ORIGINS = ("measured", "truth", "mocked", "simulated", "overridden")
CREATED_UTC = "2026-10-31T12:00:00Z"


# ---- helpers ------------------------------------------------------------------------------------------------------
class LoggedRecorder(McapRecorder):
    """The lab's recorder that also keeps the order in which the car handed it messages (the file holds none)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handed: list[tuple[str, int, bytes]] = []

    def write(self, topic, log_time_us, payload, counter=None):
        if apply_recording_edits(self._edits, topic, counter):
            self.handed.append((topic, log_time_us, bytes(payload)))
        super().write(topic, log_time_us, payload, counter)


def scripts_dir(index_path):
    """The ``scripts/`` folder of the package whose ``python/`` folder ``index.json`` names."""
    return Path(load_index(index_path)["interfaces_python"]).parent / "scripts"


def run_script(index_path, name, *args):
    done = subprocess.run([sys.executable, str(scripts_dir(index_path) / name), *map(str, args)],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def record_base(topology, index_path, out):
    """Run ``base`` once into ``out``; return the messages the car handed over, in order."""
    scenario = next(s for s in topology.scenarios if s.name == "base")
    with Car(topology, scenario, index_path) as car:
        with LoggedRecorder(out, car.topics(), car.recording_manifest(), car.script.edits) as recorder:
            car.run(recorder)
    return recorder.handed


def file_messages(path):
    """``(topic, log_time_us, publish_time_us, bytes)`` in file order."""
    with open(path, "rb") as f:
        return [(ch.topic, m.log_time // 1000, m.publish_time // 1000, m.data)
                for _, ch, m in make_reader(f).iter_messages(log_time_order=False)]


def file_metadata(path):
    with open(path, "rb") as f:
        return {m.name: dict(m.metadata) for m in make_reader(f).iter_metadata()}


def manifest_of(path):
    return yaml.safe_load(file_metadata(path)["manifest"]["yaml"])


@pytest.fixture(scope="module")
def lab_run(index_path, topology, tmp_path_factory):
    out = tmp_path_factory.mktemp("recording") / "base.mcap"
    return out, record_base(topology, index_path, out)


# ---- checks (each also run on a planted violation) -----------------------------------------------------------------
def topic_defects(names, policies):
    """Topics as ``SYS-0037``'s Accept: one per listed non-excluded port, none other, none twice."""
    bad = [f"{n}: registered twice" for n in sorted({n for n in names if names.count(n) > 1})]
    bad += [f"{p}: no topic" for p, pol in sorted(policies.items()) if pol != "excluded" and p not in names]
    bad += [f"{n}: no listed port" for n in sorted(set(names) - set(policies))]
    return bad


def order_defects(file_order, handed):
    """Order and times as ``SYS-0039``'s Accept: log time never back, publish time equal, equal times in hand-over order."""
    bad = []
    for i, (topic, log_us, publish_us, _) in enumerate(file_order):
        if i and log_us < file_order[i - 1][1]:
            bad.append(f"{topic}: log time {log_us} after {file_order[i - 1][1]}")
        if publish_us != log_us:
            bad.append(f"{topic}: publish time {publish_us} differs from log time {log_us}")
    if [(t, us, d) for t, us, _, d in file_order] != handed:
        bad.append("file order differs from the order the car handed messages over")
    return bad


def provenance_defects(manifest, origins):
    bad = [f"manifest {k} missing" for k in ("source_kind", "run_kind") if not manifest.get(k)]
    bad += [f"{t}: origin {o!r}" for t, o in sorted(origins.items()) if o not in ORIGINS]
    return bad


def plant_port_without_topic(source, target):
    """Rewrite ``source`` through the Python writer with one more port listed ``full`` and no topic for it."""
    from adas_recording.writer import McapRecorder as Writer, TopicSpec as WriterTopic
    meta = file_metadata(source)
    manifest = yaml.safe_load(meta["manifest"]["yaml"])
    manifest["record_policies"]["Q.out"] = "full"
    topics = {}
    with open(source, "rb") as f:
        summary = make_reader(f).get_summary()
        for channel in summary.channels.values():
            schema = summary.schemas[channel.schema_id]
            topics[channel.topic] = WriterTopic(schema.name, schema.data, meta["topic_provenance"][channel.topic])
    with Writer(target, topics, manifest, library="vantage-lab") as writer:
        for topic, log_us, _, data in file_messages(source):
            writer.write(topic, log_us, data)


# ---- SYT-REC-01 ------------------------------------------------------------------------------------------------------
def test_SYT_REC_01_c1_topics_follow_the_record_policies(lab_run, index_path):
    """SYT-REC-01.c1 (SYS-0037): the check passes; the topics are the non-excluded ports, once each."""
    path, _ = lab_run
    code, output = run_script(index_path, "check_recording.py", path)
    assert code == 0, output
    with open(path, "rb") as f:
        names = [c.topic for c in make_reader(f).get_summary().channels.values()]
    policies = manifest_of(path)["record_policies"]
    assert topic_defects(names, policies) == [] and set(policies.values()) == {"full"}
    assert len(names) == 23  # 3 sources + 5 components x (control, out, refSync, compState)
    assert topic_defects(names + ["A.out"], policies) == ["A.out: registered twice"]  # planted
    assert topic_defects([n for n in names if n != "B.out"], policies) == ["B.out: no topic"]  # planted
    assert topic_defects(names + ["Q.out"], policies) == ["Q.out: no listed port"]  # planted


def test_SYT_REC_01_c2_every_message_decodes_through_its_embedded_schema(lab_run):
    """SYT-REC-01.c2 (SYS-0038)."""
    path, handed = lab_run
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
    assert all(summary.schemas[c.schema_id].encoding == "protobuf" and c.message_encoding == "protobuf"
               for c in summary.channels.values())
    assert len(read_recording(path)) == len(handed)  # read_recording raises on a message its schema does not decode
    with pytest.raises(Exception):  # planted: a payload that is no message
        from lab_tools.messages import MessageFactory
        with open(path, "rb") as f:
            schema = next(iter(make_reader(f).get_summary().schemas.values()))
        MessageFactory(schema.data).parse(schema.name, b"\xff\xff\xff")


def test_SYT_REC_01_c3_order_and_times_follow_the_car(lab_run):
    """SYT-REC-01.c3 (SYS-0039): equal-`log_time` messages are in the order the car handed them over."""
    path, handed = lab_run
    in_file = file_messages(path)
    assert order_defects(in_file, handed) == []
    assert len({us for _, us, _ in handed}) < len(handed)  # equal times exist, so the order check has something to say
    equal = next(i for i in range(1, len(in_file)) if in_file[i][1] == in_file[i - 1][1])
    swapped = list(in_file)
    swapped[equal - 1], swapped[equal] = swapped[equal], swapped[equal - 1]  # planted: equal times out of the car's order
    assert order_defects(swapped, handed) == ["file order differs from the order the car handed messages over"]
    later = next(i for i in range(1, len(in_file)) if in_file[i][1] > in_file[i - 1][1])
    back = list(in_file)
    back[later - 1], back[later] = back[later], back[later - 1]  # planted: a log time going back
    assert any("after" in line for line in order_defects(back, handed))
    assert any("publish time" in line for line in order_defects([(in_file[0][0], 5, 6, b"")], [(in_file[0][0], 5, b"")]))


def test_SYT_REC_01_c4_provenance_is_stated_for_the_file_and_every_topic(lab_run):
    """SYT-REC-01.c4 (SYS-0041): `source_kind` synthetic, `run_kind` sim, sources `mocked`, component ports `simulated`."""
    path, _ = lab_run
    manifest, origins = manifest_of(path), file_metadata(path)["topic_provenance"]
    assert provenance_defects(manifest, origins) == []
    assert (manifest["source_kind"], manifest["run_kind"], manifest["manifest_version"]) == ("synthetic", "sim", 1)
    assert {o for t, o in origins.items() if t.startswith("source.")} == {"mocked"}
    assert {o for t, o in origins.items() if not t.startswith("source.")} == {"simulated"}
    assert provenance_defects({}, {"A.out": "estimated"}) == [
        "manifest source_kind missing", "manifest run_kind missing", "A.out: origin 'estimated'"]  # planted


def test_SYT_REC_01_c5_a_full_port_with_no_topic_fails_the_check(lab_run, index_path, tmp_path):
    """SYT-REC-01.c5 (SYS-0037): the planted copy fails, naming the port with no topic."""
    planted = tmp_path / "planted.mcap"
    plant_port_without_topic(lab_run[0], planted)
    code, output = run_script(index_path, "check_recording.py", planted)
    assert code == 1 and "'Q.out'" in output and "no topic" in output, output


# ---- SYT-REC-02 ------------------------------------------------------------------------------------------------------
def refused_write(topology, index_path, tmp_path, match, change):
    """Call the lab's recorder with the car's own arguments after ``change``; it must raise and leave no file."""
    from adas_recording.writer import RecordingError
    scenario = next(s for s in topology.scenarios if s.name == "base")
    path = tmp_path / "refused.mcap"
    with Car(topology, scenario, index_path) as car:
        topics, manifest = car.topics(), car.recording_manifest()
        topics, manifest = change(topics, manifest)
        with pytest.raises(RecordingError, match=match):
            McapRecorder(path, topics, manifest, car.script.edits)
    assert not path.exists()


def test_SYT_REC_02_c1_a_missing_source_kind_or_run_kind_is_refused(topology, index_path, tmp_path):
    """SYT-REC-02.c1 (SYS-0041)."""
    for key in ("source_kind", "run_kind"):
        refused_write(topology, index_path, tmp_path, f"'{key}'",
                      lambda topics, manifest, key=key: (topics, {k: v for k, v in manifest.items() if k != key}))


def test_SYT_REC_02_c2_a_topic_with_no_origin_is_refused(topology, index_path, tmp_path):
    """SYT-REC-02.c2 (SYS-0041): the error names the topic, and no origin is filled in."""
    refused_write(topology, index_path, tmp_path, "'A.out'",
                  lambda topics, manifest: ({**topics, "A.out": TopicSpec(topics["A.out"].schema_name,
                                                                          topics["A.out"].descriptor_set, None)}, manifest))


def test_SYT_REC_02_c3_an_unknown_origin_is_refused(topology, index_path, tmp_path):
    """SYT-REC-02.c3 (SYS-0041): the error names the topic and the value."""
    refused_write(topology, index_path, tmp_path, "'A.out'.*'estimated'",
                  lambda topics, manifest: ({**topics, "A.out": TopicSpec(topics["A.out"].schema_name,
                                                                          topics["A.out"].descriptor_set, "estimated")}, manifest))


# ---- SYT-REC-03 ------------------------------------------------------------------------------------------------------
def test_SYT_REC_03_c1_the_same_run_writes_the_same_bytes(lab_run, topology, index_path, tmp_path):
    """SYT-REC-03.c1 (SYS-0040): two writes of one topology, scenario and seed are byte-identical."""
    again = tmp_path / "again.mcap"
    record_base(topology, index_path, again)
    assert again.read_bytes() == lab_run[0].read_bytes()
    other = tmp_path / "other.mcap"  # planted: one byte of the manifest differs
    other.write_bytes(lab_run[0].read_bytes().replace(b"run_kind: sim", b"run_kind: SIM", 1))
    assert other.read_bytes() != lab_run[0].read_bytes()


def test_SYT_REC_03_c2_a_logging_time_is_in_the_manifest_only_when_passed(lab_run, topology, index_path, tmp_path):
    """SYT-REC-03.c2 (SYS-0040): none in the first two files, exactly the given value in the third."""
    again = tmp_path / "again.mcap"
    record_base(topology, index_path, again)
    assert "created_utc" not in manifest_of(lab_run[0]) and "created_utc" not in manifest_of(again)
    scenario = next(s for s in topology.scenarios if s.name == "base")
    third = tmp_path / "third.mcap"
    with Car(topology, scenario, index_path) as car:
        with McapRecorder(third, car.topics(), {**car.recording_manifest(), "created_utc": CREATED_UTC}) as recorder:
            car.run(recorder)
    assert manifest_of(third)["created_utc"] == CREATED_UTC
    assert third.read_bytes() != lab_run[0].read_bytes()


# ---- ANL-06 ----------------------------------------------------------------------------------------------------------
def test_ANL_06_c1_no_file_of_the_lab_imports_the_mcap_writer(index_path):
    """ANL-06.c1 (SYS-0043): the one-writer check over the lab's `src/` finds nothing."""
    code, output = run_script(index_path, "check_one_recording_writer.py", "--root", LAB_ROOT / "src")
    assert code == 0, output


def test_ANL_06_c3_a_second_writer_is_found_and_named(index_path, tmp_path):
    """ANL-06.c3 (SYS-0043): in a copy of `src/` (never in `src/` itself) a planted second writer fails the check."""
    root = tmp_path / "src"
    shutil.copytree(LAB_ROOT / "src", root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    planted = root / "lab_tools" / "planted_writer.py"
    planted.write_text("from mcap.writer import Writer\n", encoding="utf-8")
    code, output = run_script(index_path, "check_one_recording_writer.py", "--root", root)
    assert code == 1 and "planted_writer.py" in output, output
    assert "recorder.py" not in output  # the lab's own recorder is not the finding
