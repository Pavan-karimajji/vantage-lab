# Copyright (c) LT EPS. All Rights Reserved.
# Proprietary and Confidential.
# COMPONENT: VANTAGE_LAB
# @file tests/test_car_counter_wrap.py
# @brief LAB-64..LAB-67: wrap_counter, the counter_start verb and the counter_wrap scenario -- SYT-HDR-04 .c2.
# @author Pavan Karimajji <Pavan.Karimajji@larsentoubro.com>
"""Tests of the ``measurement_counter`` wrap of the fake car (design sections 5.3 to 5.5, 6.2; ``D24``).

Serves ``RCT:SYS-0004``. ``SYT-HDR-04`` ``.c2``: on the ``counter_wrap`` scenario the sample after ``0xFFFFFFFF``
carries 1 (FAIL: a 0 there, or anything but 1). The check is a plain function that is also run on a planted
violation, and the whole test is run with ``wrap_counter`` made to return 0 after ``0xFFFFFFFF``. The scenario
tests are skipped, with the reason, when the build tree is absent or holds libraries of another operating system.
"""
import dataclasses
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "src"))
sys.path.insert(0, str(LAB_ROOT / "tests"))

import lab_tools.car as car_module  # noqa: E402
from lab_tools.car import Car, CarError, parse_car_script, wrap_counter  # noqa: E402
from lab_tools.reference import ReferenceResim  # noqa: E402
from test_car_running_example import index_path, read_recording, stream, topology  # noqa: E402,F401

TOP = 0xFFFFFFFF
START = 0xFFFFFFFB  # the counter_wrap scenario's first X sample


def scenario_of(topo, name):
    return next(s for s in topo.scenarios if s.name == name)


def record(topo, scenario, index, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"{topo.name}_{scenario.name}") / f"{scenario.name}.mcap"
    with Car(topo, scenario, index) as car:
        car.record(out)
    return read_recording(out), out


def wrap_defects(headers):
    """Defects in one source's stream across a wrap: a step other than +1, where ``0xFFFFFFFF`` is followed by 1,
    or a counter of 0 anywhere (SYT-HDR-04 .c2)."""
    bad = []
    for i, h in enumerate(headers):
        if h.measurement_counter == 0:
            bad.append((i, "zero counter"))
        if i > 0:
            previous = headers[i - 1].measurement_counter
            expected = 1 if previous == TOP else previous + 1
            if h.measurement_counter != expected:
                bad.append((i, "counter step"))
    return bad


class Header:
    def __init__(self, counter):
        self.measurement_counter = counter


# ---- LAB-64: wrap_counter ------------------------------------------------------------------------------------------
def test_lab64_wrap_counter_runs_one_to_the_top_and_then_one_again():
    assert [wrap_counter(n) for n in (0xFFFFFFFE, TOP, 0x100000000)] == [0xFFFFFFFE, TOP, 1]
    assert wrap_counter(1) == 1 and wrap_counter(0x100000001) == 2
    assert all(wrap_counter(n) != 0 for n in (*range(0, 5), *range(TOP - 3, TOP + 6), 2 * TOP, 2 * TOP + 1))


def test_lab64_a_clock_started_control_goes_through_wrap_counter(index_path, topology, tmp_path_factory, monkeypatch):
    """With ``wrap_counter`` shifted so tick 0 is the 5th counter below the top, the clock-started ``A`` has controls
    0xFFFFFFFB .. 0xFFFFFFFF, 1, 2 ...: ``Car.run`` numbers the control ``wrap_counter(k + 1)``."""
    real = car_module.wrap_counter
    monkeypatch.setattr(car_module, "wrap_counter", lambda n: real(n + START - 1))
    messages, _ = record(topology, scenario_of(topology, "base"), index_path, tmp_path_factory)
    counters = [m.sig_header.measurement_counter for m in stream(messages, "A.control")]
    assert counters[:8] == [0xFFFFFFFB, 0xFFFFFFFC, 0xFFFFFFFD, 0xFFFFFFFE, TOP, 1, 2, 3]


