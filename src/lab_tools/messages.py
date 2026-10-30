# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/messages.py
# @brief LAB-20: message classes built at run time from a library's own FileDescriptorSet (R-SIM-11).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""``MessageFactory``: a private descriptor pool filled from the ``FileDescriptorSet`` a library
carries in its manifest, so a host needs no generated Python bindings and no sibling checkout."""
from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


class MessageFactory:
    """Builds message classes by full name (``"adas.lab.LabSample"``) from one descriptor set."""

    def __init__(self, descriptor_set: bytes):
        files = descriptor_pb2.FileDescriptorSet()
        files.ParseFromString(descriptor_set)
        if not files.file:
            raise ValueError("the descriptor set holds no file")
        self._pool = descriptor_pool.DescriptorPool()
        for file in files.file:  # dependencies come first in a set the library writes
            self._pool.AddSerializedFile(file.SerializeToString())
        self._classes: dict[str, type] = {}

    @classmethod
    def from_library(cls, library) -> "MessageFactory":
        """Build from ``LabLibrary.manifest().descriptor_set``."""
        return cls(library.manifest().descriptor_set)

    def message_class(self, full_name: str) -> type:
        if full_name not in self._classes:
            self._classes[full_name] = message_factory.GetMessageClass(self._pool.FindMessageTypeByName(full_name))
        return self._classes[full_name]

    def new(self, full_name: str, **fields):
        return self.message_class(full_name)(**fields)

    def parse(self, full_name: str, data: bytes):
        message = self.message_class(full_name)()
        message.ParseFromString(data)
        return message
