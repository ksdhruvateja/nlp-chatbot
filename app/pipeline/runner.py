"""Runs a repair request through phases 3-8 and handles the two customer decision
points (clarification answers and safety choice). Also hosts the DB-backed job queue
that gives us background processing, retries and crash recovery."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict

from .. import db, monitoring
from ..ai.orchestrator import analyze
from ..config import get_settings
from ..schemas import Diagnosis, SafetyWarning
from .context import prepare_context
from .repair_plan import build_plan
from .retrieval import Evidence, retrieve
from .safety import (SafetyAssessment, apply_acknowledgement, assess, emergency_check, generate_questions,
                     safe_alternatives, validate_evidence)

log = logging.getLogger(__name__)


class RetryableError(Exception):
    pass


# ---------------------------------------------------------------- persistence helpers
def set_status(request_id, status: str, stage: str | None = None, error: str | None = None) -> None:
    db.execute("""UPDATE repair_requests SET status = %s, stage = coalesce(%s, stage), error = %s,
                  updated_at = now() WHERE id = %s""", (status, stage, error, request_id))


def save_version(request_id, outcome: str, response: dict, *, diagnosis: Diagnosis | None = None,
                 evidence: list[Evidence] | None = None, safety: dict | None = None, plan: dict | None = None,
                 provider: str | None = None, model: str | None = None) -> int:
    """Phase 8: save versioned diagnosis, evidence, safety decision and status."""
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s::text))", (str(request_id),))
        version = conn.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM diagnoses WHERE request_id = %s",
                               (request_id,)).fetchone()["v"]
        response = {**response, "request_id": str(request_id), "version": version, "outcome": outcome}
        response = validate_response(response)
        conn.execute(
            """INSERT INTO diagnoses (request_id, version, outcome, provider, model, diagnosis, evidence, safety,
                                      repair_plan, response)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (request_id, version, outcome, provider, model,
             db.jsonb(diagnosis.model_dump()) if diagnosis else None,
             db.jsonb([asdict(e) for e in evidence or []]), db.jsonb(safety or {}),
             db.jsonb(plan) if plan else None, db.jsonb(response)))
    return version


def validate_response(response: dict) -> dict:
    """Return validated API response: enforce the shape the FixBridge UI relies on."""
    from ..api_models import DiagnosisResponse
    return DiagnosisResponse.model_validate(response).model_dump(mode="json")


def latest_version(request_id) -> dict | None:
    return db.fetch_one("SELECT * FROM diagnoses WHERE request_id = %s ORDER BY version DESC LIMIT 1", (request_id,))


def _history(request: dict, summary: str, details: dict) -> None:
    db.execute("""INSERT INTO service_history (property_id, request_id, event_type, summary, details, source)
                  VALUES (%s, %s, 'diagnosis', %s, %s, 'system')""",
               (request["property_id"], request["id"], summary[:500], db.jsonb(details)))


def _base(diag: Diagnosis | None, evidence: list[Evidence], provider=None, model=None) -> dict:
    return {
        "summary": diag.summary if diag else "",
        "likely_causes": [c.model_dump() for c in diag.likely_causes] if diag else [],
        "confidence": diag.confidence if diag else None,
        "sources": [e.citation() for e in evidence],
        "provider": provider, "model": model,
    }


