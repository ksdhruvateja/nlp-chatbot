"""Cross-cutting monitoring: model call logging, cost tracking and alerts."""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("fixbridge")

# USD per 1M tokens (input, output). Update as provider pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
}


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = PRICING.get(model)
    if price is None or input_tokens is None:
        return None  # unknown pricing (e.g. self-hosted local model)
    return round(input_tokens / 1e6 * price[0] + (output_tokens or 0) / 1e6 * price[1], 6)


def log_model_call(request_id, provider: str, model: str, success: bool, latency_ms: int,
                   input_tokens: int | None = None, output_tokens: int | None = None,
                   error: str | None = None) -> None:
    cost = estimate_cost(model, input_tokens, output_tokens)
    log.info("model_call provider=%s model=%s ok=%s latency_ms=%s cost=%s err=%s",
             provider, model, success, latency_ms, cost, error)
    db.execute(
        """INSERT INTO model_calls (request_id, provider, model, success, latency_ms,
                                    input_tokens, output_tokens, cost_usd, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (request_id, provider, model, success, latency_ms, input_tokens, output_tokens, cost,
         (error or "")[:2000] or None),
    )


def alert(severity: str, kind: str, message: str, details: dict | None = None) -> None:
    (log.error if severity == "critical" else log.warning)("ALERT[%s] %s: %s", severity, kind, message)
    db.execute("INSERT INTO alerts (severity, kind, message, details) VALUES (%s, %s, %s, %s)",
               (severity, kind, message, db.jsonb(details or {})))
    # Hook point: forward to PagerDuty / Slack / email here.


def metrics_summary(hours: int = 24) -> dict:
    calls = db.fetch_all(
        """SELECT provider, model, count(*) AS calls,
                  count(*) FILTER (WHERE success) AS ok,
                  round(avg(latency_ms)) AS avg_latency_ms,
                  coalesce(sum(cost_usd), 0)::float AS cost_usd
           FROM model_calls WHERE created_at > now() - make_interval(hours => %s)
           GROUP BY provider, model ORDER BY calls DESC""", (hours,))
    statuses = db.fetch_all(
        """SELECT status, count(*) AS n FROM repair_requests
           WHERE created_at > now() - make_interval(hours => %s) GROUP BY status""", (hours,))
    alerts = db.fetch_all("SELECT severity, kind, message, created_at FROM alerts ORDER BY id DESC LIMIT 20")
    return {"window_hours": hours, "model_calls": calls,
            "requests_by_status": {r["status"]: r["n"] for r in statuses}, "recent_alerts": alerts}
