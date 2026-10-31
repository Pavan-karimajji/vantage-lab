# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/unit/test_topology.py
# @brief LAB-07: the topology loader and every check of design section 3.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
import textwrap
from pathlib import Path

import pytest

from lab_tools.topology import TopologyError, load_topology

LAB_ROOT = Path(__file__).resolve().parents[2]
RUNNING_EXAMPLE = LAB_ROOT / "catalogue" / "running_example.yaml"


def write_topology(tmp_path: Path, body: str, name: str = "t") -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(f"topology: {name}\nseed: 1\nduration_ms: 100\n" + textwrap.dedent(body), encoding="utf-8")
    return path


def test_lab07_running_example_loads_clean():
    topology = load_topology(RUNNING_EXAMPLE)
    assert topology.name == "running_example"
    assert [s.name for s in topology.sources] == ["X", "Y", "Z"]
    assert [c.name for c in topology.components] == ["A", "B", "C", "D", "E"]
    b = topology.component("B")
    assert [i.name for i in b.inputs] == ["in_A", "in_X", "in_Y", "in_Z"]
    assert (b.period_ms, b.phase_ms, b.jitter_ms, b.memory, b.outputs) == (40, 60, 4, True, ("out",))
    a = topology.component("A")
    assert (a.phase_ms, a.memory) == (20, False)  # phase defaults to the period
    assert [s.name for s in topology.scenarios] == ["base", "restart", "fault", "missing_sample", "missing_run",
                                                    "no_ref_sync", "counter_jump"]
    assert [case["name"] for case in topology.scenarios[0].resim] == ["cut_c", "all_simulated", "jump", "hang", "crash"]


def test_lab07_producer_that_does_not_exist(tmp_path):
    path = write_topology(tmp_path, """\
        sources: [{name: X, period_ms: 10}]
        components: [{name: A, period_ms: 20, inputs: [{producer: Q}]}]
        """)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.file == str(path)
    assert exc.value.key == "components[A].inputs[0].producer"
    assert str(path) in str(exc.value) and "components[A].inputs[0].producer" in str(exc.value)


@pytest.mark.parametrize("clock,case", [
    ("period_ms: 20, trigger: in_X", "both"),
    ("", "neither"),
])
def test_lab07_both_or_neither_of_period_and_trigger(tmp_path, clock, case):
    path = write_topology(tmp_path, f"""\
        sources: [{{name: X, period_ms: 10}}]
        components: [{{name: A, {clock + ', ' if clock else ''}inputs: [{{producer: X}}]}}]
        """)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key == "components[A]"
    assert case in exc.value.message


def test_lab07_trigger_that_is_not_a_declared_input(tmp_path):
    path = write_topology(tmp_path, """\
        sources: [{name: X, period_ms: 10}]
        components: [{name: A, trigger: in_Q, inputs: [{producer: X}]}]
        """)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key == "components[A].trigger"


def test_lab07_trigger_that_is_a_declared_input_loads(tmp_path):
    path = write_topology(tmp_path, """\
        sources: [{name: X, period_ms: 30}]
        components: [{name: T, trigger: in_X, inputs: [{producer: X}]}]
        """)
    t = load_topology(path)
    assert t.component("T").trigger == "in_X" and t.component("T").period_ms is None


@pytest.mark.parametrize("body,key", [
    ("""\
     sources: [{name: X, period_ms: 10}, {name: X, period_ms: 20}]
     components: []
     """, "sources[X].name"),
    ("""\
     sources: []
     components: [{name: A, period_ms: 20, inputs: []}, {name: A, period_ms: 40, inputs: []}]
     """, "components[A].name"),
    ("""\
     sources: [{name: A, period_ms: 10}]
     components: [{name: A, period_ms: 20, inputs: []}]
     """, "components[A].name"),
])
def test_lab07_duplicate_name(tmp_path, body, key):
    path = write_topology(tmp_path, body)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key == key
    assert "duplicate" in exc.value.message


def test_lab07_loop_through_inputs_not_marked_previous_cycle(tmp_path):
    path = write_topology(tmp_path, """\
        sources: [{name: X, period_ms: 10}]
        components:
          - {name: A, period_ms: 20, inputs: [{producer: X}, {producer: B}]}
          - {name: B, period_ms: 20, inputs: [{producer: A}]}
        """)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key.startswith("components[") and exc.value.key.endswith(".inputs")
    assert "A" in exc.value.message and "B" in exc.value.message


def test_lab07_loop_broken_by_previous_cycle_loads(tmp_path):
    path = write_topology(tmp_path, """\
        sources: [{name: X, period_ms: 10}]
        components:
          - {name: A, period_ms: 20, inputs: [{producer: X}, {producer: B, previous_cycle: true}]}
          - {name: B, period_ms: 20, inputs: [{producer: A}]}
        """)
    assert load_topology(path).component("A").inputs[1].previous_cycle is True


def test_lab07_loop_is_exempt_only_with_expect_refused_at_load(tmp_path):
    body = """\
        sources: [{name: X, period_ms: 10}]
        components:
          - {name: A, period_ms: 20, inputs: [{producer: X}, {producer: B}]}
          - {name: B, period_ms: 20, inputs: [{producer: A}]}
        scenarios:
          - {name: base, expect: %s}
        """
    with pytest.raises(TopologyError):
        load_topology(write_topology(tmp_path, body % "exact", "plain"))
    assert load_topology(write_topology(tmp_path, body % "refused_at_load", "refused")).refused_at_load


def test_lab07_self_loop_is_a_loop(tmp_path):
    path = write_topology(tmp_path, """\
        sources: []
        components: [{name: A, period_ms: 20, inputs: [{producer: A}]}]
        """)
    with pytest.raises(TopologyError):
        load_topology(path)


def test_lab07_unknown_key_names_the_key(tmp_path):
    path = write_topology(tmp_path, """\
        sources: []
        components: [{name: A, period_ms: 20, inputs: [], colour: red}]
        """)
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key == "components[A].colour"


def test_lab07_topology_name_must_equal_the_file_name(tmp_path):
    path = tmp_path / "other.yaml"
    path.write_text("topology: t\nseed: 1\nduration_ms: 100\nsources: []\ncomponents: []\n", encoding="utf-8")
    with pytest.raises(TopologyError) as exc:
        load_topology(path)
    assert exc.value.key == "topology"


def test_lab07_missing_file_is_a_topology_error(tmp_path):
    with pytest.raises(TopologyError) as exc:
        load_topology(tmp_path / "absent.yaml")
    assert "absent.yaml" in str(exc.value)
