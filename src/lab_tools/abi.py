# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/abi.py
# @brief LAB-19: loads a lab library through ctypes and speaks the generic SIL ABI (adas_sil_abi.h).
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""ABI loader: ``LabLibrary`` over ``adasAbiVersion`` / ``adasManifest`` / ``adasInit`` / ``adasExec`` /
``adasShutdown``, with ``AdasPort`` and ``AdasPortSet`` mirroring ``adas_sil_abi.h`` field for field.

The library path is always a value read from the build tree's ``index.json`` (``load_index``), never
built from a guess. The manifest is decoded by a small wire reader because the schema of
``ComponentManifest`` is not itself in the manifest's descriptor set; the port types are
(``lab_tools.messages.MessageFactory``). No generated protobuf bindings are used (R-SIM-11).
"""
from __future__ import annotations

import ctypes
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

ABI_VERSION = 1


class LabAbiError(RuntimeError):
    """The library does not honour the generic ABI, or a call on it failed."""


class AdasPort(ctypes.Structure):
    """One port, either direction (``AdasPort`` of adas_sil_abi.h; 48 bytes)."""

    _fields_ = [
        ("name", ctypes.c_char_p),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("len", ctypes.c_size_t),
        ("cap", ctypes.c_size_t),
        ("ageS", ctypes.c_double),
        ("valid", ctypes.c_int32),
        ("updated", ctypes.c_int32),
    ]


class AdasPortSet(ctypes.Structure):
    """A counted array of ports (``AdasPortSet`` of adas_sil_abi.h; 16 bytes)."""

    _fields_ = [("ports", ctypes.POINTER(AdasPort)), ("count", ctypes.c_size_t)]


def _check_layout() -> None:
    """The same offsets adas_sil_abi.h asserts at compile time; an ABI break fails at import."""
    expected = {"name": 0, "data": 8, "len": 16, "cap": 24, "ageS": 32, "valid": 40, "updated": 44}
    for field_name, offset in expected.items():
        if getattr(AdasPort, field_name).offset != offset:
            raise LabAbiError(f"AdasPort.{field_name} is at {getattr(AdasPort, field_name).offset}, expected {offset}")
    if ctypes.sizeof(AdasPort) != 48 or ctypes.sizeof(AdasPortSet) != 16:
        raise LabAbiError("AdasPort / AdasPortSet size differs from adas_sil_abi.h")


_check_layout()


@dataclass(frozen=True)
class InputPort:
    """One input to hand over. ``payload`` None offers the port with ``valid`` 0 (nothing received)."""

    name: str
    payload: bytes | None
    age_s: float = 0.0


class PortSet:
    """An ``AdasPortSet`` and the buffers its pointers refer to, kept alive together."""

    def __init__(self, names: Sequence[str], payloads: Sequence[bytes | None], caps: Sequence[int],
                 ages: Sequence[float], valids: Sequence[int]):
        self._names = [n.encode("utf-8") for n in names]
        self._buffers = [ctypes.create_string_buffer(bytes(p) if p else b"", max(cap, len(p or b""), 1))
                         for p, cap in zip(payloads, caps)]
        self._ports = (AdasPort * len(names))()
        for i, port in enumerate(self._ports):
            port.name = self._names[i]
            port.data = ctypes.cast(self._buffers[i], ctypes.POINTER(ctypes.c_uint8))
            port.len = len(payloads[i] or b"")
            port.cap = caps[i]
            port.ageS = ages[i]
            port.valid = valids[i]
            port.updated = 0
        self.port_set = AdasPortSet(ctypes.cast(self._ports, ctypes.POINTER(AdasPort)), len(names))

    @classmethod
    def for_inputs(cls, inputs: Iterable[InputPort]) -> "PortSet":
        items = list(inputs)
        return cls([i.name for i in items], [i.payload for i in items],
                   [len(i.payload or b"") for i in items], [i.age_s for i in items],
                   [0 if i.payload is None else 1 for i in items])

    @classmethod
    def for_outputs(cls, names: Sequence[str], cap: int = 4096) -> "PortSet":
        return cls(names, [None] * len(names), [cap] * len(names), [0.0] * len(names), [0] * len(names))

    def outputs(self) -> dict[str, bytes | None]:
        """For an output set after ``exec``: name -> bytes written, None where the library wrote nothing."""
        result: dict[str, bytes | None] = {}
        for port in self._ports:
            written = bytes(ctypes.string_at(port.data, port.len)) if port.updated and port.len else b""
            result[port.name.decode("utf-8")] = written if port.updated else None
        return result


@dataclass(frozen=True)
class PortDecl:
    name: str
    proto_type: str
    max_bytes: int = 0
    optional: bool = False
    owner_function: str = ""


@dataclass(frozen=True)
class Manifest:
    """The fields of ``adas.sil.ComponentManifest`` a host reads."""

    component: str = ""
    version: str = ""
    abi_version: int = 0
    build_id: str = ""
    project: str = ""
    config_bundle_sha256: str = ""
    period_ns: int = 0
    inputs: tuple[PortDecl, ...] = ()
    outputs: tuple[PortDecl, ...] = ()
    descriptor_set: bytes = field(default=b"", repr=False)


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def wire_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Yield (field number, value) of one serialized message; int for varints, bytes otherwise."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, pos = _varint(buf, pos)
        elif kind == 2:
            length, pos = _varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif kind == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif kind == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise LabAbiError(f"unsupported wire type {kind} in the manifest")
        yield number, value


def decode_manifest(raw: bytes) -> Manifest:
    text = {1: "component", 2: "version", 4: "build_id", 7: "project", 8: "config_bundle_sha256"}
    numbers = {3: "abi_version", 10: "period_ns"}
    values: dict[str, object] = {}
    inputs: list[PortDecl] = []
    outputs: list[PortDecl] = []
    for number, value in wire_fields(raw):
        if number in text:
            values[text[number]] = value.decode("utf-8")
        elif number in numbers:
            values[numbers[number]] = value
        elif number == 9:
            values["descriptor_set"] = value
        elif number in (5, 6):
            spec = dict(wire_fields(value))
            decl = PortDecl(name=spec.get(1, b"").decode("utf-8"), proto_type=spec.get(2, b"").decode("utf-8"),
                            max_bytes=spec.get(3, 0), optional=bool(spec.get(4, 0)),
                            owner_function=spec.get(5, b"").decode("utf-8"))
            (inputs if number == 5 else outputs).append(decl)
    return Manifest(inputs=tuple(inputs), outputs=tuple(outputs), **values)  # type: ignore[arg-type]


def load_index(index_path: str | Path) -> dict:
    """Read a build tree's ``index.json``: ``{"topology", "interfaces_python", "components": {name: {library, config,
    build_id}}}``; ``interfaces_python`` is the ``interfaces`` package's ``python/`` folder, where the recording writer is."""
    return json.loads(Path(index_path).read_text(encoding="utf-8"))


