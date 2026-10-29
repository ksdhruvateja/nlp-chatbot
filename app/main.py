"""FixBridge AI repair service - HTTP API.

Phase 1 (user input) happens in the FixBridge UI; this API implements Phase 2
(API and file processing), kicks off phases 3-8 as a background job, exposes the
customer decision points, and implements Phase 9 (outcomes and property history).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, ingestion, monitoring, pricing
from .ai.orchestrator import health_report
from .api_models import (EquipmentIn, KnowledgeMetaIn, KnowledgeReviewIn, OutcomeIn, OutcomeReviewIn, ProductIn, PropertyIn,
                         SafetyChoiceIn)
from .config import get_settings
from .pipeline import runner
from .schemas import SERVICE_CATEGORIES
from .security import audit, authorize_property, authorize_request, current_user, issue_token, require_role
from .storage import UploadRejected, get_store, validate_upload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fixbridge")

MAX_FILES_PER_REQUEST = 10
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    worker = None
    if get_settings().run_worker_in_process:
        worker = runner.Worker()
        worker.start()
    yield
    if worker:
        worker.stop()
    db.close_pool()


app = FastAPI(title="FixBridge AI Repair Service", version="1.0.0", lifespan=lifespan,
              root_path=os.getenv("ROOT_PATH", "").rstrip("/"))


# ---------------------------------------------------------------- cross-cutting middleware
@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.monotonic()
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error rid=%s", rid)
        response = JSONResponse({"detail": "Internal error", "request_id": rid}, status_code=500)
    response.headers["x-request-id"] = rid
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    log.info("%s %s %s %dms rid=%s", request.method, request.url.path, response.status_code,
             (time.monotonic() - start) * 1000, rid)
    return response


_rate: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int, window_s: int) -> None:
    now = time.time()
    q = _rate[key]
    while q and q[0] < now - window_s:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests, please slow down")
    q.append(now)


async def _read_uploads(files: list[UploadFile]) -> list[tuple[str, str, bytes, str]]:
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400, f"At most {MAX_FILES_PER_REQUEST} files per submission")
    limit = get_settings().max_upload_mb * 1024 * 1024
    out = []
    for f in files:
        if not f.filename:
            continue
        data = await f.read(limit + 1)
        mime = (f.content_type or "application/octet-stream").split(";")[0].strip().lower()
        try:
            kind = validate_upload(f.filename, mime, data)
        except UploadRejected as exc:
            raise HTTPException(400, str(exc)) from exc
        out.append((Path(f.filename).name[:200], mime, data, kind))
    return out


def _store_media(conn, request_id, uploads, clarification_round: int = 0) -> list[str]:
    ids = []
    store = get_store()
    for filename, mime, data, kind in uploads:
        key, sha = store.put(data)
        row = conn.execute(
            """INSERT INTO media (request_id, kind, mime_type, filename, size_bytes, sha256, storage_key,
                                  clarification_round)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (request_id, kind, mime, filename, len(data), sha, key, clarification_round)).fetchone()
        ids.append(str(row["id"]))
    return ids


def _request_view(req: dict) -> dict:
    latest = runner.latest_version(req["id"])
    media = db.fetch_all("""SELECT id, kind, filename, mime_type, size_bytes, clarification_round, created_at
                            FROM media WHERE request_id = %s ORDER BY created_at""", (req["id"],))
    outcome = db.fetch_one("SELECT resolution, rating, label, created_at FROM outcomes WHERE request_id = %s "
                           "ORDER BY created_at DESC LIMIT 1", (req["id"],))
    return {"id": str(req["id"]), "property_id": str(req["property_id"]), "service_category": req["service_category"],
            "description": req["description"], "status": req["status"], "stage": req["stage"],
            "clarification_round": req["clarification_round"], "error": req["error"],
            "created_at": req["created_at"].isoformat(), "updated_at": req["updated_at"].isoformat(),
            "media": [{**m, "id": str(m["id"]), "created_at": m["created_at"].isoformat()} for m in media],
            "result": latest["response"] if latest else None, "outcome": outcome}


