"""Duffel Flights API (v2) – live airline offers (NDC + GDS) with baggage and fare data.

Docs: https://duffel.com/docs/api/v2/offer-requests and /docs/api/v2/offers
Flow per search:
  1. POST /air/offer_requests?return_offers=false   -> offer request id
  2. GET  /air/offers?offer_request_id=..&sort=total_amount&limit=N  -> cheapest N offers
Test tokens (duffel_test_...) only return the fictional "Duffel Airways" – use a live token
(duffel_live_...) for real prices.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx

from .base import (
    Offer,
    ProviderAuthError,
    ProviderError,
    SearchQuery,
    Segment,
    google_flights_search_url,
    iso_duration_minutes,
    request_with_retries,
)

log = logging.getLogger(__name__)

DUFFEL_VERSION = "v2"


class DuffelProvider:
    name = "duffel"

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = "https://api.duffel.com",
        max_offers: int = 20,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        sleep=None,
    ) -> None:
        if not access_token:
            raise ProviderAuthError("DUFFEL_ACCESS_TOKEN is not set")
        self.is_test_mode = access_token.startswith("duffel_test_")
        if self.is_test_mode:
            log.warning("Duffel TEST token in use – offers are fictional (Duffel Airways), not real prices")
        self.max_offers = max_offers
        self._sleep_kw = {"sleep": sleep} if sleep else {}
        self.client = client or httpx.Client(timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Duffel-Version": DUFFEL_VERSION,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ search
    def search(self, q: SearchQuery) -> list[Offer]:
        slices = [{"origin": q.origin, "destination": q.destination, "departure_date": q.departure_date.isoformat()}]
        if q.return_date:
            slices.append(
                {"origin": q.destination, "destination": q.origin, "departure_date": q.return_date.isoformat()}
            )
        body: dict = {
            "data": {
                "slices": slices,
                "passengers": [{"type": "adult"} for _ in range(q.adults)],
                "cabin_class": q.cabin_class,
            }
        }
        if q.max_connections is not None:
            body["data"]["max_connections"] = q.max_connections

        resp = request_with_retries(
            self.client,
            "POST",
            f"{self.base_url}/air/offer_requests",
            params={"return_offers": "false", "supplier_timeout": "20000"},
            json=body,
            headers=self.headers,
            **self._sleep_kw,
        )
        request_id = resp.json()["data"]["id"]

        params = {"offer_request_id": request_id, "sort": "total_amount", "limit": str(self.max_offers)}
        if q.max_connections is not None:
            params["max_connections"] = str(q.max_connections)
        resp = request_with_retries(
            self.client, "GET", f"{self.base_url}/air/offers", params=params, headers=self.headers, **self._sleep_kw
        )
        raw_offers = resp.json().get("data", [])
        offers = []
        for raw in raw_offers:
            try:
                offers.append(self.parse_offer(raw, q))
            except (KeyError, TypeError, ValueError) as exc:  # one bad offer must not kill the search
                log.warning("Skipping unparsable Duffel offer %s: %s", raw.get("id"), exc)
        return offers

    # ------------------------------------------------------------------ places
    def place_suggestions(self, query: str) -> list[dict]:
        resp = request_with_retries(
            self.client,
            "GET",
            f"{self.base_url}/places/suggestions",
            params={"query": query},
            headers=self.headers,
            **self._sleep_kw,
        )
        return resp.json().get("data", [])

    # ------------------------------------------------------------------ parsing
    def parse_offer(self, raw: dict, q: SearchQuery) -> Offer:
        segments: list[Segment] = []
        baggage: list[dict] = []
        baggage_known = True
        stops = 0
        total_minutes = 0
        duration_known = True
        airlines: list[str] = []

        for si, sl in enumerate(raw["slices"]):
            segs = sl["segments"]
            stops = max(stops, len(segs) - 1)
            dur = iso_duration_minutes(sl.get("duration"))
            if dur is None:
                duration_known = False
            else:
                total_minutes += dur
            checked_min: int | None = None
            carry_min: int | None = None
            for seg in segs:
                mc = seg.get("marketing_carrier") or {}
                oc = seg.get("operating_carrier") or {}
                name = mc.get("name") or oc.get("name") or "?"
                if name not in airlines:
                    airlines.append(name)
                segments.append(
                    Segment(
                        origin=seg["origin"]["iata_code"],
                        destination=seg["destination"]["iata_code"],
                        departing_at=seg["departing_at"],
                        arriving_at=seg["arriving_at"],
                        carrier=name,
                        carrier_code=mc.get("iata_code"),
                        flight_number=seg.get("marketing_carrier_flight_number"),
                        operating_carrier=oc.get("name"),
                        duration_minutes=iso_duration_minutes(seg.get("duration")),
                        slice_index=si,
                    )
                )
                # Baggage of the first passenger (all passengers share a fare here).
                pax = (seg.get("passengers") or [None])[0]
                if not pax or "baggages" not in pax:
                    baggage_known = False
                    continue
                checked = sum(b.get("quantity", 0) for b in pax["baggages"] if b.get("type") == "checked")
                carry = sum(b.get("quantity", 0) for b in pax["baggages"] if b.get("type") == "carry_on")
                checked_min = checked if checked_min is None else min(checked_min, checked)
                carry_min = carry if carry_min is None else min(carry_min, carry)
            baggage.append({"slice": si, "checked": checked_min, "carry_on": carry_min})

        owner = (raw.get("owner") or {}).get("name")
        if owner and owner not in airlines:
            airlines.insert(0, owner)

        expires = raw.get("expires_at")
        return Offer(
            provider=self.name,
            provider_offer_id=raw["id"],
            origin=q.origin,
            destination=q.destination,
            departure_date=q.departure_date,
            return_date=q.return_date,
            total_amount=Decimal(raw["total_amount"]),
            currency=raw["total_currency"],
            airlines=airlines,
            stops=stops,
            duration_minutes=total_minutes if duration_known else None,
            segments=segments,
            baggage=baggage if baggage_known else None,
            # Duffel has no public per-offer consumer URL; the offer is bookable through the API
            # (POST /air/orders). We link to an equivalent Google Flights search instead.
            booking_url=google_flights_search_url(q),
            booking_url_kind="search",
            expires_at=_parse_dt(expires),
            extra={
                "live_mode": raw.get("live_mode"),
                "conditions": raw.get("conditions"),
                "total_emissions_kg": raw.get("total_emissions_kg"),
            },
        )


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


__all__ = ["DuffelProvider", "ProviderError"]
