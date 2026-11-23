"""SerpApi Google Flights engine – live Google Flights results (optional second source).

Docs: https://serpapi.com/google-flights-api
Note: for round trips Google Flights prices the whole trip but lists outbound legs only
(return legs need a second call with departure_token). We store the outbound segments and
the round-trip price; the link is the Google Flights page for exactly this search.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

import httpx

from .base import Offer, ProviderAuthError, ProviderError, SearchQuery, Segment, request_with_retries

log = logging.getLogger(__name__)

TRAVEL_CLASS = {"economy": "1", "premium_economy": "2", "business": "3", "first": "4"}
# SerpApi `stops`: 0 any, 1 nonstop, 2 <=1 stop, 3 <=2 stops
STOPS = {0: "1", 1: "2", 2: "3"}
NO_RESULTS = re.compile(r"hasn't returned any results", re.I)


class SerpApiProvider:
    name = "serpapi"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://serpapi.com",
        gl: str = "de",
        hl: str = "de",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        sleep=None,
    ) -> None:
        if not api_key:
            raise ProviderAuthError("SERPAPI_API_KEY is not set")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.gl, self.hl = gl, hl
        self.client = client or httpx.Client(timeout=timeout)
        self._sleep_kw = {"sleep": sleep} if sleep else {}

    def search(self, q: SearchQuery) -> list[Offer]:
        params = {
            "engine": "google_flights",
            "departure_id": q.origin,
            "arrival_id": q.destination,
            "outbound_date": q.departure_date.isoformat(),
            "type": "1" if q.return_date else "2",
            "currency": q.currency,
            "gl": self.gl,
            "hl": self.hl,
            "adults": str(q.adults),
            "travel_class": TRAVEL_CLASS.get(q.cabin_class, "1"),
            "api_key": self.api_key,
        }
        if q.return_date:
            params["return_date"] = q.return_date.isoformat()
        if q.max_connections is not None:
            params["stops"] = STOPS[q.max_connections]

        resp = request_with_retries(self.client, "GET", f"{self.base_url}/search.json", params=params, **self._sleep_kw)
        data = resp.json()
        if err := data.get("error"):
            if NO_RESULTS.search(err):
                return []
            if "api key" in err.lower():
                raise ProviderAuthError(err)
            raise ProviderError(err)

        page_url = (data.get("search_metadata") or {}).get("google_flights_url")
        offers = []
        for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            try:
                offers.append(self.parse_item(item, q, page_url))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping unparsable SerpApi item: %s", exc)
        return offers

    def parse_item(self, item: dict, q: SearchQuery, page_url: str | None) -> Offer:
        if item.get("price") is None:
            raise ValueError("no price")
        segments = []
        airlines: list[str] = []
        bag_notes: list[str] = []
        for f in item["flights"]:
            airline = f.get("airline") or "?"
            if airline not in airlines:
                airlines.append(airline)
            fn = (f.get("flight_number") or "").split()
            segments.append(
                Segment(
                    origin=f["departure_airport"]["id"],
                    destination=f["arrival_airport"]["id"],
                    departing_at=f["departure_airport"].get("time", ""),
                    arriving_at=f["arrival_airport"].get("time", ""),
                    carrier=airline,
                    carrier_code=fn[0] if len(fn) == 2 else None,
                    flight_number=fn[-1] if fn else None,
                    duration_minutes=f.get("duration"),
                    slice_index=0,
                )
            )
            bag_notes += [e for e in f.get("extensions", []) if re.search(r"bag|gepäck|koffer", e, re.I)]

        baggage = [{"slice": 0, "notes": sorted(set(bag_notes))}] if bag_notes else None
        return Offer(
            provider=self.name,
            provider_offer_id=item.get("booking_token"),
            origin=q.origin,
            destination=q.destination,
            departure_date=q.departure_date,
            return_date=q.return_date,
            total_amount=Decimal(str(item["price"])),
            currency=q.currency,
            airlines=airlines,
            stops=len(item.get("layovers") or []),
            duration_minutes=item.get("total_duration"),
            segments=segments,
            baggage=baggage,
            booking_url=page_url,
            booking_url_kind="search" if page_url else None,
            extra={"outbound_only": bool(q.return_date), "type": item.get("type")},
        )