# ---------------------------------------------------------------- health
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        db.fetch_one("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    providers = health_report()
    ai_ok = any(p["enabled"] and p["configured"] and not p["circuit_open"]
                and p.get("reachable", True) for p in providers.values())
    body = {"database": db_ok, "ai_available": ai_ok, "providers": providers}
    return JSONResponse(body, status_code=200 if db_ok and ai_ok else 503)


# ---------------------------------------------------------------- identity
if os.getenv("DEV_AUTH", "false").lower() == "true":
    @app.post("/api/dev/login")
    def dev_login(email: str = Form(...), name: str = Form(""), role: str = Form("customer")):
        """DEV ONLY: in production the existing FixBridge login issues tokens."""
        user = db.fetch_one("""INSERT INTO users (email, name, role) VALUES (%s, %s, %s)
                               ON CONFLICT (email) DO UPDATE SET name = coalesce(nullif(excluded.name, ''), users.name)
                               RETURNING id, email, name, role""", (email.lower(), name, role))
        return {"token": issue_token(user["id"]), "user": {**user, "id": str(user["id"])}}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {**user, "id": str(user["id"])}


@app.get("/api/service-categories")
def service_categories():
    return list(SERVICE_CATEGORIES)


# ---------------------------------------------------------------- properties ("passport")
@app.get("/api/properties")
def list_properties(user: dict = Depends(current_user)):
    rows = db.fetch_all("""SELECT DISTINCT p.id, p.name, p.property_type, p.year_built FROM properties p
                           LEFT JOIN property_members m ON m.property_id = p.id
                           WHERE p.owner_id = %(u)s OR m.user_id = %(u)s ORDER BY p.name""", {"u": user["id"]})
    return [{**r, "id": str(r["id"])} for r in rows]


@app.post("/api/properties", status_code=201)
def create_property(body: PropertyIn, request: Request, user: dict = Depends(current_user)):
    row = db.fetch_one("""INSERT INTO properties (owner_id, name, address, property_type, year_built, passport)
                          VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                       (user["id"], body.name, body.address, body.property_type, body.year_built,
                        db.jsonb(body.passport)))
    audit(user["id"], "property.create", str(row["id"]), request=request)
    return {"id": str(row["id"])}


@app.get("/api/properties/{property_id}")
def get_property(property_id: str, user: dict = Depends(current_user)):
    authorize_property(user, property_id)
    prop = db.fetch_one("SELECT * FROM properties WHERE id = %s", (property_id,))
    equipment = db.fetch_all("SELECT * FROM equipment WHERE property_id = %s ORDER BY created_at", (property_id,))
    history = db.fetch_all("""SELECT event_type, summary, source, details, created_at, request_id
                              FROM service_history WHERE property_id = %s ORDER BY created_at DESC LIMIT 50""",
                           (property_id,))
    return json.loads(json.dumps({"property": prop, "equipment": equipment, "service_history": history}, default=str))


@app.post("/api/properties/{property_id}/equipment", status_code=201)
def add_equipment(property_id: str, body: EquipmentIn, user: dict = Depends(current_user)):
    authorize_property(user, property_id)
    row = db.fetch_one("""INSERT INTO equipment (property_id, service_category, kind, brand, model_number,
                              serial_number, install_year, notes)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                       (property_id, body.service_category, body.kind, body.brand,
                        body.model_number.upper() if body.model_number else None, body.serial_number,
                        body.install_year, body.notes))
    return {"id": str(row["id"])}


# ---------------------------------------------------------------- Phase 2: repair requests
@app.post("/api/repair-requests", status_code=202)
async def submit_repair_request(
        request: Request,
        property_id: str = Form(...),
        service_category: str = Form(...),
        description: str = Form(...),
        equipment_id: str | None = Form(None),
        files: list[UploadFile] = File(default=[]),
        idempotency_key: str | None = Header(default=None),
        user: dict = Depends(current_user)):
    # Authenticate user and authorize property
    authorize_property(user, property_id)
    rate_limit(f"submit:{user['id']}", limit=20, window_s=3600)

    # Validate request, file type, size and security
    if service_category not in SERVICE_CATEGORIES:
        raise HTTPException(422, f"service_category must be one of {list(SERVICE_CATEGORIES)}")
    description = description.strip()
    if not 10 <= len(description) <= 5000:
        raise HTTPException(422, "Please describe the problem in 10-5000 characters")
    if equipment_id:
        if not db.fetch_one("SELECT 1 FROM equipment WHERE id::text = %s AND property_id = %s",
                            (equipment_id, property_id)):
            raise HTTPException(422, "Unknown equipment for this property")
    uploads = await _read_uploads(files)

    if idempotency_key:
        existing = db.fetch_one("SELECT * FROM repair_requests WHERE user_id = %s AND idempotency_key = %s",
                                (user["id"], idempotency_key[:200]))
        if existing:
            return _request_view(existing)

    # Create repair request ID and processing status; store original media; create metadata/checksums/refs
    with db.connection() as conn:
        req = conn.execute(
            """INSERT INTO repair_requests (property_id, user_id, service_category, description, equipment_id,
                                            idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (property_id, user["id"], service_category, description, equipment_id or None,
             idempotency_key[:200] if idempotency_key else None)).fetchone()
        media_ids = _store_media(conn, req["id"], uploads)
    audit(user["id"], "repair_request.create", str(req["id"]), {"media": media_ids}, request)
    runner.enqueue(req["id"])
    return _request_view(req)


@app.get("/api/repair-requests")
def list_requests(user: dict = Depends(current_user)):
    rows = db.fetch_all("""SELECT r.id, r.service_category, r.description, r.status, r.created_at, p.name AS property
                           FROM repair_requests r JOIN properties p ON p.id = r.property_id
                           LEFT JOIN property_members m ON m.property_id = p.id AND m.user_id = %(u)s
                           WHERE p.owner_id = %(u)s OR m.user_id IS NOT NULL
                           ORDER BY r.created_at DESC LIMIT 100""", {"u": user["id"]})
    return [{**r, "id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]


@app.get("/api/repair-requests/{request_id}")
def get_request(request_id: str, user: dict = Depends(current_user)):
    return _request_view(authorize_request(user, request_id))


@app.get("/api/repair-requests/{request_id}/versions")
def get_versions(request_id: str, user: dict = Depends(current_user)):
    authorize_request(user, request_id)
    rows = db.fetch_all("""SELECT version, outcome, provider, model, created_at, response FROM diagnoses
                           WHERE request_id = %s ORDER BY version""", (request_id,))
    return json.loads(json.dumps(rows, default=str))


# ---------------------------------------------------------------- clarification loop
@app.post("/api/repair-requests/{request_id}/clarifications", status_code=202)
async def answer_clarification(request_id: str, request: Request, answers: str = Form("{}"),
                               files: list[UploadFile] = File(default=[]), user: dict = Depends(current_user)):
    req = authorize_request(user, request_id)
    if req["status"] != "needs_clarification":
        raise HTTPException(409, "This request is not waiting for clarification")
    try:
        parsed = json.loads(answers)
        assert isinstance(parsed, dict)
    except (json.JSONDecodeError, AssertionError) as exc:
        raise HTTPException(422, "answers must be a JSON object of question_id -> answer") from exc
    clar = db.fetch_one("SELECT * FROM clarifications WHERE request_id = %s ORDER BY round DESC LIMIT 1",
                        (request_id,))
    valid_ids = {q["id"] for q in clar["questions"]}
    clean = {k: str(v)[:2000] for k, v in parsed.items() if k in valid_ids and str(v).strip()}
    uploads = await _read_uploads(files)
    if not clean and not uploads:
        raise HTTPException(422, "Please answer at least one question or upload a file")

    # Validate new inputs and store media -> update request context and evidence history
    with db.connection() as conn:
        _store_media(conn, request_id, uploads, clarification_round=clar["round"])
        conn.execute("UPDATE clarifications SET answers = %s, answered_at = now() WHERE id = %s",
                     (db.jsonb(clean), clar["id"]))
        conn.execute("""UPDATE repair_requests SET clarification_round = %s, status = 'received',
                        stage = 'requeued', updated_at = now() WHERE id = %s""", (clar["round"], request_id))
    audit(user["id"], "repair_request.clarify", request_id, {"round": clar["round"]}, request)
    runner.enqueue(request_id)
    return _request_view(db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,)))


# ---------------------------------------------------------------- safety decision
@app.post("/api/repair-requests/{request_id}/safety-choice")
def safety_choice(request_id: str, body: SafetyChoiceIn, request: Request, user: dict = Depends(current_user)):
    req = authorize_request(user, request_id)
    last = runner.latest_version(request_id)
    if body.choice == "continue" and last and last["outcome"] == "safety_warning":
        required = {w["code"] for w in last["safety"].get("warnings", []) if w["severity"] != "advisory"}
        if not required <= set(body.acknowledged_warning_codes):
            raise HTTPException(422, {"message": "Please acknowledge every warning", "required": sorted(required)})
    try:
        outcome = runner.apply_safety_choice(str(req["id"]), user["id"], body.choice)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(user["id"], f"safety.{body.choice}", request_id, {"outcome": outcome}, request)
    return _request_view(db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,)))


# ---------------------------------------------------------------- private media access
@app.get("/api/media/{media_id}")
def get_media(media_id: str, request: Request, preview: bool = False, user: dict = Depends(current_user)):
    try:
        uuid.UUID(media_id)
    except ValueError as exc:
        raise HTTPException(404, "Not found") from exc
    m = db.fetch_one("SELECT * FROM media WHERE id = %s", (media_id,))
    if not m:
        raise HTTPException(404, "Not found")
    authorize_request(user, m["request_id"])
    store = get_store()
    if preview and m["derived"].get("preview_key"):
        data, mime = store.get(m["derived"]["preview_key"]), "image/jpeg"
    else:
        data, mime = store.get(m["storage_key"], m["sha256"]), m["mime_type"]
    audit(user["id"], "media.read", media_id, request=request)
    return Response(data, media_type=mime, headers={
        "content-disposition": f'inline; filename="{m["filename"].replace(chr(34), "")}"',
        "cache-control": "private, no-store"})


# ---------------------------------------------------------------- Phase 9: outcomes & property history
@app.post("/api/repair-requests/{request_id}/outcome", status_code=201)
def report_outcome(request_id: str, body: OutcomeIn, request: Request, user: dict = Depends(current_user)):
    req = authorize_request(user, request_id)
    if req["status"] not in ("completed", "cancelled", "blocked"):
        raise HTTPException(409, "Outcome can be reported once the request has a final result")
    cents = round(body.actual_cost * 100) if body.actual_cost is not None else None
    with db.connection() as conn:
        out = conn.execute(
            """INSERT INTO outcomes (request_id, user_id, resolution, parts_used, actual_cost_cents, rating, feedback)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (request_id, user["id"], body.resolution, db.jsonb(body.parts_used), cents, body.rating,
             body.feedback)).fetchone()
        # Update property passport and service history (labelled as user-reported until verified).
        event = {"fixed_diy": "repair", "fixed_professional": "professional_visit"}.get(body.resolution, "note")
        conn.execute("""INSERT INTO service_history (property_id, request_id, event_type, summary, details, source)
                        VALUES (%s, %s, %s, %s, %s, 'user_reported')""",
                     (req["property_id"], request_id, event,
                      f"{body.resolution.replace('_', ' ').capitalize()}: {req['description'][:150]}",
                      db.jsonb({"parts_used": body.parts_used, "actual_cost_cents": cents, "outcome_id": str(out["id"])})))
        if body.parts_used:
            conn.execute(
                """UPDATE properties SET updated_at = now(),
                       passport = jsonb_set(passport, '{replaced_parts}',
                                            coalesce(passport->'replaced_parts', '[]'::jsonb) || %s::jsonb)
                   WHERE id = %s""",
                (json.dumps([{**p, "request_id": request_id, "source": "user_reported"} for p in body.parts_used]),
                 req["property_id"]))
    audit(user["id"], "outcome.report", request_id, {"resolution": body.resolution}, request)
    return {"id": str(out["id"]), "label": "user_reported"}


# ---------------------------------------------------------------- admin / reviewer
@app.post("/api/admin/knowledge", status_code=201)
async def upload_knowledge(meta: str = Form(...), file: UploadFile = File(...),
                           user: dict = Depends(require_role("admin"))):
    try:
        m = KnowledgeMetaIn.model_validate_json(meta)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    data = await file.read(200 * 1024 * 1024)
    try:
        result = ingestion.ingest(data, file.filename or "upload", ingestion.DocumentMeta(
            **m.model_dump(), approved_by=user["email"]))
    except ingestion.IngestionRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(user["id"], "knowledge.ingest", result["document_id"], result)
    return result


@app.post("/api/repair-requests/{request_id}/refresh-prices")
def refresh_prices(request_id: str, request: Request, user: dict = Depends(current_user)):
    """Customer asks for current prices: re-price the latest DIY plan with a live lookup."""
    authorize_request(user, request_id)
    rate_limit(f"prices:{user['id']}", limit=10, window_s=3600)
    if not pricing.enabled():
        raise HTTPException(503, "Live price lookup is not configured")
    try:
        runner.reprice(request_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(user["id"], "prices.refresh", request_id, request=request)
    return _request_view(db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,)))


