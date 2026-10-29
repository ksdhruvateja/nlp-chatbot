"""Phase 7 - Repair plan and cost enrichment.

Generate evidence-based DIY repair plan -> validate steps, tools, materials and
prerequisites -> retrieve compatible products and verified current prices ->
calculate cost range with explicit assumptions -> add source references,
confidence and limitations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import db, pricing
from ..schemas import Diagnosis
from .retrieval import Evidence
from .safety import SafetyAssessment

PRICE_FRESHNESS = timedelta(days=30)

# Safety prerequisites injected before the first step whenever a warning applies.
PREREQS = {
    "WARN_ELECTRICAL": ("Switch off the circuit at the breaker and confirm it is dead with a non-contact voltage "
                        "tester.", "Non-contact voltage tester"),
    "WARN_WATER": ("Close the water supply valve feeding the fixture/appliance and open a tap to relieve pressure.",
                   "Bucket and towels"),
    "WARN_SCALD": ("Turn the water heater off (or to 'vacation'/pilot) and let the water cool for several hours.",
                   "Heat-resistant gloves"),
    "WARN_GAS_APPLIANCE": ("Turn the gas control to OFF and wait 5 minutes for gas to clear before relighting.",
                           None),
    "WARN_FALL": ("Set up a ladder on firm, level ground and have a helper hold it.", "Stable ladder"),
    "WARN_MOLD": ("Put on an N95 respirator, gloves and eye protection.", "N95 respirator"),
}


def _cost(cents: int) -> float:
    return round(cents / 100, 2)


def find_products(part_type: str, category: str, models: list[str]) -> list[dict]:
    return db.fetch_all(
        """SELECT sku, name, brand, part_type, compatible_models, price_cents, currency, price_source,
                  price_verified_at
           FROM products
           WHERE part_type = %s AND service_category = %s
             AND (cardinality(compatible_models) = 0 OR compatible_models && %s::text[])
           ORDER BY (cardinality(compatible_models) > 0) DESC, price_cents""",
        (part_type, category, [m.upper() for m in models]))


def build_plan(diag: Diagnosis, evidence: list[Evidence], assessment: SafetyAssessment, facts: dict,
               service_category: str, media_refs: dict[str, str], force_live_prices: bool = False) -> dict:
    by_ref = {e.ref: e for e in evidence}
    warning_codes = {w.code for w in assessment.warnings}
    limitations: list[str] = list(diag.uncertainty_notes)

    # ---- validate steps
    steps = []
    for step in diag.proposed_steps:
        steps.append({"title": step.title, "detail": step.detail, "tools": step.tools, "materials": step.materials,
                      "hazard_tags": step.hazard_tags, "sources": step.evidence_refs,
                      "verified": bool(step.evidence_refs)})
    unverified = [s["title"] for s in steps if not s["verified"]]
    if unverified:
        limitations.append(f"{len(unverified)} step(s) are general guidance not tied to a specific manual passage.")

    # ---- prerequisites (safety first, then model-provided)
    prerequisites, tools = [], []
    for code in PREREQS:
        if code in warning_codes:
            text, tool = PREREQS[code]
            prerequisites.append(text)
            if tool:
                tools.append(tool)
    prerequisites += [p for p in diag.prerequisites if p not in prerequisites]
    for s in steps:
        tools += s["tools"]
    tools = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
    materials = list(dict.fromkeys(m.strip() for s in steps for m in s["materials"] if m.strip()))

    # ---- products, prices and cost range
    now = datetime.now(timezone.utc)
    parts, assumptions = [], []
    low = high = 0
    models = facts.get("model_numbers") or []
    for need in diag.parts_needed:
        options = find_products(need.part_type, service_category, models)
        fresh = [o for o in options if now - o["price_verified_at"] <= PRICE_FRESHNESS]
        if force_live_prices or not fresh:
            # No fresh catalog price (or the customer asked): fetch live market prices on demand.
            brand = (facts.get("brands") or [None])[0]
            if pricing.refresh_part(need.part_type, need.description, service_category, brand, models,
                                    force=force_live_prices):
                options = find_products(need.part_type, service_category, models)
                fresh = [o for o in options if now - o["price_verified_at"] <= PRICE_FRESHNESS]
        entry = {"part_type": need.part_type, "description": need.description, "quantity": need.quantity,
                 "options": [{"sku": o["sku"], "name": o["name"], "brand": o["brand"],
                              "price": _cost(o["price_cents"]), "currency": o["currency"],
                              "model_specific": bool(o["compatible_models"]),
                              "price_source": o["price_source"],
                              "price_verified_at": o["price_verified_at"].isoformat(),
                              "price_verified": o in fresh,
                              "live_market_price": o["sku"].startswith("gshop:"),
                              "compatibility_verified": bool(o["compatible_models"])} for o in options[:4]]}
        parts.append(entry)
        if fresh:
            prices = [o["price_cents"] for o in fresh]
            low += min(prices) * need.quantity
            high += max(prices) * need.quantity
            if not any(o["compatible_models"] for o in fresh):
                assumptions.append(f"{need.part_type}: universal-fit products assumed compatible; confirm against "
                                   "your model before buying.")
        elif options:
            assumptions.append(f"{need.part_type}: price not verified in the last {PRICE_FRESHNESS.days} days; "
                               "excluded from the estimate.")
        else:
            assumptions.append(f"{need.part_type}: no compatible product in catalog; excluded from the estimate.")
    if not diag.parts_needed:
        assumptions.append("No replacement parts expected.")
    assumptions.append("Excludes tools you may need to buy, taxes, shipping and any professional labor.")
    if not models:
        assumptions.append("Model number unknown; part compatibility is based on service category only.")
    if any(o["live_market_price"] and not o["compatibility_verified"] for p in parts for o in p["options"]):
        assumptions.append("Some prices are live Google Shopping listings; confirm the part fits your model "
                           "before buying.")

    sources = [by_ref[r].citation() for r in
               sorted({r for s in steps for r in s["sources"]} | {r for c in diag.likely_causes for r in c.evidence_refs})
               if r in by_ref]
    media_sources = sorted({r for s in steps for r in s["sources"] if r in media_refs})

    confidence = diag.confidence
    if unverified:
        confidence = round(confidence * (1 - 0.1 * len(unverified) / max(1, len(steps))), 2)
    limitations.append("FixBridge guidance is informational; stop and contact a professional if anything differs "
                       "from what is described.")
    return {
        "title": diag.summary,
        "skill_level": diag.skill_level,
        "estimated_time_minutes": diag.estimated_time_minutes,
        "prerequisites": prerequisites,
        "tools": tools,
        "materials": materials,
        "steps": steps,
        "parts": parts,
        "cost_estimate": {"currency": "USD", "low": _cost(low), "high": _cost(high), "assumptions": assumptions},
        "sources": sources,
        "media_sources": media_sources,
        "confidence": confidence,
        "limitations": limitations,
    }
