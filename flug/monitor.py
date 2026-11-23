"""Run searches for watches, store price history and decide/send alerts."""

from __future__ import annotations

import datetime as dt
import html
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, WatchConfig, date_combinations
from .db import Alert, OfferSnapshot, ProviderUsage, SearchRun, Watch, utcnow
from .providers import MONTHLY_LIMIT_ATTR, FlightProvider, Offer, ProviderAuthError, ProviderError, SearchQuery

log = logging.getLogger(__name__)

REASON_BELOW_TARGET = "below_target"
REASON_NEW_LOW = "new_low"


# --------------------------------------------------------------------------- watch sync
def sync_watches(session: Session, configs: list[WatchConfig]) -> list[Watch]:
    """Upsert YAML watches into the DB. Watches removed from YAML are deactivated (history kept)."""
    existing = {w.name: w for w in session.scalars(select(Watch))}
    seen = set()
    for cfg in configs:
        seen.add(cfg.name)
        w = existing.get(cfg.name)
        cfg_json = cfg.model_dump(mode="json", by_alias=True)
        if w is None:
            w = Watch(name=cfg.name, target_price=cfg.target_price, yaml_target_price=cfg.target_price)
            session.add(w)
        elif w.yaml_target_price != cfg.target_price:
            # YAML target changed -> it wins over a dashboard edit.
            w.target_price = cfg.target_price
            w.yaml_target_price = cfg.target_price
        w.config = cfg_json
        w.currency = cfg.currency
        w.active = cfg.active
    for name, w in existing.items():
        if name not in seen:
            w.active = False
    session.commit()
    return list(session.scalars(select(Watch).where(Watch.active.is_(True)).order_by(Watch.id)))


# --------------------------------------------------------------------------- budget
def _month() -> str:
    return utcnow().strftime("%Y-%m")


def usage_this_month(session: Session, provider: str) -> int:
    row = session.scalar(select(ProviderUsage).where(ProviderUsage.provider == provider, ProviderUsage.month == _month()))
    return row.searches if row else 0


def _count_usage(session: Session, provider: str) -> None:
    row = session.scalar(select(ProviderUsage).where(ProviderUsage.provider == provider, ProviderUsage.month == _month()))
    if row is None:
        row = ProviderUsage(provider=provider, month=_month(), searches=0)
        session.add(row)
    row.searches += 1


# --------------------------------------------------------------------------- formatting
def fmt_money(amount: Decimal, currency: str) -> str:
    return f"{amount:,.2f} {currency}".replace(",", " ")


