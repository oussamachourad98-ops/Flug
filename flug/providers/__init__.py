from __future__ import annotations

from ..config import Settings
from .base import FlightProvider, Offer, ProviderAuthError, ProviderError, SearchQuery, Segment
from .duffel import DuffelProvider
from .serpapi import SerpApiProvider

MONTHLY_LIMIT_ATTR = {"duffel": "duffel_monthly_search_limit", "serpapi": "serpapi_monthly_search_limit"}


def build_providers(s: Settings) -> list[FlightProvider]:
    providers: list[FlightProvider] = []
    for name in s.provider_names:
        if name == "duffel":
            providers.append(
                DuffelProvider(
                    s.duffel_access_token,
                    base_url=s.duffel_api_url,
                    max_offers=s.duffel_max_offers_per_search,
                    timeout=s.http_timeout_seconds,
                )
            )
        elif name == "serpapi":
            providers.append(
                SerpApiProvider(
                    s.serpapi_api_key,
                    base_url=s.serpapi_api_url,
                    gl=s.serpapi_gl,
                    hl=s.serpapi_hl,
                    timeout=s.http_timeout_seconds,
                )
            )
        else:
            raise ValueError(f"Unknown provider '{name}' (supported: duffel, serpapi)")
    if not providers:
        raise ValueError("PROVIDERS is empty")
    return providers


__all__ = [
    "FlightProvider",
    "Offer",
    "ProviderAuthError",
    "ProviderError",
    "SearchQuery",
    "Segment",
    "DuffelProvider",
    "SerpApiProvider",
    "build_providers",
    "MONTHLY_LIMIT_ATTR",
]
