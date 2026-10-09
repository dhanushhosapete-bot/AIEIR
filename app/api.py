"""HTTP API.

Founder routes: /signup, /consent, /intake, /roadmap, /home, /kpis, /weeks, /reflections,
/chat, /resume, /messages/{id}/feedback, /me/...   (Bearer founder token)
Staff routes:   /staff (dashboard page) and /staff/api/...                 (Bearer staff token)

Run:  uvicorn app.api:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import alerts, db, services, staff as staffsvc
from .classifier import CLASSIFIER_VERSION
from .config import settings

log = logging.getLogger("aieir.api")
STATIC = Path(__file__).resolve().parent / "static"
FORBIDDEN_FOUNDER_KEYS = {"zone", "zones", "safety", "safety_zone", "categories", "confidence", "risk", "mode", "rationale"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = None
    if os.environ.get("ENABLE_ALERT_WORKER", "1") == "1":
        import threading
        stop = threading.Event()
        alerts.start_worker(stop)
    if not alerts.configured():
        log.error("RED alert email is not configured; alerts reach the dashboard only.")
    yield
    if stop:
        stop.set()


app = FastAPI(title="AI EIR", lifespan=lifespan)


def founder_json(payload: Any) -> JSONResponse:
    """Founder responses must never carry safety labels. Fail closed if one slips in."""
    def walk(v):
        if isinstance(v, dict):
            bad = FORBIDDEN_FOUNDER_KEYS & {str(k).lower() for k in v}
            if bad:
                raise RuntimeError(f"founder response contained internal safety fields: {sorted(bad)}")
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(payload)
    return JSONResponse(payload)


@app.exception_handler(services.FlowBlocked)
async def _blocked(_: Request, e: services.FlowBlocked):
    return JSONResponse({"blocked": e.reason, "message": e.message, **e.extra}, status_code=409)


@app.exception_handler(services.NotFound)
@app.exception_handler(staffsvc.NotFound)
async def _nf(_: Request, __):
    return JSONResponse({"error": "not found"}, status_code=404)


@app.exception_handler(staffsvc.Forbidden)
async def _forbidden(_: Request, e):
    return JSONResponse({"error": "your role can't do that"}, status_code=403)


@app.exception_handler(ValueError)
async def _bad(_: Request, e: ValueError):
    return JSONResponse({"error": str(e)}, status_code=422)


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return h[7:].strip()


def current_founder(request: Request) -> str:
    uid = db.user_for_token(_bearer(request))
    if not uid:
        raise HTTPException(401, "invalid token")
    return str(uid)


def current_staff(request: Request) -> db.Staff:
    s = db.staff_for_token(_bearer(request))
    if not s:
        raise HTTPException(401, "invalid staff token")
    return s


# ------------------------------------------------------------------ founder: account & consent
class Consent(BaseModel):
    version: str
    accepted: bool


class Signup(BaseModel):
    email: str
    name: str | None = None
    country: str | None = "US"
    consent: Consent


@app.get("/consent")
def consent_text():
    return {"version": services.CONSENT_VERSION, "text": services.CONSENT_TEXT}


@app.post("/signup")
def signup(body: Signup):
    if not body.consent.accepted or body.consent.version != services.CONSENT_VERSION:
        raise HTTPException(400, "Please read and accept the current safety-review notice to continue.")
    token = "fdr_" + secrets.token_urlsafe(32)
    try:
        db.create_founder(body.email, body.name, body.country, token, body.consent.version)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "An account with that email already exists.")
    return {"token": token}


# ------------------------------------------------------------------ founder: coaching loop
class Answer(BaseModel):
    answer: str | None = None
    skip: bool = False


class Status(BaseModel):
    status: str


class Reflection(BaseModel):
    kpi_ids: list[str] = Field(min_length=1)
    text: str | None = None
    skip: bool = False


class Chat(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class Feedback(BaseModel):
    rating: int
    reasons: list[str] = []
    comment: str | None = Field(default=None, max_length=1000)


class Prefs(BaseModel):
    tone: str | None = None
    professional_stretch: int | None = None
    professional_count: int | None = None


@app.get("/intake")
def intake(uid: str = Depends(current_founder)):
    return founder_json(services.intake_status(uid))


@app.post("/intake/answer")
def intake_answer(body: Answer, uid: str = Depends(current_founder)):
    return founder_json(services.intake_answer(uid, None if body.skip else body.answer))


@app.post("/roadmap")
def roadmap(uid: str = Depends(current_founder)):
    return founder_json(services.create_roadmap(uid))


@app.get("/home")
def home(uid: str = Depends(current_founder)):
    return founder_json(services.home(uid))


@app.post("/kpis/{kpi_id}/status")
def kpi_status(kpi_id: str, body: Status, uid: str = Depends(current_founder)):
    return founder_json(services.set_kpi_status(uid, kpi_id, body.status))


@app.post("/weeks/close")
def close_week(uid: str = Depends(current_founder)):
    return founder_json(services.close_week(uid))


@app.post("/weeks/next")
def next_week(uid: str = Depends(current_founder)):
    return founder_json(services.start_next_week(uid))


@app.post("/reflections")
def reflect(body: Reflection, uid: str = Depends(current_founder)):
    return founder_json(services.submit_reflection(uid, body.kpi_ids, body.text, skip=body.skip))


@app.post("/chat")
def chat(body: Chat, uid: str = Depends(current_founder)):
    return founder_json(services.founder_turn(uid, body.message, channel="chat"))


@app.post("/resume")
def resume(uid: str = Depends(current_founder)):
    return founder_json(services.resume(uid))


@app.post("/messages/{message_id}/feedback")
def feedback(message_id: str, body: Feedback, uid: str = Depends(current_founder)):
    return founder_json(services.give_feedback(uid, message_id, body.rating, body.reasons, body.comment))


@app.get("/me/adaptation")
def get_adaptation(uid: str = Depends(current_founder)):
    return founder_json(services.get_adaptation(uid))


@app.put("/me/adaptation")
def put_adaptation(body: Prefs, uid: str = Depends(current_founder)):
    return founder_json(services.set_adaptation(uid, **body.model_dump()))


@app.delete("/me/adaptation")
def reset_adaptation(uid: str = Depends(current_founder)):
    return founder_json(services.reset_adaptation(uid))


@app.get("/me/export")
def export(uid: str = Depends(current_founder)):
    return founder_json(services.export_my_data(uid))


@app.delete("/me")
def delete_me(uid: str = Depends(current_founder)):
    return founder_json(services.delete_my_data(uid))


# ------------------------------------------------------------------ staff
class Review(BaseModel):
    verdict: str
    human_zone: str
    note: str | None = Field(default=None, max_length=2000)


@app.get("/staff", response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse((STATIC / "dashboard.html").read_text(), headers={"Cache-Control": "no-store",
                        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


@app.get("/staff/api/me")
def staff_me(s: db.Staff = Depends(current_staff)):
    return {"role": s.role, "permissions": sorted(p for p, roles in staffsvc.PERMISSIONS.items() if s.role in roles)}


@app.get("/staff/api/overview")
def staff_overview(s: db.Staff = Depends(current_staff)):
    return staffsvc.overview(s)


@app.get("/staff/api/founders")
def staff_founders(s: db.Staff = Depends(current_staff)):
    return staffsvc.founder_list(s)


@app.get("/staff/api/founders/{founder_id}")
def staff_founder(founder_id: str, s: db.Staff = Depends(current_staff)):
    return staffsvc.founder_detail(s, founder_id)


@app.get("/staff/api/reviews")
def staff_queue(s: db.Staff = Depends(current_staff)):
    return staffsvc.review_queue(s)


@app.post("/staff/api/reviews/{event_id}")
def staff_review(event_id: str, body: Review, s: db.Staff = Depends(current_staff)):
    return staffsvc.submit_review(s, event_id, body.verdict, body.human_zone, body.note)


@app.post("/staff/api/alerts/{alert_id}/ack")
def staff_ack(alert_id: str, s: db.Staff = Depends(current_staff)):
    return staffsvc.ack_alert(s, alert_id)


@app.post("/staff/api/alerts/{alert_id}/resolve")
def staff_resolve(alert_id: str, s: db.Staff = Depends(current_staff)):
    return staffsvc.ack_alert(s, alert_id, resolve=True)


@app.get("/staff/api/trends")
def staff_trends(s: db.Staff = Depends(current_staff)):
    return staffsvc.trends(s)


@app.get("/staff/api/audit")
def staff_audit(s: db.Staff = Depends(current_staff)):
    return staffsvc.audit_log(s)


@app.get("/staff/api/stream")
async def staff_stream(request: Request, s: db.Staff = Depends(current_staff)):
    """Server-Sent Events. Payloads carry ids and zone only; details are fetched separately."""
    staffsvc.require(s, "view_overview")

    async def gen():
        conn = await psycopg.AsyncConnection.connect(db._url("staff"), autocommit=True)
        try:
            await conn.execute("LISTEN aieir_live")
            yield "event: hello\ndata: {}\n\n"
            while not await request.is_disconnected():
                got = False
                async for n in conn.notifies(timeout=15):
                    got = True
                    yield f"data: {n.payload}\n\n"
                if not got:
                    yield ": keep-alive\n\n"
        finally:
            await conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/health")
def health():
    ok = True
    try:
        with db.anonymous_session() as conn:
            conn.execute("SELECT 1")
    except Exception:
        ok = False
    enc = True
    try:
        from . import crypto
        crypto._keys()
    except Exception:
        enc = False
    body = {"database": ok, "encryption_configured": enc, "alert_email_configured": alerts.configured(),
            "model_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "classifier_version": CLASSIFIER_VERSION, "prompt_version": settings.eir_prompt_version}
    return JSONResponse(body, status_code=200 if ok and enc else 503)


@app.get("/")
def root():
    return {"service": "AI EIR API", "staff_portal": "/staff", "api_docs": "/docs", "health": "/health"}