@app.get("/api/admin/knowledge/pending")
def pending_knowledge(brand: str | None = None, limit: int = 100,
                      user: dict = Depends(require_role("admin", "reviewer"))):
    rows = db.fetch_all("""SELECT d.id, d.title, d.brand, d.model_numbers, d.doc_type, d.published_year,
                                  d.service_category, d.source_url, d.created_at, count(p.id) AS passages
                           FROM knowledge_documents d LEFT JOIN knowledge_passages p ON p.document_id = d.id
                           WHERE d.review_status = 'pending' AND (%s::text IS NULL OR d.brand = %s)
                           GROUP BY d.id ORDER BY d.created_at LIMIT %s""", (brand, brand, min(limit, 1000)))
    return json.loads(json.dumps(rows, default=str))


@app.post("/api/admin/knowledge/review")
def review_knowledge(body: KnowledgeReviewIn, request: Request, user: dict = Depends(require_role("admin"))):
    """Approve (make usable for answers) or reject crawled documents, individually or by brand."""
    if not body.document_ids and not body.brand:
        raise HTTPException(422, "Give document_ids or brand")
    approved = body.action == "approve"
    rows = db.fetch_all(
        """UPDATE knowledge_documents SET review_status = %s, active = %s, reviewed_by = %s, reviewed_at = now()
           WHERE review_status = 'pending' AND (id::text = ANY(%s) OR (%s::text IS NOT NULL AND brand = %s))
           RETURNING id""",
        ("approved" if approved else "rejected", approved, user["email"], body.document_ids, body.brand, body.brand))
    audit(user["id"], f"knowledge.{body.action}", None, {"count": len(rows), "brand": body.brand}, request)
    return {"updated": len(rows)}


