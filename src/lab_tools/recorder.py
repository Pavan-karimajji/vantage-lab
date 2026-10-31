# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/recorder.py
# @brief LAB-29, LAB-51, LAB-55, LAB-62: the scenario's recording edits, and the fake car's recording written through the interfaces writer (VANTAGE_Lab_Design.md sections 5.5 and 5.6).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""The fake car's recording: the lab's own recording edits, written through the one Python writer in the
``interfaces`` package (``adas_recording.McapRecorder``, ``RCT_Architecture.md`` section 6a, ``D22``).

The writer is found through ``index.json`` (``interfaces_python``, the package's ``python/`` folder, put on the
import path by ``use_writer`` before the first recording), never through a sibling checkout. Topics, channel
numbers, message order, times and metadata are the writer's. The lab gives only its own argument values: no
logging time (so one topology, scenario and seed always give byte-identical files), sources ``mocked``,
component ports ``simulated`` (the caller's ``TopicSpec.provenance``), every port ``full`` in ``record_policies``
except the ports a drill drops, which are ``excluded`` so the hole is stated, and ``library="vantage-lab"``.

A scenario's ``recording:`` edits (``drop_sample``, ``drop_ref_sync``, ``repeat_sample``) are applied here, when the file is
written (``apply_recording_edits``): the car's bus still had the sample, and the refSync of a run still names
a dropped sample, so the file is a recording with a hole in it, which is what the drills need. A repeated sample
is written a second time, byte-identical, ``REPEAT_DELAY_US`` after the original (LAB-62); the car's bus has it once.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

LIBRARY_ID = "vantage-lab"
REPEAT_DELAY_US = 1000  # LAB-62: a repeated sample is written this long after the original


def use_writer(python_dir: str | Path) -> None:
    """Put the ``interfaces`` package's ``python/`` folder (``index.json`` ``interfaces_python``) on the import path."""
    if str(python_dir) not in sys.path:
        sys.path.insert(0, str(python_dir))


@dataclass(frozen=True)
class RecordingEdits:
    """What a scenario's ``recording:`` block asks of the file: ``drop_samples`` is a set of
    ``(topic, counter)`` pairs (``source.Y``, 20) left out; ``drop_ref_sync`` writes no ``refSync`` topic;
    ``repeat_samples`` is a set of such pairs written a second time (LAB-62)."""

    drop_samples: frozenset[tuple[str, int]] = frozenset()
    drop_ref_sync: bool = False
    repeat_samples: frozenset[tuple[str, int]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.drop_samples) or self.drop_ref_sync or bool(self.repeat_samples)

    def as_manifest(self) -> dict[str, object]:
        """The edits as the ``manifest`` metadata states them, so a reader knows the hole is deliberate."""
        result: dict[str, object] = {}
        if self.drop_samples:
            result["drop_sample"] = [{"port": t, "counter": c} for t, c in sorted(self.drop_samples)]
        if self.drop_ref_sync:
            result["drop_ref_sync"] = "all"
        if self.repeat_samples:
            result["repeat_sample"] = [{"port": t, "counter": c} for t, c in sorted(self.repeat_samples)]
        return result


def apply_recording_edits(edits: RecordingEdits, topic: str, counter: int | None = None) -> int:
    """How many times ``topic`` is written, and with ``counter`` (a source sample's ``measurement_counter``) that
    message is: 0 when left out, 1 normally, 2 when ``repeat_sample`` names it (the second copy goes
    ``REPEAT_DELAY_US`` later). ``drop_ref_sync`` removes every component's ``refSync`` topic; ``drop_sample``
    removes the sample of that counter from a source topic. A message with no ``counter`` is never dropped or
    repeated as a sample."""
    if edits.drop_ref_sync and not topic.startswith("source.") and topic.split(".", 1)[-1] == "refSync":
        return 0
    if counter is None:
        return 1
    if (topic, counter) in edits.drop_samples:
        return 0
    return 2 if (topic, counter) in edits.repeat_samples else 1


@dataclass(frozen=True)
class TopicSpec:
    """What a topic carries: the message's full name, the descriptor set to decode it, its origin tag."""

    schema_name: str
    descriptor_set: bytes
    provenance: str  # "mocked" for a source, "simulated" for a component port (R-SIM-12)


class McapRecorder:
    """Writes one recording through ``adas_recording.McapRecorder``. Use as a context manager, or call ``close``.

    ``topics`` are every declared port; the ports the edits drop are listed ``excluded`` in ``record_policies``
    and get no topic, every other port is ``full``. ``manifest`` states ``source_kind``, ``run_kind`` and
    ``components`` itself; the writer refuses a file that does not (``RecordingError``)."""

    def __init__(self, path: str | Path, topics: Mapping[str, TopicSpec], manifest: Mapping[str, object],
                 edits: RecordingEdits = RecordingEdits()):
        from adas_recording.writer import McapRecorder as Writer, TopicSpec as WriterTopic  # use_writer first

        self.path = Path(path)
        self._edits = edits
        self._later: list[tuple[int, str, bytes]] = []  # repeated samples waiting for their time (LAB-62)
        self.repeated: set[tuple[str, int]] = set()  # the repeats that were written
        written = {t: spec for t, spec in topics.items() if apply_recording_edits(edits, t)}
        block = dict(manifest)
        block["record_policies"] = {t: "full" if t in written else "excluded" for t in sorted(topics)}
        self._writer = Writer(self.path, {t: WriterTopic(s.schema_name, s.descriptor_set, s.provenance)
                                          for t, s in written.items()}, block, library=LIBRARY_ID)

    @property
    def counts(self) -> dict[str, int]:
        """Messages written per topic so far."""
        return self._writer.counts

    def write(self, topic: str, log_time_us: int, payload: bytes, counter: int | None = None) -> None:
        """Append one message logged at ``log_time_us`` (integer us); times never go backwards. ``counter`` is
        a source sample's ``measurement_counter``; a recording edit may leave the message out (LAB-51)."""
        self._flush_until(log_time_us)
        copies = apply_recording_edits(self._edits, topic, counter)
        if copies:
            self._writer.write(topic, log_time_us, payload)
        if copies > 1:  # LAB-62: the same bytes again; the writer's times never go back, so it waits its turn
            self._later.append((log_time_us + REPEAT_DELAY_US, topic, payload))
            self.repeated.add((topic, counter))

    def _flush_until(self, log_time_us: int | None) -> None:
        """Write the repeats due at or before ``log_time_us`` (all of them when ``None``), before the next message."""
        while self._later and (log_time_us is None or self._later[0][0] <= log_time_us):
            due_us, topic, payload = self._later.pop(0)
            self._writer.write(topic, due_us, payload)

    def close(self) -> None:
        self._flush_until(None)
        self._writer.close()

    def __enter__(self) -> "McapRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
