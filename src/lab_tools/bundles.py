# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file src/lab_tools/bundles.py
# @brief LAB-34: bundle_variant -- a copy of a built config bundle with calibration keys set or removed and a new bundle.sha256.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Calibration variants of a built config bundle (VANTAGE_Lab_Design.md sections 6.1 and 6.3, ``F-LAB-CAL``).

``bundle_variant`` copies a bundle folder, sets or removes keys of its ``default.yaml`` and repacks it
with a new ``bundle.sha256``. The source bundle, which sits beside a built library, is never written.
The hash is the same algorithm the build uses (sorted POSIX-relative paths, UTF-8, ``\\n`` newlines,
``bundle.sha256`` excluded), written here again so the lab needs no sibling checkout.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Iterable, Mapping

DEFAULT_YAML = "default.yaml"
HASH_FILE = "bundle.sha256"
_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


class BundleError(RuntimeError):
    """A bundle variant cannot be made as asked."""


def bundle_sha256(bundle_dir: Path) -> str:
    """The hash of a bundle folder, ``bundle.sha256`` itself excluded."""
    files = sorted(p.relative_to(bundle_dir).as_posix() for p in Path(bundle_dir).rglob("*")
                   if p.is_file() and p.name != HASH_FILE)
    digest = hashlib.sha256()
    for rel in files:
        text = (Path(bundle_dir) / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        digest.update(rel.encode("utf-8") + b"\n")
        digest.update(text.encode("utf-8") + b"\n")
    return digest.hexdigest()


def bundle_variant(source: str | Path, destination: str | Path, set_values: Mapping[str, int | str] | None = None,
                   remove: Iterable[str] = ()) -> str:
    """Copy bundle ``source`` to the new folder ``destination``; ``set_values`` writes ``KEY: value`` lines of its
    ``default.yaml`` (a new key is appended), ``remove`` deletes them; repack ``bundle.sha256`` and return the hash.

    Refused with ``BundleError``: a ``destination`` that is, holds or lies inside ``source`` or already
    holds files; a key that is not ALL_CAPS; a key both set and removed; a key to remove that is absent."""
    source, destination = Path(source), Path(destination)
    set_keys = dict(set_values or {})
    remove_keys = list(remove)
    if not (source / DEFAULT_YAML).is_file():
        raise BundleError(f"{source}: not a config bundle (no {DEFAULT_YAML})")
    src, dst = source.resolve(), destination.resolve()
    if dst == src or src in dst.parents or dst in src.parents:
        raise BundleError(f"{destination}: must be a separate folder, not {source} or inside it")
    if destination.exists() and any(destination.iterdir()):
        raise BundleError(f"{destination}: already holds files")
    for key in list(set_keys) + remove_keys:
        if not _KEY.match(key):
            raise BundleError(f"{key!r}: a calibration key is ALL_CAPS (R-NAME-3)")
    both = sorted(set_keys.keys() & set(remove_keys))
    if both:
        raise BundleError(f"{', '.join(both)}: both set and removed")

    lines = (source / DEFAULT_YAML).read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    present = {line.split(":", 1)[0].strip() for line in lines if ":" in line and not line.lstrip().startswith("#")}
    missing = [k for k in remove_keys if k not in present]
    if missing:
        raise BundleError(f"{source / DEFAULT_YAML}: cannot remove {', '.join(missing)}: not present")
    kept: list[str] = []
    for line in lines:
        key = line.split(":", 1)[0].strip() if ":" in line and not line.lstrip().startswith("#") else None
        if key in remove_keys:
            continue
        if key in set_keys:
            line = f"{key}: {set_keys.pop(key)}"
        kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()
    kept.extend(f"{key}: {value}" for key, value in set_keys.items())

    shutil.copytree(source, destination, dirs_exist_ok=True)
    (destination / DEFAULT_YAML).write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
    digest = bundle_sha256(destination)
    (destination / HASH_FILE).write_text(digest + "\n", encoding="utf-8", newline="\n")
    return digest
