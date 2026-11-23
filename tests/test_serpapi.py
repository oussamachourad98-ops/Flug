import datetime as dt
from decimal import Decimal

import httpx
import pytest
import respx

from flug.providers import ProviderAuthError, SearchQuery, SerpApiProvider

URL = "https://serpapi.com/search.json"
RESP = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?hl=de&tfs=abc"},
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"name": "Frankfurt", "id": "FRA", "time": "2027-04-10 07:00"},
                    "arrival_airport": {"name": "Lisbon", "id": "LIS", "time": "2027-04-10 09:10"},
                    "duration": 190,
                    "airline": "TAP Air Portugal",
                    "flight_number": "TP 577",
                    "extensions": ["Average legroom (29 in)", "Carry-on bag included"],
                }
            ],
            "total_duration": 190,
            "price": 211,
            "type": "Round trip",
            "booking_token": "tok",
        }
    ],
    "other_flights": [
        {
            "flights": [
                {"departure_airport": {"id": "FRA", "time": "2027-04-10 06:00"},
                 "arrival_airport": {"id": "MAD", "time": "2027-04-10 08:30"}, "duration": 150,
                 "airline": "Iberia", "flight_number": "IB 3501"},
                {"departure_airport": {"id": "MAD", "time": "2027-04-10 10:00"},
                 "arrival_airport": {"id": "LIS", "time": "2027-04-10 10:20"}, "duration": 80,
                 "airline": "Iberia", "flight_number": "IB 3104"},
            ],
            "layovers": [{"id": "MAD", "duration": 90}],
            "total_duration": 320,
            "price": 199,
        },
        {"flights": [], "price": None},
    ],
}
Q = SearchQuery("FRA", "LIS", dt.date(2027, 4, 10), dt.date(2027, 4, 17), max_connections=0)


@respx.mock
def test_parsing_and_params():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=RESP))
    offers = SerpApiProvider("k", sleep=lambda s: None).search(Q)
    p = route.calls.last.request.url.params
    assert p["engine"] == "google_flights" and p["type"] == "1" and p["return_date"] == "2027-04-17"
    assert p["stops"] == "1"  # nonstop
    assert len(offers) == 2
    a, b = offers
    assert a.total_amount == Decimal("211") and a.currency == "EUR"
    assert a.segments[0].carrier_code == "TP" and a.segments[0].flight_number == "577"
    assert a.baggage == [{"slice": 0, "notes": ["Carry-on bag included"]}]
    assert a.booking_url.startswith("https://www.google.com/travel/flights")
    assert a.extra["outbound_only"] is True
    assert b.stops == 1 and b.baggage is None and b.duration_minutes == 320


@respx.mock
def test_no_results_is_empty():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"error": "Google Flights hasn't returned any results for this query."}))
    assert SerpApiProvider("k").search(Q) == []


@respx.mock
def test_invalid_key():
    respx.get(URL).mock(return_value=httpx.Response(401, json={"error": "Invalid API key."}))
    with pytest.raises(ProviderAuthError):
        SerpApiProvider("k").search(Q)
