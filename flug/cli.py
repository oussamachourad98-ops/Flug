"""Command line entry point.

  flug serve            dashboard + scheduler (every SEARCH_INTERVAL_MINUTES)
  flug search-once      one search round for all watches (for cron / CI)
  flug check-config     validate watches.yaml and print search volume estimate
  flug places <query>   look up IATA airport/city codes (Duffel)
  flug test-telegram    send a test message
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from .config import date_combinations, get_settings, load_watches
from .providers.base import ProviderError

log = logging.getLogger("flug")


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs (Telegram token!)


def _notifier(settings):
    if not settings.telegram_enabled:
        log.warning("Telegram not configured – alerts are only logged")
        return None
    from .telegram import TelegramNotifier

    return TelegramNotifier(
        settings.telegram_bot_token, settings.telegram_chat_id, base_url=settings.telegram_api_url
    ).send


def _search_round(settings, session_factory, lock: threading.Lock) -> None:
    from .monitor import run_all
    from .providers import build_providers

    if not lock.acquire(blocking=False):
        log.warning("previous search round still running – skipping this one")
        return
    try:
        configs = load_watches(settings.watches_file)
        run_all(session_factory, configs, build_providers(settings), settings, _notifier(settings))
    except Exception:
        log.exception("search round failed")
    finally:
        lock.release()


def cmd_search_once(settings) -> int:
    from .db import make_engine, make_session_factory

    sf = make_session_factory(make_engine(settings.database_url))
    from .monitor import run_all
    from .providers import build_providers

    results = run_all(sf, load_watches(settings.watches_file), build_providers(settings), settings, _notifier(settings))
    return 1 if results and all(r.status == "failed" for r in results) else 0


def cmd_serve(settings) -> int:
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler

    from .db import make_engine, make_session_factory
    from .web import create_app

    sf = make_session_factory(make_engine(settings.database_url))
    load_watches(settings.watches_file)  # fail fast on a broken config
    app = create_app(settings, sf)
    lock = threading.Lock()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        _search_round,
        "interval",
        minutes=settings.search_interval_minutes,
        args=[settings, sf, lock],
        id="search",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sched.start()
    if settings.run_on_start:
        threading.Thread(target=_search_round, args=(settings, sf, lock), daemon=True).start()
    log.info("Dashboard on http://%s:%d – searching every %d min", settings.dashboard_host, settings.dashboard_port, settings.search_interval_minutes)
    try:
        uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port, log_level="warning")
    finally:
        sched.shutdown(wait=False)
    return 0


def cmd_check_config(settings) -> int:
    watches = load_watches(settings.watches_file)
    runs_per_month = 30 * 24 * 60 // settings.search_interval_minutes
    total = 0
    for w in watches:
        if not w.active:
            continue
        combos = date_combinations(w)
        pairs = sum(1 for o in w.origins for d in w.destinations if o != d)
        n = pairs * len(combos) * len(settings.provider_names)
        total += n
        print(f"{w.name}: {len(w.origins)} origin(s) x {len(w.destinations)} dest(s) x {len(combos)} date pair(s) = {n} searches/round")
    print(f"Total: {total} searches per round, ~{total * runs_per_month} per month at {settings.search_interval_minutes} min interval")
    for name in settings.provider_names:
        limit = getattr(settings, f"{name}_monthly_search_limit")
        per_provider = total // max(len(settings.provider_names), 1) * runs_per_month
        if per_provider > limit:
            print(f"WARNING: {name} monthly limit is {limit}; searches will stop when it is reached. "
                  f"Increase the interval or reduce dates/airports.")
    return 0


def cmd_places(settings, query: str) -> int:
    from .providers import DuffelProvider

    p = DuffelProvider(settings.duffel_access_token, base_url=settings.duffel_api_url)
    for place in p.place_suggestions(query):
        kind = place.get("type")
        print(f"{place.get('iata_code')}\t{kind}\t{place.get('name')}\t{place.get('city_name') or ''}")
        for ap in place.get("airports") or []:
            print(f"  {ap.get('iata_code')}\tairport\t{ap.get('name')}")
    return 0


def cmd_test_telegram(settings) -> int:
    notify = _notifier(settings)
    if notify is None:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
        return 1
    notify("✅ <b>Flug</b>: Telegram-Benachrichtigungen funktionieren.")
    print("sent")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flug", description="Personal flight price monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("search-once")
    sub.add_parser("check-config")
    p = sub.add_parser("places")
    p.add_argument("query")
    sub.add_parser("test-telegram")
    args = parser.parse_args(argv)

    settings = get_settings()
    _setup_logging(settings.log_level)
    try:
        if args.cmd == "serve":
            return cmd_serve(settings)
        if args.cmd == "search-once":
            return cmd_search_once(settings)
        if args.cmd == "check-config":
            return cmd_check_config(settings)
        if args.cmd == "places":
            return cmd_places(settings, args.query)
        if args.cmd == "test-telegram":
            return cmd_test_telegram(settings)
    except (FileNotFoundError, ValueError, RuntimeError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