@app.get("/api/admin/crawl")
def crawl_status(user: dict = Depends(require_role("admin", "reviewer"))):
    from .crawler import frontier_stats
    return json.loads(json.dumps(frontier_stats(), default=str))


@app.get("/api/admin/knowledge")
def list_knowledge(user: dict = Depends(require_role("admin", "reviewer"))):
    rows = db.fetch_all("""SELECT d.id, d.title, d.source, d.version, d.permission, d.service_category, d.brand,
                                  d.model_numbers, d.active, d.approved_by, d.created_at, count(p.id) AS passages
                           FROM knowledge_documents d LEFT JOIN knowledge_passages p ON p.document_id = d.id
                           GROUP BY d.id ORDER BY d.created_at DESC""")
    return json.loads(json.dumps(rows, default=str))


@app.post("/api/admin/products", status_code=201)
def upsert_product(body: ProductIn, user: dict = Depends(require_role("admin"))):
    db.execute("""INSERT INTO products (sku, name, brand, service_category, part_type, compatible_models,
                      price_cents, currency, price_source, price_verified_at)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                  ON CONFLICT (sku) DO UPDATE SET name = excluded.name, brand = excluded.brand,
                      service_category = excluded.service_category, part_type = excluded.part_type,
                      compatible_models = excluded.compatible_models, price_cents = excluded.price_cents,
                      currency = excluded.currency, price_source = excluded.price_source,
                      price_verified_at = now()""",
               (body.sku, body.name, body.brand, body.service_category, body.part_type,
                [m.upper() for m in body.compatible_models], round(body.price * 100), body.currency,
                body.price_source))
    return {"sku": body.sku}