# ---------------------------------------------------------------- main pipeline
def run_diagnosis(request_id: str, cloud=None, local=None) -> str:
    s = get_settings()
    req = db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,))
    if req["status"] in ("cancelled",):
        return "skipped"
    set_status(request_id, "processing", "context_preparation")

    # Phase 3
    ctx = prepare_context(request_id)

    # Emergencies short-circuit before any model call.
    emergency = emergency_check(ctx.facts)
    if emergency:
        alt = safe_alternatives(None, emergency, req["service_category"])
        save_version(request_id, "professional", {
            **_base(None, []), "summary": emergency.warnings[0].message, "safety": emergency.to_dict(),
            "professional_recommendation": alt, "status": "blocked"}, safety=emergency.to_dict())
        _history(req, f"Emergency detected: {emergency.warnings[0].title}", {"codes": [w.code for w in emergency.warnings]})
        set_status(request_id, "blocked", "done")
        monitoring.alert("warning", "emergency_detected", emergency.warnings[0].title, {"request_id": str(request_id)})
        return "blocked"

    # Phase 4
    set_status(request_id, "processing", "knowledge_retrieval")
    _, evidence = retrieve(ctx)

    # Phase 5
    set_status(request_id, "processing", "ai_analysis")
    orch = analyze(ctx, evidence, cloud=cloud, local=local)
    if not orch.ok:
        raise RetryableError(orch.error or "AI unavailable")
    diag, result = orch.diagnosis, orch.result
    provider, model = result.provider, result.model

    # Phase 6
    set_status(request_id, "processing", "safety_validation")
    check = validate_evidence(diag, evidence, orch.media_refs, ctx.facts)
    base = _base(diag, evidence, provider, model)
    base["evidence_check"] = {"supported_refs": check.supported_refs, "invalid_refs": check.invalid_refs,
                              "inapplicable_refs": check.inapplicable_refs, "reasons": check.reasons}
    extra = {"media_refs": orch.media_refs, "attempts": orch.attempts}

    if not check.sufficient:
        if req["clarification_round"] >= s.max_clarification_rounds:
            assessment = assess(diag, ctx.facts, req["service_category"])
            alt = safe_alternatives(diag, assessment, req["service_category"])
            resp = {**base, "status": "completed", "professional_recommendation": alt,
                    "limitations": ["We couldn't gather enough reliable evidence to give safe DIY steps after "
                                    f"{req['clarification_round']} rounds of questions."] + check.reasons}
            save_version(request_id, "professional", resp, diagnosis=diag, evidence=evidence,
                         safety={**assessment.to_dict(), **extra}, provider=provider, model=model)
            _history(req, f"Diagnosis inconclusive; professional recommended: {diag.summary}", {})
            set_status(request_id, "completed", "done")
            return "professional"
        round_no = req["clarification_round"] + 1
        questions = generate_questions(diag, check, ctx.facts, round_no)
        db.execute("""INSERT INTO clarifications (request_id, round, questions) VALUES (%s, %s, %s)
                      ON CONFLICT (request_id, round) DO UPDATE SET questions = excluded.questions""",
                   (request_id, round_no, db.jsonb([q.model_dump() for q in questions])))
        resp = {**base, "status": "needs_clarification",
                "clarification": {"round": round_no, "max_rounds": s.max_clarification_rounds,
                                  "questions": [q.model_dump() for q in questions]},
                "limitations": check.reasons}
        save_version(request_id, "clarification", resp, diagnosis=diag, evidence=evidence,
                     safety=extra, provider=provider, model=model)
        set_status(request_id, "needs_clarification", "awaiting_customer")
        return "clarification"

    assessment = assess(diag, ctx.facts, req["service_category"])
    safety = {**assessment.to_dict(), **extra}
    if assessment.level == "safe":
        return _finish_diy(req, diag, evidence, assessment, ctx.facts, orch.media_refs, base, safety, provider, model)

    if assessment.level == "warning_required":
        resp = {**base, "status": "awaiting_safety_choice", "safety": assessment.to_dict()}
        save_version(request_id, "safety_warning", resp, diagnosis=diag, evidence=evidence, safety=safety,
                     provider=provider, model=model)
        set_status(request_id, "awaiting_safety_choice", "awaiting_customer")
        return "safety_warning"

    alt = safe_alternatives(diag, assessment, req["service_category"])
    resp = {**base, "status": "completed", "safety": assessment.to_dict(), "professional_recommendation": alt}
    save_version(request_id, "professional", resp, diagnosis=diag, evidence=evidence, safety=safety,
                 provider=provider, model=model)
    _history(req, f"Professional recommended: {diag.summary}", {"codes": [w.code for w in assessment.warnings]})
    set_status(request_id, "completed", "done")
    return "professional"


