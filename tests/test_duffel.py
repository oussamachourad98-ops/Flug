import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
import respx

from flug.providers import DuffelProvider, ProviderAuthError, ProviderError, SearchQuery

API = "https://api.duffel.com"

# Shape follows the Duffel v2 offer schema (trimmed).
OFFER = {
    "id": "off_0000AbC",
    "live_mode": True,
    "total_amount": "187.40",
    "total_currency": "EUR",
    "expires_at": "2027-01-01T12:30:00.000000Z",
    "owner": {"name": "TAP Air Portugal", "iata_code": "TP"},
    "conditions": {"refund_before_departure": {"allowed": False}},
    "slices": [
        {
            "duration": "PT5H10M",
            "segments": [
                {
                    "origin": {"iata_code": "FRA"},
                    "destination": {"iata_code": "OPO"},
                    "departing_at": "2027-04-10T07:00:00",
                    "arriving_at": "2027-04-10T09:00:00",
                    "duration": "PT3H",
                    "marketing_carrier": {"name": "TAP Air Portugal", "iata_code": "TP"},
                    "operating_carrier": {"name": "TAP Express", "iata_code": "NI"},
                    "marketing_carrier_flight_number": "571",
                    "passengers": [{"passenger_id": "pas_1", "baggages": [
                        {"type": "checked", "quantity": 1}, {"type": "carry_on", "quantity": 1}]}],
                },
                {
                    "origin": {"iata_code": "OPO"},
                    "destination": {"iata_code": "LIS"},
                    "departing_at": "2027-04-10T10:30:00",
                    "arriving_at": "2027-04-10T11:10:00",
                    "duration": "PT40M",
                    "marketing_carrier": {"name": "TAP Air Portugal", "iata_code": "TP"},
                    "operating_carrier": {"name": "TAP Air Portugal", "iata_code": "TP"},
                    "marketing_carrier_flight_number": "1941",
                    "passengers": [{"passenger_id": "pas_1", "baggages": [{"type": "carry_on", "quantity": 1}]}],
                },
            ],
        },
        {
            "duration": "PT3H",
            "segments": [
                {
                    "origin": {"iata_code": "LIS"},
                    "destination": {"iata_code": "FRA"},
                    "departing_at": "2027-04-17T12:00:00",
                    "arriving_at": "2027-04-17T16:00:00",
                    "duration": "PT3H",
                    "marketing_carrier": {"name": "Lufthansa", "iata_code": "LH"},
                    "marketing_carrier_flight_number": "1167",
                    "passengers": [{"passenger_id": "pas_1", "baggages": [
                        {"type": "checked", "quantity": 1}, {"type": "carry_on", "quantity": 1}]}],
                }
            ],
        },
    ],
}

Q = SearchQuery("FRA", "LIS", dt.date(2027, 4, 10), dt.date(2027, 4, 17), adults=2, max_connections=1)


def _provider(**kw):
    return DuffelProvider("duffel_live_abc", sleep=lambda s: None, **kw)


@respx.mock
def test_search_flow_and_parsing():
    create = respx.post(f"{API}/air/offer_requests").mock(
        return_value=httpx.Response(201, json={"data": {"id": "orq_1"}})
    )
    listing = respx.get(f"{API}/air/offers").mock(return_value=httpx.Response(200, json={"data": [OFFER]}))

    offers = _provider().search(Q)

    req = create.calls.last.request
    assert req.headers["Authorization"] == "Bearer duffel_live_abc"
    assert req.headers["Duffel-Version"] == "v2"
    assert req.url.params["return_offers"] == "false"
    body = json.loads(req.content)["data"]
    assert body["slices"] == [
        {"origin": "FRA", "destination": "LIS", "departure_date": "2027-04-10"},
        {"origin": "LIS", "destination": "FRA", "departure_date": "2027-04-17"},
    ]
    assert body["passengers"] == [{"type": "adult"}, {"type": "adult"}]
    assert body["max_connections"] == 1
    lp = listing.calls.last.request.url.params
    assert lp["offer_request_id"] == "orq_1" and lp["sort"] == "total_amount"

    (o,) = offers
    assert o.total_amount == Decimal("187.40") and o.currency == "EUR"
    assert o.airlines == ["TAP Air Portugal", "Lufthansa"]
    assert o.stops == 1
    assert o.duration_minutes == 310 + 180
    assert o.baggage == [{"slice": 0, "checked": 0, "carry_on": 1}, {"slice": 1, "checked": 1, "carry_on": 1}]
    assert [s.flight_number for s in o.segments] == ["571", "1941", "1167"]
    assert o.segments[0].operating_carrier == "TAP Express"
    assert o.expires_at == dt.datetime(2027, 1, 1, 12, 30)
    assert o.booking_url_kind == "search" and "google.com/travel/flights" in o.booking_url
    assert o.provider_offer_id == "off_0000AbC"


@respx.mock
def test_missing_baggage_is_unknown_not_zero():
    raw = json.loads(json.dumps(OFFER))
    del raw["slices"][1]["segments"][0]["passengers"]
    respx.post(f"{API}/air/offer_requests").mock(return_value=httpx.Response(201, json={"data": {"id": "orq"}}))
    respx.get(f"{API}/air/offers").mock(return_value=httpx.Response(200, json={"data": [raw]}))
    (o,) = _provider().search(Q)
    assert o.baggage is None


@respx.mock
def test_bad_offer_is_skipped():
    respx.post(f"{API}/air/offer_requests").mock(return_value=httpx.Response(201, json={"data": {"id": "orq"}}))
    respx.get(f"{API}/air/offers").mock(return_value=httpx.Response(200, json={"data": [{"id": "x"}, OFFER]}))
    assert len(_provider().search(Q)) == 1


@respx.mock
def test_retries_on_429_then_succeeds():
    route = respx.post(f"{API}/air/offer_requests")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "1"}, json={"errors": []}),
        httpx.Response(201, json={"data": {"id": "orq"}}),
    ]
    respx.get(f"{API}/air/offers").mock(return_value=httpx.Response(200, json={"data": []}))
    assert _provider().search(Q) == []
    assert route.call_count == 2


@respx.mock
def test_auth_error_not_retried():
    route = respx.post(f"{API}/air/offer_requests").mock(return_value=httpx.Response(401, json={"errors": []}))
    with pytest.raises(ProviderAuthError):
        _provider().search(Q)
    assert route.call_count == 1


@respx.mock
def test_validation_error_raises_provider_error():
    respx.post(f"{API}/air/offer_requests").mock(return_value=httpx.Response(422, json={"errors": [{"title": "x"}]}))
    with pytest.raises(ProviderError):
        _provider().search(Q)


@respx.mock
def test_gives_up_after_repeated_5xx():
    route = respx.post(f"{API}/air/offer_requests").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError):
        _provider().search(Q)
    assert route.call_count == 4


def test_missing_token():
    with pytest.raises(ProviderAuthError):
        DuffelProvider("")


def test_test_token_flagged():
    assert DuffelProvider("duffel_test_x").is_test_mode
