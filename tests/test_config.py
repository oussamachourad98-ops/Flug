import datetime as dt

import pytest
from pydantic import ValidationError

from flug.config import date_combinations, load_watches

from .conftest import TODAY, make_watch


def test_stay_nights_combinations():
    w = make_watch(departure={"from": "2027-04-10", "to": "2027-04-11"}, stay_nights={"min": 3, "max": 4})
    assert date_combinations(w, today=TODAY) == [
        (dt.date(2027, 4, 10), dt.date(2027, 4, 13)),
        (dt.date(2027, 4, 10), dt.date(2027, 4, 14)),
        (dt.date(2027, 4, 11), dt.date(2027, 4, 14)),
        (dt.date(2027, 4, 11), dt.date(2027, 4, 15)),
    ]


def test_return_range_skips_impossible_and_past():
    w = make_watch(
        departure=["2026-12-01", "2027-04-10", "2027-04-12"],
        stay_nights=None,
        **{"return": {"from": "2027-04-11", "to": "2027-04-12"}},
    )
    assert date_combinations(w, today=TODAY) == [
        (dt.date(2027, 4, 10), dt.date(2027, 4, 11)),
        (dt.date(2027, 4, 10), dt.date(2027, 4, 12)),
        (dt.date(2027, 4, 12), dt.date(2027, 4, 12)),
    ]


def test_one_way_and_cap():
    w = make_watch(departure={"from": "2027-04-01", "to": "2027-04-30"}, stay_nights=None, max_date_combinations=5)
    combos = date_combinations(w, today=TODAY)
    assert len(combos) == 5 and all(r is None for _, r in combos)


@pytest.mark.parametrize("bad", ["FR", "FRAX", "F1A", ""])
def test_invalid_iata(bad):
    with pytest.raises(ValidationError):
        make_watch(origins=[bad])


def test_codes_normalised_and_deduped():
    assert make_watch(origins=["fra", "FRA", " muc "]).origins == ["FRA", "MUC"]


def test_return_and_stay_are_exclusive():
    with pytest.raises(ValidationError):
        make_watch(**{"return": ["2027-04-20"]})


def test_example_file_is_valid():
    watches = load_watches(__import__("pathlib").Path(__file__).parent.parent / "watches.example.yaml")
    assert len(watches) == 3


def test_duplicate_names_rejected(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(
        "watches:\n"
        "  - {name: A, origins: [FRA], destinations: [LIS], departure: [2027-04-10]}\n"
        "  - {name: A, origins: [FRA], destinations: [OPO], departure: [2027-04-10]}\n"
    )
    with pytest.raises(ValidationError):
        load_watches(p)