def _finish_diy(req, diag, evidence, assessment: SafetyAssessment, facts, media_refs, base, safety, provider,
                model, acknowledged: bool = False) -> str:
    plan = build_plan(diag, evidence, assessment, facts, req["service_category"], media_refs)
    resp = {**base, "status": "completed", "safety": {**assessment.to_dict(), "acknowledged": acknowledged},
            "repair_plan": plan, "confidence": plan["confidence"], "limitations": plan["limitations"]}
    save_version(req["id"], "diy_plan", resp, diagnosis=diag, evidence=evidence, safety=safety, plan=plan,
                 provider=provider, model=model)
    _history(req, f"DIY plan provided: {diag.summary}", {"cost_estimate": plan["cost_estimate"]})
    set_status(req["id"], "completed", "done")
    return "diy_plan"


def reprice(request_id: str) -> int:
    """Rebuild the latest DIY plan with live prices (customer asked). Saves a new version."""
    req = db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,))
    last = latest_version(request_id)
    if not last or last["outcome"] != "diy_plan":
        raise ValueError("Prices can only be refreshed for a DIY repair plan")
    diag = Diagnosis.model_validate(last["diagnosis"])
    evidence = [Evidence(**e) for e in last["evidence"]]
    stored = last["safety"]
    assessment = SafetyAssessment(stored["level"], [SafetyWarning(**w) for w in stored.get("warnings", [])],
                                  stored.get("prohibited_steps", []), stored.get("emergency", False))
    # Keep the same (possibly safety-filtered) steps the customer already has.
    diag = diag.model_copy(update={"proposed_steps": [
        s for s in diag.proposed_steps if s.title in {p["title"] for p in last["repair_plan"]["steps"]}]})
    plan = build_plan(diag, evidence, assessment, prepare_context(request_id).facts, req["service_category"],
                      stored.get("media_refs", {}), force_live_prices=True)
    resp = {**last["response"], "repair_plan": plan, "confidence": plan["confidence"],
            "limitations": plan["limitations"]}
    for k in ("request_id", "version", "outcome"):
        resp.pop(k, None)
    return save_version(request_id, "diy_plan", resp, diagnosis=diag, evidence=evidence, safety=stored, plan=plan,
                        provider=last["provider"], model=last["model"])


# ---------------------------------------------------------------- customer decisions
def apply_safety_choice(request_id: str, user_id: str, choice: str) -> str:
    req = db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,))
    last = latest_version(request_id)
    if req["status"] != "awaiting_safety_choice" or not last or last["outcome"] != "safety_warning":
        raise ValueError("This request is not awaiting a safety decision")
    diag = Diagnosis.model_validate(last["diagnosis"])
    evidence = [Evidence(**e) for e in last["evidence"]]
    stored = last["safety"]
    assessment = SafetyAssessment(stored["level"], [SafetyWarning(**w) for w in stored["warnings"]],
                                  stored["prohibited_steps"], stored.get("emergency", False))
    db.execute("""INSERT INTO safety_acknowledgements (request_id, diagnosis_version, user_id, choice, warning_codes)
                  VALUES (%s, %s, %s, %s, %s)""",
               (request_id, last["version"], user_id, choice, [w.code for w in assessment.warnings]))
    base = _base(diag, evidence, last["provider"], last["model"])

    if choice == "cancel":  # stop DIY workflow
        alt = safe_alternatives(diag, assessment, req["service_category"])
        save_version(request_id, "cancelled", {**base, "status": "cancelled", "safety": assessment.to_dict(),
                                               "professional_recommendation": alt},
                     diagnosis=diag, evidence=evidence, safety=stored, provider=last["provider"], model=last["model"])
        _history(req, "Customer declined DIY after safety warning", {})
        set_status(request_id, "cancelled", "done")
        return "cancelled"

    prop = db.fetch_one("SELECT passport FROM properties WHERE id = %s", (req["property_id"],))
    filtered, permitted, reason = apply_acknowledgement(diag, assessment, prop["passport"])
    if not permitted:  # block dangerous step-by-step instruction
        alt = safe_alternatives(diag, assessment, req["service_category"])
        save_version(request_id, "professional", {**base, "status": "completed", "safety": assessment.to_dict(),
                                                  "professional_recommendation": alt, "limitations": [reason]},
                     diagnosis=diag, evidence=evidence, safety=stored, provider=last["provider"], model=last["model"])
        _history(req, f"DIY blocked after acknowledgement: {reason}", {})
        set_status(request_id, "completed", "done")
        return "professional"

    ctx_facts = prepare_context(request_id).facts
    return _finish_diy(req, filtered, evidence, assessment, ctx_facts, stored.get("media_refs", {}), base,
                       stored, last["provider"], last["model"], acknowledged=True)


