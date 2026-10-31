# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/compare.py
# @brief LAB-38: equal_outputs -- two output sequences are equal when they have the same count and each pair is byte-identical once cycle_counter is 0.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""*Equal outputs* of the resim contract (``RCT_System_Requirements.md`` preamble, ``D16``): two sequences of one
port's messages are equal when they hold the same number of messages and each pair is byte-identical once both
headers' ``cycle_counter`` is set to 0. Nothing else is left out: a differing time, counter, sticker or value
is a difference. The messages are decoded with the schema the recording carries (R-SIM-11), so the lab needs no
generated bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .messages import MessageFactory

HEADER_FIELD = "sig_header"
CYCLE_COUNTER = "cycle_counter"


@dataclass(frozen=True)
class OutputsComparison:
    """The result of ``equal_outputs``; truthy when the two sequences are equal."""

    equal: bool
    recorded_count: int
    reproduced_count: int
    first_difference: int | None = None  # index of the first pair that differs; None when the counts differ
    reason: str = "equal"

    def __bool__(self) -> bool:
        return self.equal


def normalised(factory: MessageFactory, type_name: str, payload: bytes) -> bytes:
    """``payload`` re-encoded deterministically with the header's ``cycle_counter`` set to 0."""
    message = factory.parse(type_name, payload)
    if HEADER_FIELD in message.DESCRIPTOR.fields_by_name:
        getattr(message, HEADER_FIELD).cycle_counter = 0
    return message.SerializeToString(deterministic=True)


def equal_outputs(recorded: Sequence[bytes], reproduced: Sequence[bytes], factory: MessageFactory,
                  type_name: str) -> OutputsComparison:
    """Compare two sequences of serialized ``type_name`` messages as *equal outputs*."""
    if len(recorded) != len(reproduced):
        return OutputsComparison(False, len(recorded), len(reproduced),
                                 reason=f"{len(recorded)} recorded messages, {len(reproduced)} reproduced")
    for i, (a, b) in enumerate(zip(recorded, reproduced)):
        if normalised(factory, type_name, a) != normalised(factory, type_name, b):
            return OutputsComparison(False, len(recorded), len(reproduced), i,
                                     f"message {i} differs outside cycle_counter")
    return OutputsComparison(True, len(recorded), len(reproduced))