# ---- LAB-65: the counter_start verb --------------------------------------------------------------------------------
def test_lab65_counter_start_refuses_what_it_cannot_do(topology):
    """An unknown source, a value outside 1 .. 0xFFFFFFFF, a second entry for one source and a malformed entry are each refused, naming the scenario."""
    base = scenario_of(topology, "base")

    def refused(match, **blocks):
        with pytest.raises(CarError, match=match):
            parse_car_script(topology, dataclasses.replace(base, **blocks))

    refused("scenario base: car.counter_start names 'Q'", car={"counter_start": [{"source": "Q", "first": 5}]})
    refused("scenario base: car.counter_start.first must be an integer of at least 1",
            car={"counter_start": [{"source": "X", "first": 0}]})
    refused("scenario base: car.counter_start.first must be an integer from 1 to 4294967295",
            car={"counter_start": [{"source": "X", "first": TOP + 1}]})
    refused("scenario base: car.counter_start names source 'X' twice",
            car={"counter_start": [{"source": "X", "first": 5}, {"source": "X", "first": 9}]})
    refused("must have exactly source, first", car={"counter_start": [{"source": "X"}]})
    assert parse_car_script(topology, dataclasses.replace(base, car={"counter_start": [{"source": "X", "first": TOP}]})
                            ).counter_start == {"X": TOP}


# ---- LAB-66, LAB-67: the counter_wrap scenario, SYT-HDR-04 .c2 -------------------------------------------------------
def test_lab66_the_scenario_is_in_the_catalogue(topology):
    scenario = scenario_of(topology, "counter_wrap")
    assert scenario.car == {"counter_start": [{"source": "X", "first": START}]} and scenario.expect == "exact"
    assert [c["name"] for c in scenario.resim] == ["all_simulated"]


def test_syt_hdr_04_c2_the_sample_after_the_top_carries_one(index_path, topology, tmp_path_factory, capsys):
    """LAB-67, ``SYT-HDR-04`` ``.c2`` on ``counter_wrap``: X goes 0xFFFFFFFB .. 0xFFFFFFFF, 1, 2 ...; its
    ``cycle_counter`` equals the counter; the other sources are as in ``base``; the re-run by refSync is equal."""
    base, _ = record(topology, scenario_of(topology, "base"), index_path, tmp_path_factory)
    messages, path = record(topology, scenario_of(topology, "counter_wrap"), index_path, tmp_path_factory)
    headers = [m.sig_header for m in stream(messages, "source.X")]
    counters = [h.measurement_counter for h in headers]
    assert counters[:8] == [START, 0xFFFFFFFC, 0xFFFFFFFD, 0xFFFFFFFE, TOP, 1, 2, 3]
    assert wrap_defects(headers) == []
    assert [h.cycle_counter for h in headers] == counters
    assert [h.source_timestamp_us for h in headers] == \
        [m.sig_header.source_timestamp_us for m in stream(base, "source.X")]
    for source in ("source.Y", "source.Z"):
        assert [m.sig_header.measurement_counter for m in stream(messages, source)] == \
            [m.sig_header.measurement_counter for m in stream(base, source)]
    result = ReferenceResim(topology, path, index_path).run()
    assert result.equal, "\n".join(result.describe())
    with capsys.disabled():
        print(f"\ncounter_wrap: X goes {counters[3]:#x}, {counters[4]:#x}, {counters[5]}; re-run by refSync equal outputs")


def test_syt_hdr_04_c2_check_reports_a_zero_after_the_top():
    """The check on a planted violation: a 0 after 0xFFFFFFFF, and any value but 1 after it."""
    assert wrap_defects([Header(c) for c in (TOP - 1, TOP, 1, 2)]) == []
    assert wrap_defects([Header(c) for c in (TOP - 1, TOP, 0, 1)]) == [(2, "zero counter"), (2, "counter step")]
    assert wrap_defects([Header(c) for c in (TOP, 2)]) == [(1, "counter step")]


def test_syt_hdr_04_c2_would_fail_if_wrap_counter_returned_zero_after_the_top(index_path, topology, tmp_path_factory,
                                                                             monkeypatch):
    """Planted: ``wrap_counter`` keeps 2**32 as 0. The recording the car then writes is reported by the check."""
    monkeypatch.setattr(car_module, "wrap_counter", lambda n: n & TOP if n > TOP else n)
    messages, _ = record(topology, scenario_of(topology, "counter_wrap"), index_path, tmp_path_factory)
    defects = wrap_defects([m.sig_header for m in stream(messages, "source.X")])
    assert (5, "zero counter") in defects