# ---------------------------------------------------------------- job queue
def enqueue(request_id: str, delay_s: int = 0) -> None:
    db.execute("""INSERT INTO jobs (request_id, run_after)
                  SELECT %(r)s, now() + make_interval(secs => %(d)s)
                  WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE request_id = %(r)s AND status IN ('queued', 'running'))""",
               {"r": request_id, "d": delay_s})


def claim_job() -> dict | None:
    with db.connection() as conn:
        # Recover jobs orphaned by a crashed worker.
        conn.execute("""UPDATE jobs SET status = 'queued' WHERE status = 'running'
                        AND updated_at < now() - interval '10 minutes'""")
        return conn.execute("""
            UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = now()
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()
                        ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""").fetchone()


def process_job(job: dict, cloud=None, local=None) -> None:
    rid = job["request_id"]
    try:
        outcome = run_diagnosis(rid, cloud=cloud, local=local)
        db.execute("UPDATE jobs SET status = 'done', updated_at = now() WHERE id = %s", (job["id"],))
        log.info("request %s -> %s", rid, outcome)
    except Exception as exc:  # noqa: BLE001
        retryable = isinstance(exc, RetryableError) or not isinstance(exc, (ValueError, KeyError, TypeError))
        err = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, RetryableError):
            log.warning("job %s attempt %s failed: %s", job["id"], job["attempts"], exc)
        else:
            log.exception("job %s failed", job["id"])
        if retryable and job["attempts"] < job["max_attempts"]:
            delay = 30 * 2 ** (job["attempts"] - 1)
            db.execute("""UPDATE jobs SET status = 'queued', last_error = %s, updated_at = now(),
                          run_after = now() + make_interval(secs => %s) WHERE id = %s""", (err, delay, job["id"]))
            set_status(rid, "processing", "waiting_retry", "Diagnosis temporarily unavailable; retrying shortly.")
            return
        db.execute("UPDATE jobs SET status = 'failed', last_error = %s, updated_at = now() WHERE id = %s",
                   (err, job["id"]))
        try:
            save_version(rid, "failure", {**_base(None, []), "status": "failed",
                                          "summary": "We couldn't complete the diagnosis right now. Please try again "
                                                     "later or request a professional visit.",
                                          "error": {"retryable": retryable, "message": "temporarily unavailable"
                                                    if retryable else "processing error"}})
        finally:
            set_status(rid, "failed", "done", "Diagnosis failed")
            monitoring.alert("critical", "job_failed", f"Repair request {rid} failed permanently", {"error": err})


def run_pending(limit: int = 100, cloud=None, local=None) -> int:
    n = 0
    while n < limit and (job := claim_job()):
        process_job(job, cloud=cloud, local=local)
        n += 1
    return n


class Worker(threading.Thread):
    def __init__(self, poll_s: float = 1.0):
        super().__init__(daemon=True, name="fixbridge-worker")
        self.poll_s = poll_s
        self._halt = threading.Event()

    def run(self) -> None:
        log.info("worker started")
        while not self._halt.is_set():
            try:
                if not run_pending(limit=10):
                    self._halt.wait(self.poll_s)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("worker loop error")
                time.sleep(5)

    def stop(self) -> None:
        self._halt.set()
