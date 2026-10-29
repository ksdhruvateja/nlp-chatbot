"""Phase 4 - Repair knowledge retrieval.

Build search query from problem and context -> search repair knowledge index
(semantic search using embeddings + keyword search using exact terms and model
numbers) -> filter by service, brand, model, source and version -> rank relevant
passages -> return source-cited evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import db
from ..embeddings import embed
from .context import PreparedContext

CANDIDATES = 30
TOP_K = 8
SEMANTIC_WEIGHT = 0.5
MIN_SCORE = 0.25
# Category pairs where knowledge from one is relevant to the other.
RELATED = {"gas": ["hvac", "appliance", "plumbing"], "hvac": ["gas", "electrical"],
           "appliance": ["electrical", "plumbing", "gas"], "plumbing": ["gas"],
           "electrical": ["appliance"], "general": []}

_STOP = set("the a an is are was were it its this that and or but to of in on for with my our i we me "
            "have has had be been not no do does did can could would should there their they them from at "
            "by as if so when what which who how about just very really also into out up keeps keep every few "
            "minutes minute itself since after before time times day days morning night constantly still "
            "anymore some any all lot help please thanks".split())


@dataclass
class SearchQuery:
    text: str
    keywords: list[str]
    service_category: str
    brands: list[str]
    model_numbers: list[str]


@dataclass
class Evidence:
    ref: str                  # stable citation id used by the model, e.g. "K3"
    passage_id: str
    document_id: str
    title: str
    source: str
    version: str
    page: int | None
    section: str
    content: str
    models: list[str]
    score: float
    match: dict               # which signals matched (semantic/keyword/model/brand)

    def citation(self) -> dict:
        return {"ref": self.ref, "title": self.title, "source": self.source, "version": self.version,
                "page": self.page, "section": self.section, "passage_id": self.passage_id}


def build_query(ctx: PreparedContext) -> SearchQuery:
    text = ctx.text_for_search()
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", ctx.request["description"].lower()) if w not in _STOP]
    keywords = list(dict.fromkeys(words + [c.lower() for c in ctx.facts.get("error_codes", [])]))[:20]
    return SearchQuery(text=text[:4000], keywords=keywords, service_category=ctx.request["service_category"],
                       brands=ctx.facts.get("brands", []), model_numbers=ctx.facts.get("model_numbers", []))


def _filters(q: SearchQuery) -> tuple[str, dict]:
    cats = [q.service_category, *RELATED.get(q.service_category, [])]
    # Brand/model filter: keep generic passages plus ones for the user's brand/model; drop other brands' docs.
    where = """d.active AND d.review_status = 'approved' AND p.service_category = ANY(%(cats)s)
               AND (p.brand IS NULL OR cardinality(%(brands)s::text[]) = 0 OR lower(p.brand) = ANY(%(brands)s))"""
    return where, {"cats": cats, "brands": [b.lower() for b in q.brands]}


def search(q: SearchQuery, top_k: int = TOP_K) -> list[Evidence]:
    where, params = _filters(q)
    params |= {"vec": db.vector_literal(embed(q.text)), "n": CANDIDATES,
               "tsq": " OR ".join(q.keywords + q.model_numbers) or q.service_category,
               "models": q.model_numbers}

    semantic = db.fetch_all(f"""
        SELECT p.id, 1 - (p.embedding <=> %(vec)s::vector) AS sim
        FROM knowledge_passages p JOIN knowledge_documents d ON d.id = p.document_id
        WHERE {where} ORDER BY p.embedding <=> %(vec)s::vector LIMIT %(n)s""", params)
    keyword = db.fetch_all(f"""
        SELECT p.id, ts_rank_cd(p.tsv, websearch_to_tsquery('english', %(tsq)s)) AS rank
        FROM knowledge_passages p JOIN knowledge_documents d ON d.id = p.document_id
        WHERE {where} AND (p.tsv @@ websearch_to_tsquery('english', %(tsq)s)
                           OR p.model_numbers && %(models)s::text[])
        ORDER BY rank DESC LIMIT %(n)s""", params)

    # Weighted fusion of max-normalized semantic and keyword scores (keeps score magnitude,
    # unlike pure rank fusion), plus boosts for exact model/brand matches.
    scores: dict = {}
    signals: dict = {}
    max_sim = max((float(r["sim"]) for r in semantic), default=0) or 1
    max_kw = max((float(r["rank"]) for r in keyword), default=0) or 1
    for r in semantic:
        scores[r["id"]] = scores.get(r["id"], 0) + SEMANTIC_WEIGHT * max(0.0, float(r["sim"])) / max_sim
        signals.setdefault(r["id"], {})["semantic"] = round(float(r["sim"]), 4)
    for r in keyword:
        scores[r["id"]] = scores.get(r["id"], 0) + (1 - SEMANTIC_WEIGHT) * float(r["rank"]) / max_kw
        signals.setdefault(r["id"], {})["keyword"] = round(float(r["rank"]), 4)
    if not scores:
        return []

    rows = db.fetch_all("""
        SELECT p.id, p.document_id, p.page, p.section, p.content, p.model_numbers, p.brand, p.version,
               p.service_category, d.title, d.source
        FROM knowledge_passages p JOIN knowledge_documents d ON d.id = p.document_id
        WHERE p.id = ANY(%s)""", (list(scores),))
    models = set(q.model_numbers)
    brands = {b.lower() for b in q.brands}
    ranked = []
    for r in rows:
        s = scores[r["id"]]
        sig = signals[r["id"]]
        if models & set(r["model_numbers"] or []):
            s *= 1.5
            sig["model_match"] = True
        elif r["model_numbers"]:
            s *= 0.6   # manual for a specific model the customer hasn't said they own
        if r["brand"] and r["brand"].lower() in brands:
            s *= 1.2
            sig["brand_match"] = True
        if r["service_category"] == q.service_category:
            s *= 1.1
        if s < MIN_SCORE and not sig.get("model_match"):
            continue  # weak hits are noise
        ranked.append((s, r, sig))
    ranked.sort(key=lambda t: t[0], reverse=True)

    return [Evidence(ref=f"K{i + 1}", passage_id=str(r["id"]), document_id=str(r["document_id"]),
                     title=r["title"], source=r["source"], version=r["version"], page=r["page"],
                     section=r["section"], content=r["content"],
                     models=list(r["model_numbers"] or []), score=round(s, 5), match=sig)
            for i, (s, r, sig) in enumerate(ranked[:top_k])]


def retrieve(ctx: PreparedContext) -> tuple[SearchQuery, list[Evidence]]:
    q = build_query(ctx)
    return q, search(q)
