# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/supervise.py
# @brief LAB-53: run_under_supervisor -- starts `simengine <setup> --supervise` on a lab run setup and returns the exit code, the attempt records and the outcome a reader saw sealed.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""The supervised run of the lab (``VANTAGE_Lab_Design.md`` section 6.2: the cases ``hang`` and ``crash``).

``run_under_supervisor`` starts the engine under its supervisor, ``simengine <setup> --supervise
--attempt-timeout-ms N [--restart-on-crash N]``. When ``reader`` (a path to a test reader that understands
``--until-finished <name>``) is given and the setup names a live measurement (``outputs.livemeas.name``), the
reader is started first and attached before the end, so it reads the status the run is left in -- the engine's own
closing outcome, or the supervisor's seal after a crash or a timeout.

It returns what it saw and decides nothing. The lab runs the engine as a process and never imports it.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

_OUTCOME = re.compile(r"^outcome=(\d+)\s*$", re.MULTILINE)
READER_GRACE_MS = 30000  # how long the reader waits beyond the supervised run


@dataclass
class SupervisedRun:
    """What a supervised run left: the supervisor's exit code, one record per attempt, the sealed outcome the reader
    printed (``None`` without a reader, or when the reader saw none) and the supervisor's standard error."""

    exit_code: int
    attempts: list[dict] = field(default_factory=list)
    sealed_outcome: int | None = None
    stderr: str = ""


def build_command(setup: Path, *, simengine: Path, attempt_timeout_ms: int, restart_on_crash: int = 0) -> list[str]:
    """``simengine <setup> --supervise --attempt-timeout-ms N [--restart-on-crash N]``."""
    command = [str(simengine), str(setup), "--supervise", "--attempt-timeout-ms", str(attempt_timeout_ms)]
    if restart_on_crash:
        command += ["--restart-on-crash", str(restart_on_crash)]
    return command


def attempts_path(setup: Path) -> Path:
    """Where the supervisor writes its attempt records: ``<run setup>.attempts.jsonl``."""
    return Path(str(setup) + ".attempts.jsonl")


def read_attempts(setup: Path) -> list[dict]:
    """The attempt records of ``setup``, one object per line, in order; empty when there is no file."""
    path = attempts_path(setup)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def live_measurement_name(setup: Path) -> str | None:
    """``outputs.livemeas.name`` of the run setup, or ``None`` when it names no live measurement."""
    declared = json.loads(Path(setup).read_text(encoding="utf-8")).get("outputs", {}).get("livemeas")
    return declared.get("name") if isinstance(declared, dict) else None


def parse_outcome(reader_output: str) -> int | None:
    """The number after ``outcome=`` in what the reader printed; ``None`` when it printed none."""
    found = _OUTCOME.findall(reader_output)
    return int(found[-1]) if found else None


def run_under_supervisor(setup: Path, *, simengine: Path, attempt_timeout_ms: int, restart_on_crash: int = 0,
                         reader: Path | None = None, cwd: Path | None = None, env: dict | None = None,
                         gate_file: Path | None = None) -> SupervisedRun:
    """The run setup under ``simengine --supervise``; with ``reader`` and a named live measurement, a reader
    attached first (``reader --until-finished <name>``). With ``gate_file`` as well, the run is held (by a stage the
    setup holds on that file) until the reader has printed ``attached``: the supervisor is started, the reader is
    waited for -- or the supervisor's end, or the reader's budget -- and then the file is created."""
    setup = Path(setup)
    name = live_measurement_name(setup) if reader is not None else None
    attempts_path(setup).unlink(missing_ok=True)
    reading = None
    if reader is not None and name is not None:
        budget_ms = attempt_timeout_ms * (restart_on_crash + 1) + READER_GRACE_MS
        reading = subprocess.Popen([str(reader), "--until-finished", name, "--timeout-ms", str(budget_ms)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    command = build_command(setup, simengine=simengine, attempt_timeout_ms=attempt_timeout_ms,
                            restart_on_crash=restart_on_crash)
    if reading is None or gate_file is None:
        done = subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env)
        output = None
    else:
        Path(gate_file).unlink(missing_ok=True)
        supervisor = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, env=env)
        lines: list[str] = []
        attached = threading.Event()

        def watch() -> None:
            for line in reading.stdout:
                lines.append(line)
                if line.strip() == "attached":
                    attached.set()

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        waited = 0.0
        while not attached.is_set() and supervisor.poll() is None and waited < budget_ms / 1000:
            attached.wait(0.05)
            waited += 0.05
        Path(gate_file).write_text("go\n", encoding="utf-8")
        stdout, stderr = supervisor.communicate()
        done = subprocess.CompletedProcess(command, supervisor.returncode, stdout, stderr)
        watcher.join(READER_GRACE_MS / 1000)
        output = "".join(lines)
    sealed = None
    if reading is not None:
        if output is None:
            try:
                output, _ = reading.communicate(timeout=READER_GRACE_MS / 1000)
            except subprocess.TimeoutExpired:
                reading.kill()
                output, _ = reading.communicate()
        else:
            reading.wait()
        sealed = parse_outcome(output or "")
    return SupervisedRun(done.returncode, read_attempts(setup), sealed, done.stderr)
