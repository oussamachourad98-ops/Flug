from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from flug.config import Settings, WatchConfig
from flug.db import make_engine, make_session_factory
from flug.providers import Offer, SearchQuery, Segment

TODAY = dt.date(2027, 1, 1)


@pytest.fixture
def session_factory(tmp_path):
    return make_session_factory(make_engine(f"sqlite:///{tmp_path}/t.db"))


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/t.db",
        dashboard_password="pw",
        duffel_access_token="duffel_live_x",
    )


def make_watch(**kw) -> WatchConfig:
    base = {
        "name": "Test",
        "origins": ["FRA", "MUC"],
        "destinations": ["LIS"],
        "departure": {"from": "2027-04-10", "to": "2027-04-10"},
        "stay_nights": {"min": 7, "max": 7},
        "target_price": 200,
        "currency": "EUR",
    }
    base.update(kw)
    return WatchConfig.model_validate(base)


def make_offer(q: SearchQuery, price: str, flight="100", provider="fake", currency="EUR") -> Offer:
    return Offer(
        provider=provider,
        provider_offer_id=f"off_{q.origin}_{flight}",
        origin=q.origin,
        destination=q.destination,
        departure_date=q.departure_date,
        return_date=q.return_date,
        total_amount=Decimal(price),
        currency=currency,
        airlines=["TAP Air Portugal"],
        stops=0,
        duration_minutes=180,
        segments=[
            Segment(q.origin, q.destination, f"{q.departure_date}T08:00:00", f"{q.departure_date}T11:00:00",
                    "TAP Air Portugal", "TP", flight)
        ],
        baggage=[{"slice": 0, "checked": 1, "carry_on": 1}],
        booking_url="https://example.test/book",
        booking_url_kind="search",
    )


class FakeProvider:
    """Prices per origin can be changed between runs."""

    name = "duffel"  # so the duffel budget limit applies

    def __init__(self, prices: dict[str, str], fail: set[str] | None = None):
        self.prices = prices
        self.fail = fail or set()
        self.calls: list[SearchQuery] = []

    def search(self, q):
        from flug.providers import ProviderError

        self.calls.append(q)
        if q.origin in self.fail:
            raise ProviderError("boom")
        p = self.prices.get(q.origin)
        return [make_offer(q, p)] if p else []
