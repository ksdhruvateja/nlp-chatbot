"""Live pricing (SerpAPI mocked) wired into repair plans."""
import httpx

from app import db, pricing
from app.pipeline import runner
from tests.conftest import FakeAdapter
from tests.test_flow import get, submit

OFFERS = {"shopping_results": [
    {"title": "Universal Dishwasher Drain Pump Assembly", "extracted_price": 39.99, "source": "Store A",
     "product_link": "https://a.test/p1", "product_id": "111"},
    {"title": "Drain Pump for ACME ADW5000X dishwasher", "extracted_price": 54.50, "source": "Store B",
     "product_link": "https://b.test/p2", "product_id": "222"},
    {"title": "Dishwasher rack wheels", "extracted_price": 9.99, "source": "Store C", "product_id": "333"},
    {"title": "No price item", "source": "Store D", "product_id": "444"}]}


def pump_diag(payload):
    ref = payload["retrieved_evidence"][0]["ref"]
    return {"summary": "The drain pump is jammed or failed.", "confidence": 0.8, "recommended_action": "diy",
            "likely_causes": [{"cause": "Failed drain pump", "likelihood": 0.7, "evidence_refs": [ref]}],
            "proposed_steps": [{"title": "Clean the filter", "detail": "Rinse the filter.", "evidence_refs": [ref]}],
            "parts_needed": [{"part_type": "drain_pump", "description": "dishwasher drain pump"}]}


def test_live_prices_used_when_catalog_has_none(client, customer, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return httpx.Response(200, json=OFFERS, request=httpx.Request("GET", url))
    monkeypatch.setenv("PRICE_LOOKUP_PROVIDER", "serpapi")
    monkeypatch.setenv("SERPAPI_API_KEY", "test")
    monkeypatch.setattr(pricing.httpx, "get", fake_get)

    req = submit(client, customer, "Dishwasher ADW5000X won't drain, pump hums.", "appliance")
    runner.run_pending(cloud=FakeAdapter(pump_diag))
    plan = get(client, customer, req["id"])["result"]["repair_plan"]
    opts = plan["parts"][0]["options"]
    assert calls and calls[0]["engine"] == "google_shopping"
    assert {o["price"] for o in opts} == {39.99, 54.5}                  # irrelevant + unpriced listings dropped
    assert all(o["live_market_price"] for o in opts)
    assert plan["cost_estimate"]["low"] == 39.99
    assert any("Google Shopping" in a for a in plan["cost_estimate"]["assumptions"])

    # Customer asks for fresh prices -> forced lookup, new version.
    OFFERS["shopping_results"][0]["extracted_price"] = 35.00
    r = client.post(f"/api/repair-requests/{req['id']}/refresh-prices", headers=customer["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["result"]["version"] == 2
    assert r.json()["result"]["repair_plan"]["cost_estimate"]["low"] == 35.0
    assert len(calls) == 2


def test_refresh_prices_disabled_without_key(client, customer, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    req = submit(client, customer, "Toilet keeps running constantly overnight.")
    r = client.post(f"/api/repair-requests/{req['id']}/refresh-prices", headers=customer["headers"])
    assert r.status_code == 503


def test_admin_review_crawled_documents(client, admin):
    from app import ingestion
    res = ingestion.ingest(b"# Filter\n\nClean the dishwasher filter monthly to keep it draining well.", "x.md",
                           ingestion.DocumentMeta(title="Crawled", source="https://x.test/m.pdf",
                                                  permission="manufacturer_public", approved_by="crawler",
                                                  service_category="appliance", version="a1", brand="Zeta",
                                                  review_status="pending"))
    pending = client.get("/api/admin/knowledge/pending?brand=Zeta", headers=admin).json()
    assert [p["id"] for p in pending] == [res["document_id"]]
    r = client.post("/api/admin/knowledge/review", headers=admin, json={"action": "approve", "brand": "Zeta"})
    assert r.json() == {"updated": 1}
    doc = db.fetch_one("SELECT active, review_status FROM knowledge_documents WHERE id = %s", (res["document_id"],))
    assert doc == {"active": True, "review_status": "approved"}