@app.get("/api/admin/outcomes")
def list_outcomes(user: dict = Depends(require_role("admin", "reviewer"))):
    rows = db.fetch_all("""SELECT o.*, d.outcome AS ai_outcome, d.provider, d.model
                           FROM outcomes o LEFT JOIN LATERAL (
                               SELECT outcome, provider, model FROM diagnoses WHERE request_id = o.request_id
                               ORDER BY version DESC LIMIT 1) d ON true
                           ORDER BY o.created_at DESC LIMIT 200""")
    return json.loads(json.dumps(rows, default=str))


@app.post("/api/admin/outcomes/{outcome_id}/review")
def review_outcome(outcome_id: str, body: OutcomeReviewIn, request: Request,
                   user: dict = Depends(require_role("admin", "reviewer"))):
    row = db.fetch_one("""UPDATE outcomes SET label = %s, approved_for_eval = %s,
                              verified_by = CASE WHEN %s = 'verified' THEN %s::uuid ELSE verified_by END
                          WHERE id::text = %s RETURNING request_id""",
                       (body.label, body.approved_for_eval, body.label, user["id"], outcome_id))
    if not row:
        raise HTTPException(404, "Outcome not found")
    if body.label == "verified":
        db.execute("UPDATE service_history SET source = 'verified' WHERE details->>'outcome_id' = %s", (outcome_id,))
    audit(user["id"], "outcome.review", outcome_id, body.model_dump(), request)
    return {"ok": True}