def fmt_duration(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    return f"{minutes // 60}h{minutes % 60:02d}"


def fmt_baggage(baggage: list | None) -> str:
    if not baggage:
        return "Gepäck: keine Angabe"
    parts = []
    for b in baggage:
        if "notes" in b:
            parts.append("; ".join(b["notes"]))
            continue
        label = "Hin" if b["slice"] == 0 else "Rück"
        checked = "?" if b.get("checked") is None else b["checked"]
        carry = "?" if b.get("carry_on") is None else b["carry_on"]
        parts.append(f"{label}: {checked}× Aufgabe, {carry}× Hand")
    return "Gepäck: " + " | ".join(parts)


def format_alert(watch: Watch, offer: Offer, reasons: list[str], previous_low: Decimal | None) -> str:
    e = html.escape
    lines = []
    if REASON_BELOW_TARGET in reasons:
        lines.append(f"✈️ <b>Unter Zielpreis</b> ({fmt_money(watch.target_price, watch.currency)})")
    if REASON_NEW_LOW in reasons:
        prev = f" (vorher {fmt_money(previous_low, offer.currency)})" if previous_low is not None else ""
        lines.append(f"📉 <b>Neuer Tiefstpreis</b>{prev}")
    trip = f"{offer.departure_date:%d.%m.%Y}"
    if offer.return_date:
        trip += f" – {offer.return_date:%d.%m.%Y}"
    lines += [
        f"<b>{e(watch.name)}</b>: {e(offer.origin)} → {e(offer.destination)}, {trip}",
        f"💶 <b>{fmt_money(offer.total_amount, offer.currency)}</b> gesamt",
        f"🛫 {e(', '.join(offer.airlines))} · {offer.stops} Stopp(s) · {fmt_duration(offer.duration_minutes)}",
    ]
    for s in offer.segments:
        fn = f"{s.carrier_code or ''}{s.flight_number or ''}"
        lines.append(f"   • {e(s.origin)}→{e(s.destination)} {e(s.departing_at[:16].replace('T', ' '))} {e(fn)}")
    lines.append(e(fmt_baggage(offer.baggage)))
    lines.append(f"Quelle: {e(offer.provider)}" + (" (nur Hinflug-Details)" if offer.extra.get("outbound_only") else ""))
    if offer.booking_url:
        label = "Angebot buchen" if offer.booking_url_kind == "offer" else "Zur Buchungssuche"
        lines.append(f'<a href="{e(offer.booking_url, quote=True)}">{label}</a>')
    return "\n".join(lines)


# --------------------------------------------------------------------------- run
@dataclass
class RunResult:
    watch: str
    status: str
    searches: int = 0
    offers: int = 0
    cheapest: Offer | None = None
    alerts_sent: int = 0
    errors: list[str] = field(default_factory=list)


def _snapshot(watch: Watch, run: SearchRun, o: Offer) -> OfferSnapshot:
    return OfferSnapshot(
        watch_id=watch.id,
        run_id=run.id,
        provider=o.provider,
        provider_offer_id=(o.provider_offer_id or "")[:200] or None,
        fingerprint=o.fingerprint,
        origin=o.origin,
        destination=o.destination,
        departure_date=o.departure_date,
        return_date=o.return_date,
        total_amount=o.total_amount,
        currency=o.currency,
        airlines=", ".join(o.airlines)[:300],
        stops=o.stops,
        duration_minutes=o.duration_minutes,
        baggage=o.baggage,
        segments=[s.to_dict() for s in o.segments],
        booking_url=o.booking_url,
        booking_url_kind=o.booking_url_kind,
        expires_at=o.expires_at,
    )


def previous_low(session: Session, watch: Watch, currency: str, before_run: int) -> Decimal | None:
    return session.scalar(
        select(func.min(OfferSnapshot.total_amount)).where(
            OfferSnapshot.watch_id == watch.id,
            OfferSnapshot.currency == currency,
            OfferSnapshot.run_id < before_run,
        )
    )


def already_alerted(session: Session, watch: Watch, offer: Offer, min_drop: Decimal) -> bool:
    """Same itinerary already alerted at a price that is not meaningfully higher than now."""
    best = session.scalar(
        select(func.min(Alert.total_amount)).where(
            Alert.watch_id == watch.id, Alert.fingerprint == offer.fingerprint, Alert.delivered.is_(True)
        )
    )
    return best is not None and offer.total_amount > best - min_drop


def run_watch(
    session: Session,
    watch: Watch,
    providers: list[FlightProvider],
    settings: Settings,
    notify: Callable[[str], None] | None,
    today: dt.date | None = None,
) -> RunResult:
    cfg = WatchConfig.model_validate(watch.config)
    combos = date_combinations(cfg, today=today)
    result = RunResult(watch=watch.name, status="ok")
    run = SearchRun(watch_id=watch.id, status="running", errors=[])
    session.add(run)
    session.commit()

    if not combos:
        run.status, run.finished_at = "skipped", utcnow()
        run.errors = ["no future dates left in this watch"]
        session.commit()
        result.status = "skipped"
        result.errors = run.errors
        return result

    offers: list[Offer] = []
    disabled: set[str] = set()
    for provider in providers:
        limit = getattr(settings, MONTHLY_LIMIT_ATTR.get(provider.name, ""), None)
        for origin in cfg.origins:
            for dest in cfg.destinations:
                if origin == dest:
                    continue
                for dep, ret in combos:
                    if provider.name in disabled:
                        break
                    if limit is not None and usage_this_month(session, provider.name) >= limit:
                        msg = f"{provider.name}: monthly search limit {limit} reached – skipping remaining searches"
                        log.warning(msg)
                        result.errors.append(msg)
                        disabled.add(provider.name)
                        break
                    q = SearchQuery(
                        origin=origin,
                        destination=dest,
                        departure_date=dep,
                        return_date=ret,
                        adults=cfg.adults,
                        cabin_class=cfg.cabin_class,
                        max_connections=cfg.max_connections,
                        currency=cfg.currency,
                    )
                    _count_usage(session, provider.name)
                    session.commit()
                    result.searches += 1
                    try:
                        found = provider.search(q)
                    except ProviderAuthError as exc:
                        msg = f"{provider.name}: authentication failed ({exc}) – provider disabled for this run"
                        log.error(msg)
                        result.errors.append(msg)
                        disabled.add(provider.name)
                        break
                    except ProviderError as exc:
                        msg = f"{provider.name} {origin}-{dest} {dep}/{ret}: {exc}"
                        log.warning(msg)
                        result.errors.append(msg)
                        continue
                    except Exception as exc:  # defensive: unknown provider bug
                        log.exception("unexpected provider error")
                        result.errors.append(f"{provider.name} {origin}-{dest} {dep}: unexpected {exc!r}")
                        continue
                    offers.extend(found)

    for o in offers:
        session.add(_snapshot(watch, run, o))
    run.searches, run.offers_found = result.searches, len(offers)
    result.offers = len(offers)

    if result.errors and offers:
        result.status = "partial"
    elif result.errors:
        result.status = "failed"
    run.status = result.status
    run.errors = result.errors[:50]
    run.finished_at = utcnow()
    session.commit()

    same_ccy = [o for o in offers if o.currency == watch.currency]
    if len(same_ccy) < len(offers):
        log.warning(
            "%s: %d offers in other currencies than %s are stored but not compared",
            watch.name,
            len(offers) - len(same_ccy),
            watch.currency,
        )
    if not same_ccy:
        return result

    cheapest = min(same_ccy, key=lambda o: o.total_amount)
    result.cheapest = cheapest
    prev = previous_low(session, watch, watch.currency, run.id)

    candidates: list[tuple[Offer, list[str]]] = []
    if watch.target_price is not None:
        # All distinct offers under target (cheapest first, max 3 per run to avoid spam).
        under = sorted((o for o in same_ccy if o.total_amount <= watch.target_price), key=lambda o: o.total_amount)
        uniq: dict[str, Offer] = {}
        for o in under:
            uniq.setdefault(o.fingerprint, o)
        for o in list(uniq.values())[:3]:
            reasons = [REASON_BELOW_TARGET]
            if o is cheapest and prev is not None and o.total_amount < prev:
                reasons.append(REASON_NEW_LOW)
            candidates.append((o, reasons))
    if prev is not None and cheapest.total_amount < prev and not any(o is cheapest for o, _ in candidates):
        candidates.append((cheapest, [REASON_NEW_LOW]))

    for offer, reasons in candidates:
        if already_alerted(session, watch, offer, settings.alert_min_drop):
            log.info("%s: alert for %s at %s suppressed (duplicate)", watch.name, offer.fingerprint, offer.total_amount)
            continue
        text = format_alert(watch, offer, reasons, prev)
        alert = Alert(
            watch_id=watch.id,
            fingerprint=offer.fingerprint,
            total_amount=offer.total_amount,
            currency=offer.currency,
            reasons=reasons,
            message=text,
            delivered=False,
        )
        session.add(alert)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        if notify is None:
            log.info("Alert (Telegram not configured):\n%s", text)
            continue
        try:
            notify(text)
        except Exception as exc:
            log.error("Telegram delivery failed: %s", exc)
            result.errors.append(f"telegram: {exc}")
            session.delete(alert)  # allow a retry next run
            session.commit()
            continue
        alert.delivered = True
        session.commit()
        result.alerts_sent += 1
    return result


def run_all(
    session_factory: sessionmaker,
    configs: list[WatchConfig],
    providers: list[FlightProvider],
    settings: Settings,
    notify: Callable[[str], None] | None,
    today: dt.date | None = None,
) -> list[RunResult]:
    results = []
    with session_factory() as session:
        watches = sync_watches(session, configs)
        for w in watches:
            try:
                r = run_watch(session, w, providers, settings, notify, today=today)
            except Exception as exc:  # one broken watch must not stop the others
                session.rollback()
                log.exception("watch %s crashed", w.name)
                r = RunResult(watch=w.name, status="failed", errors=[repr(exc)])
            log.info(
                "watch=%s status=%s searches=%d offers=%d cheapest=%s alerts=%d",
                r.watch,
                r.status,
                r.searches,
                r.offers,
                fmt_money(r.cheapest.total_amount, r.cheapest.currency) if r.cheapest else "-",
                r.alerts_sent,
            )
            results.append(r)
    return results