class LabLibrary:
    """One loaded lab library and, after ``init``, one component instance."""

    def __init__(self, path: Path, cdll: ctypes.CDLL):
        self.path = path
        self._lib = cdll
        self._handle: int | None = None
        cdll.adasAbiVersion.restype = ctypes.c_int32
        cdll.adasAbiVersion.argtypes = []
        cdll.adasManifest.restype = ctypes.c_size_t
        cdll.adasManifest.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        cdll.adasInit.restype = ctypes.c_void_p
        cdll.adasInit.argtypes = [ctypes.c_char_p]
        cdll.adasExec.restype = ctypes.c_int32
        cdll.adasExec.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.POINTER(AdasPortSet),
                                  ctypes.POINTER(AdasPortSet)]
        cdll.adasShutdown.restype = None
        cdll.adasShutdown.argtypes = [ctypes.c_void_p]

    @classmethod
    def load(cls, path: str | Path) -> "LabLibrary":
        """Load the library at ``path`` (a value from ``index.json``) and check its ABI generation."""
        library = Path(path)
        if not library.is_file():
            raise LabAbiError(f"no library at {library}")
        lab = cls(library, ctypes.CDLL(str(library)))
        found = lab._lib.adasAbiVersion()
        if found != ABI_VERSION:
            raise LabAbiError(f"{library.name} speaks ABI {found}, this loader speaks {ABI_VERSION}")
        return lab

    @property
    def handle(self) -> int | None:
        """The component instance ``init`` returned; None before ``init`` and after ``shutdown``."""
        return self._handle

    def manifest_bytes(self) -> bytes:
        size = self._lib.adasManifest(None, 0)
        if size == 0:
            raise LabAbiError(f"{self.path.name}: adasManifest returned 0 bytes")
        buf = ctypes.create_string_buffer(size)
        if self._lib.adasManifest(buf, size) != size:
            raise LabAbiError(f"{self.path.name}: adasManifest changed size between the two calls")
        return buf.raw[:size]

    def manifest(self) -> Manifest:
        return decode_manifest(self.manifest_bytes())

    def init(self, config_dir: str | Path | None = None) -> int:
        """``adasInit``; None resolves the bundle beside the library. A NULL handle raises."""
        if self._handle is not None:
            raise LabAbiError("init called twice without shutdown")
        argument = None if config_dir is None else str(config_dir).encode("utf-8")
        handle = self._lib.adasInit(argument)
        if not handle:
            raise LabAbiError(f"{self.path.name}: adasInit returned NULL (config {config_dir or 'beside the library'})")
        self._handle = handle
        return handle

    def exec(self, inputs: PortSet, outputs: PortSet, dt_s: float = 0.0) -> bool:
        """``adasExec`` once; True when the library returned 1."""
        if self._handle is None:
            raise LabAbiError("exec called before init")
        return self._lib.adasExec(self._handle, dt_s, ctypes.byref(inputs.port_set),
                                  ctypes.byref(outputs.port_set)) == 1

    def shutdown(self) -> None:
        if self._handle is not None:
            self._lib.adasShutdown(self._handle)
            self._handle = None

    def __enter__(self) -> "LabLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
