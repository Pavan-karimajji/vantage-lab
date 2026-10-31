# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/unit/test_generate.py
# @brief LAB-15, LAB-16, LAB-54: the generator, the calibration file and the template's hygiene.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from lab_tools.generate import (GenerateError, generate_topology, input_port_names, load_provisional,
                                manifest_input_names, ref_sync_entries, write_default_yaml)
from lab_tools.topology import load_topology

LAB_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = LAB_ROOT / "catalogue"
TEMPLATE = LAB_ROOT / "template"
DECLARED_B = ["in_A", "in_X", "in_Y", "in_Z"]


@pytest.fixture(scope="module")
def topology():
    return load_topology(CATALOGUE / "running_example.yaml")


@pytest.fixture(scope="module")
def provisional():
    return load_provisional(CATALOGUE / "provisional.yaml")


def generate(topology, provisional, out: Path, lab_dir: Path):
    return generate_topology(topology, out, provisional, lab_dir=lab_dir, library_suffix=".dll")


def test_lab15_declared_input_order_is_manifest_order_and_ref_sync_builder_order(topology, provisional, tmp_path):
    generate(topology, provisional, tmp_path / "gen", tmp_path / "lab")
    comp_dir = tmp_path / "gen" / "running_example" / "B"
    b = topology.component("B")

    # the derived lists agree with the description
    assert input_port_names(b) == DECLARED_B
    assert manifest_input_names(b) == ["control"] + DECLARED_B
    assert ref_sync_entries(b) == DECLARED_B

    # manifest order: the ports file the standard shim generator reads
    ports = yaml.safe_load((comp_dir / "ports.yaml").read_text(encoding="utf-8"))
    assert [p["name"] for p in ports["inputs"]] == ["control"] + DECLARED_B
    assert [p["name"] for p in ports["outputs"]] == ["out", "refSync", "compState"]

    # the ports struct declares the members in the same order
    header = (comp_dir / "src" / "component" / "B_ports.hpp").read_text(encoding="utf-8")
    members = re.findall(r"ReqPort<adas::lab::LabSample> (\w+);", header)
    assert members == DECLARED_B

    # RefSyncBuilder order: the names the component builds it from, control not among them
    params = (comp_dir / "lab_params.hpp").read_text(encoding="utf-8")
    names = re.search(r"kInputNames = \{\{(.*?)\}\}", params).group(1)
    assert re.findall(r'"(\w+)"', names) == DECLARED_B
    component = (comp_dir / "lab_component.hpp").read_text(encoding="utf-8")
    assert "refSync_{inputNames()}" in component and "for (const char* name : kInputNames)" in component


def test_lab15_generates_the_files_of_every_component(topology, provisional, tmp_path):
    entries = generate(topology, provisional, tmp_path / "gen", tmp_path / "lab")
    assert list(entries) == ["A", "B", "C", "D", "E"]
    for name in entries:
        comp_dir = tmp_path / "gen" / "running_example" / name
        for rel in (f"src/component/{name}_ports.hpp", "ports.yaml", "lab_params.hpp", "lab_component.hpp",
                    "lab_sil.cpp", "default.yaml"):
            assert (comp_dir / rel).is_file(), rel
        assert f"lab_running_example_{name}" in (comp_dir / "lab_sil.cpp").read_text(encoding="utf-8")


def test_lab16_default_yaml_has_the_provisional_keys_and_the_after_runs_and_start_count_at_zero(topology, provisional, tmp_path):
    generate(topology, provisional, tmp_path / "gen", tmp_path / "lab")
    text = (tmp_path / "gen" / "running_example" / "B" / "default.yaml").read_text(encoding="utf-8")
    keys = yaml.safe_load(text)
    assert keys == {"LAB_B_MAX_AGE_US": 118000, "LAB_B_RECOVERY_RUNS": 2,
                    "LAB_B_HANG_AFTER_RUNS": 0, "LAB_B_CRASH_AFTER_RUNS": 0, "LAB_B_START_COUNT": 0}


@pytest.mark.parametrize("missing", ["LAB_B_MAX_AGE_US", "LAB_B_RECOVERY_RUNS"])
def test_lab16_refuses_a_component_without_its_two_keys(topology, provisional, tmp_path, missing):
    broken = copy.deepcopy(provisional)
    del broken["running_example"]["B"][missing]
    with pytest.raises(GenerateError) as exc:
        write_default_yaml(tmp_path / "default.yaml", "running_example", topology.component("B"), broken)
    assert "B" in str(exc.value) and missing in str(exc.value)
    assert not (tmp_path / "default.yaml").exists()


def test_lab16_refuses_a_component_that_is_not_listed_at_all(topology, provisional, tmp_path):
    broken = copy.deepcopy(provisional)
    del broken["running_example"]["E"]
    with pytest.raises(GenerateError) as exc:
        generate(topology, broken, tmp_path / "gen", tmp_path / "lab")
    assert "component E" in str(exc.value) and "LAB_E_MAX_AGE_US" in str(exc.value)


