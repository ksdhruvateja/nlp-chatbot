"""On-demand live price lookup (Google Shopping results via SerpAPI).

Google's own pages are not scraped (it violates Google's terms and is blocked by CAPTCHAs);
SerpAPI returns the same Google Shopping results through a supported API.
Results are cached in `price_lookups` and the cheapest relevant offers are upserted into
`products` so the repair plan's normal catalog logic (freshness, compatibility) applies.

Enable with PRICE_LOOKUP_PROVIDER=serpapi and SERPAPI_API_KEY=...
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta

import httpx

from . import db

log = logging.getLogger(__name__)
CACHE_TTL = timedelta(hours=24)
MAX_OFFERS = 5


def enabled() -> bool:
    return os.getenv("PRICE_LOOKUP_PROVIDER", "").lower() == "serpapi" and bool(os.getenv("SERPAPI_API_KEY"))


def build_query(part_type: str, description: str, brand: str | None, models: list[str]) -> str:
    words = description or part_type.replace("_", " ")
    model = models[0] if models else ""
    return " ".join(w for w in (brand or "", model, words, "replacement part") if w).strip()[:200]


def _serpapi(query: str) -> list[dict]:
    r = httpx.get("https://serpapi.com/search.json", timeout=20, params={
        "engine": "google_shopping", "q": query, "gl": os.getenv("PRICE_COUNTRY", "us"), "hl": "en",
        "api_key": os.environ["SERPAPI_API_KEY"]})
    r.raise_for_status()
    offers = []
    for item in r.json().get("shopping_results", []):
        price = item.get("extracted_price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        offers.append({"title": item.get("title", "")[:200], "price": float(price),
                       "merchant": item.get("source", ""), "link": item.get("product_link") or item.get("link"),
                       "product_id": str(item.get("product_id") or item.get("position")),
                       "rating": item.get("rating"), "delivery": item.get("delivery")})
    return offers


def lookup(query: str, force: bool = False) -> list[dict]:
    if not force:
        cached = db.fetch_one("""SELECT results FROM price_lookups WHERE query = %s
                                 AND created_at > now() - %s ORDER BY created_at DESC LIMIT 1""",
                              (query, CACHE_TTL))
        if cached:
            return cached["results"]
    offers = _serpapi(query)
    db.execute("INSERT INTO price_lookups (query, provider, results) VALUES (%s, 'serpapi', %s)",
               (query, db.jsonb(offers)))
    return offers


def refresh_part(part_type: str, description: str, category: str, brand: str | None, models: list[str],
                 force: bool = False) -> int:
    """Look up live prices for one part and upsert them as catalog products. Returns offers stored."""
    if not enabled():
        return 0
    query = build_query(part_type, description, brand, models)
    try:
        offers = lookup(query, force)
    except (httpx.HTTPError, KeyError) as exc:
        log.warning("price lookup failed for %r: %s", query, exc)
        return 0
    # Drop accessories/bundles whose titles don't mention the part at all.
    keywords = [w for w in re.split(r"[_\s]+", part_type.lower()) if len(w) > 2]
    relevant = [o for o in offers if any(k in o["title"].lower() for k in keywords)] or offers
    relevant.sort(key=lambda o: o["price"])
    stored = 0
    with db.connection() as conn:
        for o in relevant[:MAX_OFFERS]:
            # Only claim model compatibility when the listing names the customer's model.
            compatible = [m for m in models if m.upper() in o["title"].upper()]
            conn.execute(
                """INSERT INTO products (sku, name, brand, service_category, part_type, compatible_models,
                       price_cents, currency, price_source, price_verified_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'USD', %s, now())
                   ON CONFLICT (sku) DO UPDATE SET name = excluded.name, price_cents = excluded.price_cents,
                       compatible_models = excluded.compatible_models, price_source = excluded.price_source,
                       price_verified_at = now()""",
                (f"gshop:{o['product_id']}"[:120], o["title"], None, category, part_type, compatible,
                 round(o["price"] * 100), f"Google Shopping via SerpAPI: {o['merchant']} {o['link'] or ''}"[:500]))
            stored += 1
    return stored
