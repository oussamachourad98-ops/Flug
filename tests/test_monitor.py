from decimal import Decimal

from sqlalchemy import func, select

from flug.db import Alert, OfferSnapshot, SearchRun, Watch
from flug.monitor import REASON_BELOW_TARGET, REASON_NEW_LOW, run_all, sync_watches

from .conftest import TODAY, FakeProvider, make_watch


def _run(sf, cfgs, provider, settings, sent):
    return run_all(sf, cfgs, [provider], settings, sent.append, today=TODAY)


def test_compares_all_origins_and_stores_history(session_factory, settings):
    sent = []
    p = FakeProvider({"FRA": "250", "MUC": "230"})
    (r,) = _run(session_factory, [make_watch()], p, settings, sent)
    assert {q.origin for q in p.calls} == {"FRA", "MUC"}
    assert r.cheapest.origin == "MUC" and r.cheapest.total_amount == Decimal("230")
    assert r.status == "ok" and sent == []  # above target, first run => no "new low"
    with session_factory() as s:
        assert s.scalar(select(func.count(OfferSnapshot.id))) == 2


def test_below_target_alert_sent_once(session_factory, settings):
    sent = []
    cfg = [make_watch(target_price=200)]
    p = FakeProvider({"FRA": "190", "MUC": "300"})
    _run(session_factory, cfg, p, settings, sent)
    assert len(sent) == 1 and "Unter Zielpreis" in sent[0] and "FRA → LIS" in sent[0]
    _run(session_factory, cfg, p, settings, sent)  # same offer, same price
    _run(session_factory, cfg, p, settings, sent)
    assert len(sent) == 1
    p.prices["FRA"] = "195"  # same offer more expensive -> still no alert
    _run(session_factory, cfg, p, settings, sent)
    assert len(sent) == 1
    p.prices["FRA"] = "180"  # meaningfully cheaper -> alert again
    _run(session_factory, cfg, p, settings, sent)
    assert len(sent) == 2 and "Neuer Tiefstpreis" in sent[1]


def test_new_low_without_target(session_factory, settings):
    sent = []
    cfg = [make_watch(target_price=None)]
    p = FakeProvider({"FRA": "300", "MUC": "320"})
    _run(session_factory, cfg, p, settings, sent)
    assert sent == []
    p.prices["MUC"] = "280"
    _run(session_factory, cfg, p, settings, sent)
    assert len(sent) == 1 and "Neuer Tiefstpreis" in sent[0] and "vorher 300.00 EUR" in sent[0]
    with session_factory() as s:
        a = s.scalar(select(Alert))
        assert a.reasons == [REASON_NEW_LOW] and a.delivered


def test_alert_reasons_combined(session_factory, settings):
    sent = []
    cfg = [make_watch(target_price=250)]
    p = FakeProvider({"FRA": "300"})
    _run(session_factory, cfg, p, settings, sent)
    p.prices["FRA"] = None
    p.prices["MUC"] = "240"
    _run(session_factory, cfg, p, settings, sent)
    with session_factory() as s:
        a = s.scalar(select(Alert))
        assert a.reasons == [REASON_BELOW_TARGET, REASON_NEW_LOW]


def test_provider_error_is_partial(session_factory, settings):
    sent = []
    p = FakeProvider({"MUC": "230"}, fail={"FRA"})
    (r,) = _run(session_factory, [make_watch()], p, settings, sent)
    assert r.status == "partial" and any("boom" in e for e in r.errors)
    with session_factory() as s:
        assert s.scalar(select(SearchRun)).status == "partial"


def test_failed_telegram_delivery_retried_next_run(session_factory, settings):
    cfg = [make_watch(target_price=200)]
    p = FakeProvider({"FRA": "150"})
    calls = []

    def broken(text):
        calls.append(text)
        raise RuntimeError("telegram down")

    run_all(session_factory, cfg, [p], settings, broken, today=TODAY)
    sent = []
    _run(session_factory, cfg, p, settings, sent)
    assert len(calls) == 1 and len(sent) == 1


def test_monthly_budget_stops_searching(session_factory, settings):
    settings.duffel_monthly_search_limit = 3
    cfg = [make_watch(departure={"from": "2027-04-10", "to": "2027-04-12"})]  # 2 origins x 3 dates = 6
    p = FakeProvider({"FRA": "300", "MUC": "300"})
    (r,) = _run(session_factory, cfg, p, settings, [])
    assert len(p.calls) == 3 and any("limit" in e for e in r.errors)
    (r,) = _run(session_factory, cfg, p, settings, [])
    assert len(p.calls) == 3 and r.searches == 0


def test_other_currency_not_compared(session_factory, settings):
    class UsdProvider(FakeProvider):
        def search(self, q):
            from .conftest import make_offer

            return [make_offer(q, "10", currency="USD")]

    sent = []
    (r,) = _run(session_factory, [make_watch()], UsdProvider({}), settings, sent)
    assert r.cheapest is None and sent == [] and r.offers == 2


def test_sync_keeps_dashboard_target_until_yaml_changes(session_factory):
    with session_factory() as s:
        (w,) = sync_watches(s, [make_watch(target_price=200)])
        w.target_price = Decimal("150")  # dashboard edit
        s.commit()
        (w,) = sync_watches(s, [make_watch(target_price=200)])
        assert w.target_price == Decimal("150")
        (w,) = sync_watches(s, [make_watch(target_price=210)])
        assert w.target_price == Decimal("210")
        assert sync_watches(s, []) == []
        assert s.scalar(select(Watch)).active is False


def test_past_only_watch_skipped(session_factory, settings):
    cfg = [make_watch(departure=["2026-01-01"])]
    p = FakeProvider({"FRA": "1"})
    (r,) = _run(session_factory, cfg, p, settings, [])
    assert r.status == "skipped" and p.calls == []