def test_lab16_same_input_gives_byte_identical_output(topology, provisional, tmp_path):
    generate(topology, provisional, tmp_path / "gen1", tmp_path / "lab")
    index1 = (tmp_path / "lab" / "running_example" / "index.json").read_bytes()
    generate(topology, provisional, tmp_path / "gen2", tmp_path / "lab")
    index2 = (tmp_path / "lab" / "running_example" / "index.json").read_bytes()
    assert index1 == index2
    files1 = sorted(p.relative_to(tmp_path / "gen1") for p in (tmp_path / "gen1").rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(tmp_path / "gen2") for p in (tmp_path / "gen2").rglob("*") if p.is_file())
    assert files1 == files2 and files1
    for rel in files1:
        assert (tmp_path / "gen1" / rel).read_bytes() == (tmp_path / "gen2" / rel).read_bytes(), rel


def test_lab16_index_names_library_config_and_build_id_of_every_component(topology, provisional, tmp_path):
    generate(topology, provisional, tmp_path / "gen", tmp_path / "lab")
    index = json.loads((tmp_path / "lab" / "running_example" / "index.json").read_text(encoding="utf-8"))
    assert index["topology"] == "running_example"
    assert sorted(index["components"]) == ["A", "B", "C", "D", "E"]
    b = index["components"]["B"]
    assert b["library"].endswith("/running_example/B/lab_running_example_B.dll")
    assert b["config"].endswith("/running_example/B/config")
    assert re.fullmatch(r"[0-9a-f]{16}", b["build_id"])
    assert len({c["build_id"] for c in index["components"].values()}) == 5  # sources differ per component


def test_lab16_build_id_changes_with_the_generated_sources_not_the_calibration(topology, provisional, tmp_path):
    first = generate(topology, provisional, tmp_path / "gen1", tmp_path / "lab")
    changed = copy.deepcopy(provisional)
    changed["running_example"]["B"]["LAB_B_MAX_AGE_US"]["value"] = 999
    second = generate(topology, changed, tmp_path / "gen2", tmp_path / "lab")
    assert first["B"]["build_id"] == second["B"]["build_id"]
    assert (tmp_path / "gen2" / "running_example" / "B" / "default.yaml").read_text(encoding="utf-8").count("999") == 1


def test_lab54_provisional_gives_every_component_its_two_keys_marked_provisional(topology, provisional):
    components = provisional["running_example"]
    assert sorted(components) == sorted(c.name for c in topology.components)
    for name, keys in components.items():
        assert sorted(keys) == [f"LAB_{name}_MAX_AGE_US", f"LAB_{name}_RECOVERY_RUNS"]
        for entry in keys.values():
            assert entry["status"] == "provisional" and isinstance(entry["value"], int)
    assert components["B"]["LAB_B_MAX_AGE_US"]["value"] == 118000
    assert components["B"]["LAB_B_RECOVERY_RUNS"]["value"] == 2


def test_lab54_max_age_follows_the_derivation_of_design_section_7(topology, provisional):
    """twice the largest period + jitter among the producers, plus the component's own jitter."""
    periods = {s.name: s.period_ms + s.jitter_ms for s in topology.sources}
    periods.update({c.name: c.period_ms + c.jitter_ms for c in topology.components})
    for c in topology.components:
        largest = max(periods[i.producer] for i in c.inputs)
        expected_us = 2 * (largest + c.jitter_ms) * 1000
        assert provisional["running_example"][c.name][f"LAB_{c.name}_MAX_AGE_US"]["value"] == expected_us, c.name


# ---- LAB-08..14: the template holds no clock, thread, file, random value or unordered container ----

FORBIDDEN = re.compile(r"std::(thread|chrono|random|unordered)|rand\(|time\(|fopen|ifstream")


def test_lab08_the_algorithm_template_has_no_thread_clock_file_random_or_unordered_container():
    text = (TEMPLATE / "lab_component.hpp.in").read_text(encoding="utf-8")
    assert FORBIDDEN.search(text) is None


def test_lab08_the_algorithm_template_has_the_exec_shape_of_design_section_4_1():
    text = (TEMPLATE / "lab_component.hpp.in").read_text(encoding="utf-8")
    body = text[text.index("void exec("):text.index("private:")]
    order = ["cycle_.begin(control)", "refSync_.begin()", "refSync_.recordRead", "CycleControl::RESTART",
             "state_ = State{}", "calibrationError_", "firstUntrusted", "++state_.trustedStreak < recoveryRuns_",
             "Verdict::running(compute(in))", "publish(v, cycle_, refSync_.finish(cycle_), out)"]
    positions = [body.index(token) for token in order]
    assert positions == sorted(positions)
    code = "\n".join(line.split("//")[0] for line in body.splitlines())
    assert "return" not in code, "no path of exec returns early (IMP-0022)"


SCALAR_MEMBER = re.compile(r"^\s*(?:static constexpr )?(?:std::u?int(?:8|16|32|64)_t|bool|double|float|int|unsigned)\s+\w+\s*(.*);\s*$")
POINTER_MEMBER = re.compile(r"^\s*[\w:<>]+\*\s+\w+\s*(.*);\s*$")


def test_lab12_every_scalar_and_pointer_member_of_the_algorithm_has_an_initialiser():
    """no uninitialised member (RCT:IMP-0027): a member declaration line without '(' needs '=' or '{'."""
    text = (TEMPLATE / "lab_component.hpp.in").read_text(encoding="utf-8")
    checked = 0
    for line in text.splitlines():
        if "(" in line or line.lstrip().startswith(("//", "return", "for ", "const ", "using")):
            continue
        match = SCALAR_MEMBER.match(line) or POINTER_MEMBER.match(line)
        if match:
            checked += 1
            assert "=" in line or "{" in line, line
    assert checked >= 10
