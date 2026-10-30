# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/unit/test_libraries_smoke.py
# @brief LAB-13, LAB-17: every built library loads in ONE process and answers adasAbiVersion and adasManifest.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Smoke test of the built libraries (the real ABI loader is LAB-WP-03's ``LabLibrary``).

Skipped, with the reason printed, when the build tree is absent or holds libraries of another
operating system. The manifest is read with a small wire reader here because the schema of
``ComponentManifest`` itself is not in the manifest; ``MessageFactory`` (LAB-20) replaces it.
"""
import ctypes
import json
import os
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY = "running_example"
COMPONENTS = ["A", "B", "C", "D", "E"]
DECLARED = {"A": ["in_X"], "B": ["in_A", "in_X", "in_Y", "in_Z"], "C": ["in_X"], "D": ["in_C"], "E": ["in_Y"]}


def find_index() -> Path | None:
    explicit = os.environ.get("LAB_INDEX")
    if explicit:
        return Path(explicit)
    found = sorted((LAB_ROOT / "conan_workarea").glob(f"build-sil-*/lab/{TOPOLOGY}/index.json"))
    return found[-1] if found else None


def wire_fields(buf: bytes):
    """Yield (field number, value) of one protobuf message; value is int for varints, bytes otherwise."""
    pos = 0
    while pos < len(buf):
        key = shift = 0
        while True:
            b = buf[pos]; pos += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        number, kind = key >> 3, key & 7
        if kind == 0:
            value = shift = 0
            while True:
                b = buf[pos]; pos += 1
                value |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
        elif kind == 2:
            length = shift = 0
            while True:
                b = buf[pos]; pos += 1
                length |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            value = buf[pos:pos + length]; pos += length
        elif kind == 1:
            value = buf[pos:pos + 8]; pos += 8
        elif kind == 5:
            value = buf[pos:pos + 4]; pos += 4
        else:
            raise ValueError(f"unsupported wire type {kind}")
        yield number, value


def decode_manifest(raw: bytes) -> dict:
    out = {"inputs": [], "outputs": []}
    names = {1: "component", 2: "version", 4: "build_id", 7: "project", 8: "config_bundle_sha256"}
    for number, value in wire_fields(raw):
        if number in names:
            out[names[number]] = value.decode("utf-8")
        elif number == 3:
            out["abi_version"] = value
        elif number == 9:
            out["descriptor_set"] = value
        elif number in (5, 6):
            port = {n: v for n, v in wire_fields(value)}
            out["inputs" if number == 5 else "outputs"].append(
                {"name": port[1].decode(), "proto_type": port[2].decode()})
    return out


def read_manifest(lib) -> dict:
    lib.adasManifest.restype = ctypes.c_size_t
    lib.adasManifest.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    size = lib.adasManifest(None, 0)
    buf = ctypes.create_string_buffer(size)
    assert lib.adasManifest(buf, size) == size
    return decode_manifest(buf.raw[:size])


@pytest.fixture(scope="module")
def built():
    index_path = find_index()
    if index_path is None or not index_path.is_file():
        pytest.skip(f"no build tree: {LAB_ROOT / 'conan_workarea'}/build-sil-*/lab/{TOPOLOGY}/index.json is absent "
                    f"(run build.bat -P base -t sil -p <platform>, or set LAB_INDEX)")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index["components"]
    suffix = Path(entries["A"]["library"]).suffix
    if (suffix == ".dll") != (sys.platform == "win32"):
        pytest.skip(f"the build tree at {index_path} holds {suffix} libraries; this is {sys.platform}")
    libs = {name: ctypes.CDLL(entries[name]["library"]) for name in COMPONENTS}  # all five, one process
    return entries, libs


def test_lab17_index_lists_a_library_and_a_bundle_folder_per_component(built):
    entries, _ = built
    assert sorted(entries) == COMPONENTS
    for name, entry in entries.items():
        library, config = Path(entry["library"]), Path(entry["config"])
        assert library.is_file(), library
        assert config.parent == library.parent, "the bundle sits beside its library (R-SIM-3a)"
        for rel in ("default.yaml", "ports.yaml", "bundle.sha256", "vehicle/ego_params.yaml"):
            assert (config / rel).is_file(), config / rel
    assert len({Path(e["library"]).parent for e in entries.values()}) == 5, "one folder per library"


def test_lab13_every_library_answers_abi_version(built):
    _, libs = built
    for name, lib in libs.items():
        lib.adasAbiVersion.restype = ctypes.c_int32
        assert lib.adasAbiVersion() == 1, name


def test_lab13_every_manifest_declares_control_data_ref_sync_and_comp_state_in_declared_order(built):
    entries, libs = built
    for name, lib in libs.items():
        manifest = read_manifest(lib)
        assert manifest["component"] == name
        assert manifest["abi_version"] == 1
        assert manifest["build_id"] == entries[name]["build_id"]
        assert manifest["project"], "the project the library was built for"
        assert manifest["version"]
        assert [p["name"] for p in manifest["inputs"]] == ["control"] + DECLARED[name]
        assert [p["name"] for p in manifest["outputs"]] == ["out", "refSync", "compState"]
        types = {p["name"]: p["proto_type"] for p in manifest["inputs"] + manifest["outputs"]}
        assert types["control"] == "adas.common.CycleControl"
        assert types["refSync"] == "adas.common.RefSync"
        assert types["compState"] == "adas.df.CompState"
        assert types["out"] == "adas.lab.LabSample"
        assert manifest["descriptor_set"], "the schemas travel in the manifest (R-SIM-11)"


def test_lab13_manifest_carries_the_hash_of_the_bundle_beside_the_library(built):
    entries, libs = built
    for name, lib in libs.items():
        sha = (Path(entries[name]["config"]) / "bundle.sha256").read_text(encoding="utf-8").strip()
        assert read_manifest(lib)["config_bundle_sha256"] == sha, name


def test_lab13_init_resolves_the_bundle_beside_the_library_and_refuses_a_missing_one(built, tmp_path):
    _, libs = built
    for name, lib in libs.items():
        lib.adasInit.restype = ctypes.c_void_p
        lib.adasInit.argtypes = [ctypes.c_char_p]
        lib.adasShutdown.argtypes = [ctypes.c_void_p]
        handle = lib.adasInit(None)
        assert handle, f"{name}: adasInit(NULL) found no bundle beside the library"
        lib.adasShutdown(handle)
        assert not lib.adasInit(str(tmp_path / "no_such_bundle").encode()), f"{name}: a missing bundle must give NULL"
