"""Dashboard: cheapest current offers, price history, target editing. HTTP Basic auth."""

from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .db import Alert, OfferSnapshot, ProviderUsage, SearchRun, Watch, utcnow
from .monitor import fmt_baggage, fmt_duration, fmt_money

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["money"] = lambda a, c: fmt_money(Decimal(a), c) if a is not None else "–"
TEMPLATES.env.filters["duration"] = fmt_duration
TEMPLATES.env.filters["baggage"] = fmt_baggage


def create_app(settings: Settings, session_factory: sessionmaker) -> FastAPI:
    if not settings.dashboard_password and not settings.dashboard_allow_no_auth:
        raise RuntimeError(
            "DASHBOARD_PASSWORD is not set. Set it, or DASHBOARD_ALLOW_NO_AUTH=true for local-only use."
        )
    app = FastAPI(title="Flug", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    security = HTTPBasic(auto_error=False)

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not settings.dashboard_password:
            return
        ok = creds is not None and (
            secrets.compare_digest(creds.username.encode(), settings.dashboard_user.encode())
            & secrets.compare_digest(creds.password.encode(), settings.dashboard_password.encode())
        )
        if not ok:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="flug"'}
            )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return resp

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with session_factory() as s:
            last = s.scalar(select(func.max(SearchRun.finished_at)))
        return {"ok": True, "last_run_finished_at": last.isoformat() if last else None}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def index(request: Request):
        with session_factory() as s:
            rows = []
            for w in s.scalars(select(Watch).order_by(Watch.active.desc(), Watch.name)):
                last_run = s.scalar(
                    select(SearchRun).where(SearchRun.watch_id == w.id).order_by(SearchRun.id.desc()).limit(1)
                )
                current = _cheapest_of_last_run(s, w)
                all_time = s.scalar(
                    select(func.min(OfferSnapshot.total_amount)).where(
                        OfferSnapshot.watch_id == w.id, OfferSnapshot.currency == w.currency
                    )
                )
                rows.append({"watch": w, "last_run": last_run, "current": current, "all_time": all_time})
            month = utcnow().strftime("%Y-%m")
            usage = list(s.scalars(select(ProviderUsage).where(ProviderUsage.month == month)))
            alerts = list(s.scalars(select(Alert).order_by(Alert.id.desc()).limit(10)))
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"rows": rows, "usage": usage, "settings": settings, "alerts": alerts, "month": month},
        )

    @app.get("/watch/{watch_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def watch_detail(request: Request, watch_id: int):
        with session_factory() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404)
            last_run = s.scalar(
                select(SearchRun).where(SearchRun.watch_id == w.id).order_by(SearchRun.id.desc()).limit(1)
            )
            offers = []
            per_origin = []
            if last_run:
                offers = list(
                    s.scalars(
                        select(OfferSnapshot)
                        .where(OfferSnapshot.run_id == last_run.id)
                        .order_by(OfferSnapshot.total_amount)
                        .limit(50)
                    )
                )
                seen = set()
                for o in offers:  # cheapest per departure airport = comparison table
                    if o.origin not in seen and o.currency == w.currency:
                        seen.add(o.origin)
                        per_origin.append(o)
            runs = list(s.scalars(select(SearchRun).where(SearchRun.watch_id == w.id).order_by(SearchRun.id.desc()).limit(10)))
        return TEMPLATES.TemplateResponse(
            request,
            "watch.html",
            {"w": w, "offers": offers, "per_origin": per_origin, "last_run": last_run, "runs": runs},
        )

    @app.get("/api/watch/{watch_id}/history", dependencies=[Depends(auth)])
    def history(watch_id: int, days: int = 30):
        """Cheapest price per run and departure airport (watch currency only)."""
        since = utcnow() - dt.timedelta(days=max(1, min(days, 365)))
        with session_factory() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404)
            q = (
                select(SearchRun.started_at, OfferSnapshot.origin, func.min(OfferSnapshot.total_amount))
                .join(SearchRun, SearchRun.id == OfferSnapshot.run_id)
                .where(
                    OfferSnapshot.watch_id == watch_id,
                    OfferSnapshot.currency == w.currency,
                    SearchRun.started_at >= since,
                )
                .group_by(SearchRun.id, SearchRun.started_at, OfferSnapshot.origin)
                .order_by(SearchRun.started_at)
            )
            series: dict[str, list] = {}
            for started, origin, amount in s.execute(q):
                series.setdefault(origin, []).append({"t": started.isoformat() + "Z", "price": float(amount)})
            return JSONResponse(
                {
                    "currency": w.currency,
                    "target": float(w.target_price) if w.target_price is not None else None,
                    "origins": (w.config or {}).get("origins", sorted(series)),
                    "series": series,
                }
            )

    @app.post("/watch/{watch_id}/target", dependencies=[Depends(auth)])
    def set_target(request: Request, watch_id: int, target_price: str = Form("")):
        _check_same_origin(request)
        with session_factory() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404)
            raw = target_price.strip().replace(",", ".")
            if raw == "":
                w.target_price = None
            else:
                try:
                    val = Decimal(raw)
                except InvalidOperation:
                    raise HTTPException(400, "invalid price")
                if val <= 0 or val > 1_000_000:
                    raise HTTPException(400, "price out of range")
                w.target_price = val.quantize(Decimal("0.01"))
            s.commit()
        return RedirectResponse(f"/watch/{watch_id}", status_code=303)

    return app


def _check_same_origin(request: Request) -> None:
    """Basic CSRF protection for the one state-changing form."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        host = request.headers.get("host", "")
        if host and f"//{host}" not in origin:
            raise HTTPException(403, "cross-origin request rejected")


def _cheapest_of_last_run(s, w: Watch) -> OfferSnapshot | None:
    last_ok = s.scalar(
        select(SearchRun.id)
        .where(SearchRun.watch_id == w.id, SearchRun.offers_found > 0)
        .order_by(SearchRun.id.desc())
        .limit(1)
    )
    if last_ok is None:
        return None
    return s.scalar(
        select(OfferSnapshot)
        .where(OfferSnapshot.run_id == last_ok, OfferSnapshot.currency == w.currency)
        .order_by(OfferSnapshot.total_amount)
        .limit(1)
    )
