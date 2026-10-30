# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/harness/conftest.py
# @brief Puts src/ and this folder on the import path so the harness runs without installing the package.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
LAB_ROOT = HARNESS_DIR.parents[1]
for entry in (LAB_ROOT / "src", HARNESS_DIR):
    sys.path.insert(0, str(entry))
