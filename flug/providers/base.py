"""Provider-neutral offer model and shared HTTP helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A search failed (after retries). The run continues with the other searches."""


class ProviderAuthError(ProviderError):
    """Credentials missing/invalid – retrying won't help."""


@dataclass
class Segment:
    origin: str
    destination: str
    departing_at: str  # local time ISO string as given by the provider
    arriving_at: str
    carrier: str  # marketing carrier name
    carrier_code: str | None
    flight_number: str | None
    operating_carrier: str | None = None
    duration_minutes: int | None = None
    slice_index: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Offer:
    provider: str
    provider_offer_id: str | None
    origin: str
    destination: str
    departure_date: dt.date
    return_date: dt.date | None
    total_amount: Decimal
    currency: str
    airlines: list[str]
    stops: int  # max stops on any slice
    duration_minutes: int | None  # total flying+layover time over all slices
    segments: list[Segment]
    # One entry per slice: {"slice": 0, "checked": 1, "carry_on": 1} – None means "unknown".
    baggage: list[dict] | None = None
    booking_url: str | None = None
    # "offer": link opens this exact offer; "search": link opens a search for the same flights/dates.
    booking_url_kind: str | None = None
    expires_at: dt.datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Identity of the itinerary (not the price) – same flights = same fingerprint."""
        parts = [self.provider, self.origin, self.destination, str(self.departure_date), str(self.return_date)]
        parts += [f"{s.carrier_code or s.carrier}{s.flight_number}@{s.departing_at}" for s in self.segments]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class SearchQuery:
    origin: str
    destination: str
    departure_date: dt.date
    return_date: dt.date | None
    adults: int = 1
    cabin_class: str = "economy"
    max_connections: int | None = None
    currency: str = "EUR"


class FlightProvider(Protocol):
    name: str

    def search(self, q: SearchQuery) -> list[Offer]: ...


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    backoff: float = 2.0,
    sleep=time.sleep,
    **kwargs,
) -> httpx.Response:
    """HTTP call with retries on timeouts, 429 and 5xx. Honors Retry-After."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            wait = backoff * (2**i)
            log.warning("%s %s failed (%s), retry in %.0fs", method, url, exc, wait)
        else:
            if resp.status_code in (401, 403):
                raise ProviderAuthError(f"{resp.status_code} from {url}: {_snippet(resp)}")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = ProviderError(f"{resp.status_code} from {url}: {_snippet(resp)}")
                wait = _retry_after(resp) or backoff * (2**i)
                log.warning("%s %s -> %s, retry in %.0fs", method, url, resp.status_code, wait)
            elif resp.status_code >= 400:
                raise ProviderError(f"{resp.status_code} from {url}: {_snippet(resp)}")
            else:
                return resp
        if i < attempts - 1:
            sleep(min(wait, 60))
    raise ProviderError(f"giving up after {attempts} attempts: {last_exc}")


def _retry_after(resp: httpx.Response) -> float | None:
    for header in ("retry-after", "ratelimit-reset"):
        val = resp.headers.get(header)
        if val:
            try:
                return max(float(val), 0.0)
            except ValueError:
                return None
    return None


def _snippet(resp: httpx.Response, n: int = 300) -> str:
    return resp.text[:n].replace("\n", " ")


def iso_duration_minutes(value: str | None) -> int | None:
    """Parse ISO 8601 durations like 'PT2H30M' or 'P1DT2H' into minutes."""
    if not value or not value.startswith("P"):
        return None
    days = hours = minutes = 0
    num = ""
    in_time = False
    for ch in value[1:]:
        if ch == "T":
            in_time = True
        elif ch.isdigit():
            num += ch
        elif ch == "D":
            days, num = int(num or 0), ""
        elif ch == "H" and in_time:
            hours, num = int(num or 0), ""
        elif ch == "M" and in_time:
            minutes, num = int(num or 0), ""
        elif ch == "S" and in_time:
            num = ""
        else:
            return None
    return days * 1440 + hours * 60 + minutes


def google_flights_search_url(q: SearchQuery) -> str:
    """Public Google Flights search for the same route/dates – a fallback booking entry point."""
    text = f"Flights from {q.origin} to {q.destination} on {q.departure_date.isoformat()}"
    if q.return_date:
        text += f" returning {q.return_date.isoformat()}"
    if q.cabin_class and q.cabin_class != "economy":
        text += f" {q.cabin_class.replace('_', ' ')}"
    if q.adults > 1:
        text += f" {q.adults} adults"
    return str(httpx.URL("https://www.google.com/travel/flights", params={"q": text, "curr": q.currency}))
