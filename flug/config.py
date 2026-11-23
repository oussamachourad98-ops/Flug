"""Runtime settings (environment variables) and watch definitions (YAML)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IATA_LEN = 3


class Settings(BaseSettings):
    """All secrets and tunables come from env vars (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///data/flug.db"
    watches_file: Path = Path("watches.yaml")

    # Providers. "duffel", "serpapi" or "duffel,serpapi" (both are queried and merged).
    providers: str = "duffel"
    duffel_access_token: str = ""
    duffel_api_url: str = "https://api.duffel.com"
    duffel_monthly_search_limit: int = 1500  # Duffel bills excess searches beyond a 1500:1 ratio
    duffel_max_offers_per_search: int = 20
    serpapi_api_key: str = ""
    serpapi_api_url: str = "https://serpapi.com"
    serpapi_monthly_search_limit: int = 250
    serpapi_gl: str = "de"
    serpapi_hl: str = "de"

    # Scheduler
    search_interval_minutes: int = Field(30, ge=5)
    run_on_start: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_url: str = "https://api.telegram.org"

    # Alerting
    alert_min_drop: Decimal = Decimal("1.00")  # re-alert same offer only if it got at least this much cheaper

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    dashboard_allow_no_auth: bool = False

    http_timeout_seconds: float = 60.0
    log_level: str = "INFO"

    @property
    def provider_names(self) -> list[str]:
        return [p.strip().lower() for p in self.providers.split(",") if p.strip()]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class DateRange(BaseModel):
    start: dt.date = Field(alias="from")
    end: dt.date = Field(alias="to")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _order(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date range 'to' must not be before 'from'")
        return self

    def days(self) -> list[dt.date]:
        n = (self.end - self.start).days
        return [self.start + dt.timedelta(days=i) for i in range(n + 1)]


class StayLength(BaseModel):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def _order(self) -> "StayLength":
        if self.max < self.min:
            raise ValueError("stay_nights.max must be >= stay_nights.min")
        return self


class WatchConfig(BaseModel):
    """One monitored route group: N origins x M destinations x flexible dates."""

    name: str
    origins: list[str]
    destinations: list[str]
    departure: DateRange | list[dt.date]
    # For round trips give either `return` (date range / list) or `stay_nights`. Neither => one way.
    return_dates: DateRange | list[dt.date] | None = Field(None, alias="return")
    stay_nights: StayLength | None = None
    target_price: Decimal | None = None
    currency: str = "EUR"
    adults: int = Field(1, ge=1, le=9)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = "economy"
    max_connections: int | None = Field(None, ge=0, le=2)
    max_date_combinations: int = Field(15, ge=1, le=200)
    active: bool = True

    model_config = {"populate_by_name": True}

    @field_validator("origins", "destinations")
    @classmethod
    def _iata_codes(cls, v: list[str]) -> list[str]:
        out = []
        for code in v:
            code = code.strip().upper()
            if len(code) != IATA_LEN or not code.isalpha():
                raise ValueError(
                    f"'{code}' is not a 3-letter IATA airport/city code. "
                    "Use `flug places <name>` to look codes up."
                )
            out.append(code)
        if not out:
            raise ValueError("at least one code required")
        return list(dict.fromkeys(out))

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3:
            raise ValueError("currency must be an ISO 4217 code like EUR")
        return v

    @model_validator(mode="after")
    def _trip(self) -> "WatchConfig":
        if self.return_dates is not None and self.stay_nights is not None:
            raise ValueError("use either 'return' or 'stay_nights', not both")
        return self

    @property
    def one_way(self) -> bool:
        return self.return_dates is None and self.stay_nights is None


def _as_dates(v: DateRange | list[dt.date]) -> list[dt.date]:
    return v.days() if isinstance(v, DateRange) else sorted(set(v))


def date_combinations(w: WatchConfig, today: dt.date | None = None) -> list[tuple[dt.date, dt.date | None]]:
    """Expand flexible dates into (departure, return) pairs, skipping the past, capped."""
    today = today or dt.date.today()
    deps = [d for d in _as_dates(w.departure) if d >= today]
    combos: list[tuple[dt.date, dt.date | None]] = []
    if w.one_way:
        combos = [(d, None) for d in deps]
    elif w.stay_nights is not None:
        for d in deps:
            for n in range(w.stay_nights.min, w.stay_nights.max + 1):
                combos.append((d, d + dt.timedelta(days=n)))
    else:
        rets = _as_dates(w.return_dates)  # type: ignore[arg-type]
        combos = [(d, r) for d in deps for r in rets if r >= d]
    return combos[: w.max_date_combinations]


class WatchFile(BaseModel):
    watches: list[WatchConfig]

    @model_validator(mode="after")
    def _unique(self) -> "WatchFile":
        names = [w.name for w in self.watches]
        if len(names) != len(set(names)):
            raise ValueError("watch names must be unique")
        return self


def load_watches(path: Path) -> list[WatchConfig]:
    if not path.exists():
        raise FileNotFoundError(f"Watch file {path} not found. Copy watches.example.yaml to {path}.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WatchFile.model_validate(raw).watches


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
