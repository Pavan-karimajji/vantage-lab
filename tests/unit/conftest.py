# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/unit/conftest.py
# @brief Puts src/ on the import path so the unit tests run without installing the package.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAB_ROOT / "src"))
