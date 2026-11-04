# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/unit/test_supervise.py
# @brief LAB-53: the argument building and the record parsing of lab_tools.supervise; no engine is started.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
import json
import sys
from pathlib import Path

from lab_tools import supervise

ENGINE = Path("bin") / "simengine"


def test_lab53_command_has_supervise_and_the_deadline_and_no_restart_by_default():
    command = supervise.build_command(Path("s.run.json"), simengine=ENGINE, attempt_timeout_ms=5000)
    assert command == [str(ENGINE), "s.run.json", "--supervise", "--attempt-timeout-ms", "5000"]


def test_lab53_command_adds_restart_on_crash_only_when_asked():
    command = supervise.build_command(Path("s.run.json"), simengine=ENGINE, attempt_timeout_ms=300, restart_on_crash=2)
    assert command[-2:] == ["--restart-on-crash", "2"]
    assert "--restart-on-crash" not in supervise.build_command(Path("s.run.json"), simengine=ENGINE, attempt_timeout_ms=300)


def test_lab53_attempt_records_are_read_one_per_line_in_order(tmp_path):
    setup = tmp_path / "s.run.json"
    assert supervise.read_attempts(setup) == []
    supervise.attempts_path(setup).write_text(
        '{"attempt":1,"outcome":"crash","last":false,"stages":"unknown"}\n\n'
        '{"attempt":2,"outcome":"crash","last":true,"stages":["B"]}\n', encoding="utf-8")
    records = supervise.read_attempts(setup)
    assert [r["attempt"] for r in records] == [1, 2]
    assert records[1]["stages"] == ["B"] and records[1]["last"] is True


def test_lab53_the_live_measurement_name_is_the_setups_or_none(tmp_path):
    setup = tmp_path / "s.run.json"
    setup.write_text(json.dumps({"outputs": {"livemeas": {"name": "lab_x"}}}), encoding="utf-8")
    assert supervise.live_measurement_name(setup) == "lab_x"
    setup.write_text(json.dumps({"outputs": {}}), encoding="utf-8")
    assert supervise.live_measurement_name(setup) is None


def test_lab53_a_gate_file_is_only_used_with_a_reader_and_a_named_measurement(tmp_path):
    setup = tmp_path / "s.run.json"
    setup.write_text(json.dumps({"outputs": {}}), encoding="utf-8")
    gate = tmp_path / "s.gate"
    run = supervise.run_under_supervisor(setup, simengine=Path(sys.executable), attempt_timeout_ms=1000,
                                         reader=Path(sys.executable), gate_file=gate)
    assert run.sealed_outcome is None and not gate.exists()


def test_lab53_the_sealed_outcome_is_the_number_the_reader_printed():
    assert supervise.parse_outcome("attached\noutcome=3\n") == 3
    assert supervise.parse_outcome("attached\n") is None