@app.get("/api/admin/eval-dataset")
def eval_dataset(user: dict = Depends(require_role("admin", "reviewer"))):
    """Use approved feedback for evaluation and knowledge improvement."""
    rows = db.fetch_all("""
        SELECT r.id AS request_id, r.service_category, r.description, d.version, d.outcome AS ai_outcome,
               d.provider, d.model, d.diagnosis, d.evidence, d.safety, o.resolution, o.rating, o.feedback,
               o.parts_used, o.label
        FROM outcomes o
        JOIN repair_requests r ON r.id = o.request_id
        JOIN LATERAL (SELECT * FROM diagnoses WHERE request_id = r.id ORDER BY version DESC LIMIT 1) d ON true
        WHERE o.approved_for_eval
        ORDER BY o.created_at""")
    return json.loads(json.dumps(rows, default=str))


@app.get("/api/admin/metrics")
def metrics(hours: int = 24, user: dict = Depends(require_role("admin", "reviewer"))):
    return json.loads(json.dumps({**monitoring.metrics_summary(hours), "providers": health_report()}, default=str))


@app.get("/api/admin/audit")
def audit_log(limit: int = 100, user: dict = Depends(require_role("admin"))):
    rows = db.fetch_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (min(limit, 1000),))
    return json.loads(json.dumps(rows, default=str))


# ---------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
