# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/recorder.py
# @brief LAB-29, LAB-51: writes the fake car's recording as an .mcap file, applying the scenario's recording edits (VANTAGE_Lab_Design.md sections 5.5 and 5.6).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""``McapRecorder``: one channel per declared topic, schemas taken from the libraries' own manifests,
``log_time`` in integer nanoseconds, and metadata that holds no wall-clock time, so one topology,
scenario and seed always give byte-identical files.

Topics are registered in sorted order before the first message, so channel and schema numbers never
depend on dict or event order. Messages go in the order ``write`` is called; equal logging times keep
that order. The metadata blocks use the conventions of the recordings Vantage reads: ``manifest``
(one ``yaml`` entry, ``manifest_version`` 1 so a reader knows ``publish_time`` is not a capture time)
and ``topic_provenance`` (topic -> origin tag).

A scenario's ``recording:`` edits (``drop_sample``, ``drop_ref_sync``) are applied here, when the file is
written (``apply_recording_edits``): the car's bus still had the sample, and the refSync of a run still names
a dropped sample, so the file is a recording with a hole in it, which is what the drills need.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping

import yaml
from mcap.writer import Writer

MANIFEST_VERSION = 1  # below the capture-time version: publish_time equals log_time and is not a capture stamp
LIBRARY_ID = "vantage-lab"
MANIFEST_BLOCK = "manifest"
PROVENANCE_BLOCK = "topic_provenance"


class RecorderError(RuntimeError):
    """The recorder was asked for something that would make the file depend on call order or luck."""


@dataclass(frozen=True)
class RecordingEdits:
    """What a scenario's ``recording:`` block asks of the file: ``drop_samples`` is a set of
    ``(topic, counter)`` pairs (``source.Y``, 20) left out; ``drop_ref_sync`` writes no ``refSync`` topic."""

    drop_samples: frozenset[tuple[str, int]] = frozenset()
    drop_ref_sync: bool = False

    def __bool__(self) -> bool:
        return bool(self.drop_samples) or self.drop_ref_sync

    def as_manifest(self) -> dict[str, object]:
        """The edits as the ``manifest`` metadata states them, so a reader knows the hole is deliberate."""
        result: dict[str, object] = {}
        if self.drop_samples:
            result["drop_sample"] = [{"port": t, "counter": c} for t, c in sorted(self.drop_samples)]
        if self.drop_ref_sync:
            result["drop_ref_sync"] = "all"
        return result


def apply_recording_edits(edits: RecordingEdits, topic: str, counter: int | None = None) -> bool:
    """Whether ``topic`` is written, and with ``counter`` (a source sample's ``measurement_counter``) that
    message is. ``drop_ref_sync`` removes every component's ``refSync`` topic; ``drop_sample`` removes the
    sample of that counter from a source topic. A message with no ``counter`` is never dropped as a sample."""
    if edits.drop_ref_sync and not topic.startswith("source.") and topic.split(".", 1)[-1] == "refSync":
        return False
    return counter is None or (topic, counter) not in edits.drop_samples


@dataclass(frozen=True)
class TopicSpec:
    """What a topic carries: the message's full name, the descriptor set to decode it, its origin tag."""

    schema_name: str
    descriptor_set: bytes
    provenance: str  # "mocked" for a source, "simulated" for a component port (R-SIM-12)


class McapRecorder:
    """Writes one recording. Use as a context manager, or call ``close``."""

    def __init__(self, path: str | Path, topics: Mapping[str, TopicSpec], manifest: Mapping[str, object],
                 edits: RecordingEdits = RecordingEdits()):
        self.path = Path(path)
        self._edits = edits
        topics = {t: spec for t, spec in topics.items() if apply_recording_edits(edits, t)}
        self._file: IO[bytes] | None = self.path.open("wb")
        self._writer = Writer(self._file)
        self._writer.start(profile="", library=LIBRARY_ID)
        self._channels: dict[str, int] = {}
        self._sequence: dict[str, int] = {}
        self._counts: dict[str, int] = {}
        self._last_time_us = 0
        schemas: dict[tuple[str, bytes], int] = {}
        for topic in sorted(topics):  # channel numbers are a function of the topic names alone
            spec = topics[topic]
            key = (spec.schema_name, spec.descriptor_set)
            if key not in schemas:
                schemas[key] = self._writer.register_schema(
                    name=spec.schema_name, encoding="protobuf", data=spec.descriptor_set)
            self._channels[topic] = self._writer.register_channel(
                topic=topic, message_encoding="protobuf", schema_id=schemas[key])
            self._sequence[topic] = 0
            self._counts[topic] = 0
        block = dict(manifest)
        block["manifest_version"] = MANIFEST_VERSION
        self._writer.add_metadata(MANIFEST_BLOCK, {"yaml": yaml.safe_dump(block, sort_keys=True)})
        self._writer.add_metadata(PROVENANCE_BLOCK, {t: topics[t].provenance for t in sorted(topics)})

    @property
    def counts(self) -> dict[str, int]:
        """Messages written per topic so far."""
        return dict(self._counts)

    def write(self, topic: str, log_time_us: int, payload: bytes, counter: int | None = None) -> None:
        """Append one message logged at ``log_time_us`` (integer µs); times never go backwards. ``counter`` is
        a source sample's ``measurement_counter``; a recording edit may leave the message out (LAB-51)."""
        if self._file is None:
            raise RecorderError("write after close")
        if not apply_recording_edits(self._edits, topic, counter):
            return
        if topic not in self._channels:
            raise RecorderError(f"topic {topic!r} was not declared (declared: {', '.join(sorted(self._channels))})")
        if log_time_us < self._last_time_us:
            raise RecorderError(f"{topic}: logging time {log_time_us} us is before {self._last_time_us} us already written")
        self._last_time_us = log_time_us
        ns = log_time_us * 1000
        self._writer.add_message(channel_id=self._channels[topic], log_time=ns, data=bytes(payload),
                                 publish_time=ns, sequence=self._sequence[topic])
        self._sequence[topic] += 1
        self._counts[topic] += 1

    def close(self) -> None:
        if self._file is not None:
            self._writer.finish()
            self._file.close()
            self._file = None

    def __enter__(self) -> "McapRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
