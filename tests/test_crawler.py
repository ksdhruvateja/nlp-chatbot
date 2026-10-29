"""Crawler pipeline against a simulated manufacturer site (no real network)."""
import io

import httpx
import pytest
from reportlab.pdfgen import canvas

from app import crawler, db
from app.pipeline.retrieval import SearchQuery, search


def make_pdf(lines: list[str], year: int) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setCreator("test")
    y = 800
    for line in lines + [f"(c) {year} Acme Appliances. Printed in USA."]:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


MANUAL = make_pdf(["Owner's Manual - Acme Dishwasher ADW5000X", "Troubleshooting",
                   "Dishwasher not draining: remove the filter under the lower spray arm and rinse it.",
                   "Check the drain hose for kinks behind the dishwasher and under the sink.",
                   "If water remains, check the drain pump cover for broken glass or debris.",
                   "Model ADW5000X uses a cylindrical fine filter that should be cleaned monthly."] * 2, 2019)
OLD_MANUAL = make_pdf(["Owner's Manual - Acme Dishwasher ADW1000", "Clean the filter monthly. " * 20], 2003)
SPEC = make_pdf(["Specification Sheet ADW5000X", "Dimensions 24 in wide. " * 20], 2020)

SITE = {
    "/robots.txt": (200, b"User-agent: *\nDisallow: /private/\n", "text/plain"),
    "/sitemap.xml": (200, b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://www.acme.test/sitemap-products.xml</loc></sitemap>
        <sitemap><loc>https://evil.test/sitemap.xml</loc></sitemap></sitemapindex>""", "application/xml"),
    "/sitemap-products.xml": (200, b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://www.acme.test/product/acme-dishwasher-ADW5000X</loc></url>
        <url><loc>https://www.acme.test/product/acme-dishwasher-ADW1000</loc></url>
        <url><loc>https://www.acme.test/private/product/secret-ADW9999</loc></url>
        <url><loc>https://www.acme.test/blog/post</loc></url></urlset>""", "application/xml"),
    "/product/acme-dishwasher-ADW5000X": (200, b"""<html><title>Acme Dishwasher ADW5000X</title><body>
        <a href="https://cdn.acme.test/docs/owners-ADW5000X.pdf">Owner's Manual</a>
        <a href="https://cdn.acme.test/docs/spec-ADW5000X.pdf">Spec Sheet</a>
        <a href="https://other-site.test/manual.pdf">Third-party manual</a></body></html>""", "text/html"),
    "/product/acme-dishwasher-ADW1000": (200, b"""<html><title>Acme Dishwasher ADW1000</title>
        <a href="https://cdn.acme.test/docs/owners-ADW1000.pdf">Use and care guide</a></html>""", "text/html"),
    "/docs/owners-ADW5000X.pdf": (200, MANUAL, "application/pdf"),
    "/docs/spec-ADW5000X.pdf": (200, SPEC, "application/pdf"),
    "/docs/owners-ADW1000.pdf": (200, OLD_MANUAL, "application/pdf"),
}


@pytest.fixture
def fake_site(monkeypatch):
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        assert request.headers["user-agent"].startswith("FixBridgeManualBot/")
        if request.url.host not in ("www.acme.test", "cdn.acme.test"):
            return httpx.Response(404)
        status, body, ctype = SITE.get(request.url.path, (404, b"", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    monkeypatch.setattr(crawler, "MIN_DELAY_S", 0)
    monkeypatch.setitem(crawler.SOURCES, "acme", crawler.Source(
        name="acme", brand="Acme", sitemaps=("https://www.acme.test/sitemap.xml",),
        page_domains=("www.acme.test",), pdf_domains=("cdn.acme.test",), product_url=r"/product/",
        model_from_url=r"-([A-Z0-9]{5,})$"))
    f = crawler.Fetcher(contact="test@example.com")
    f.client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": f.user_agent})
    return f, hits


def test_crawl_ingests_recent_manuals_as_pending(fake_site):
    f, hits = fake_site
    stats = crawler.crawl("acme", max_pdfs=10, max_pages=10, fetcher=f)
    assert stats["ingested"] == 1 and stats["failed"] == 0
    assert not any("evil.test" in h or "other-site.test" in h or "/private/" in h for h in hits)

    results = {r["url"].rsplit("/", 1)[-1]: r["result"] for r in
               db.fetch_all("SELECT url, result FROM crawl_urls WHERE source = 'acme' AND kind = 'pdf'")}
    assert results["owners-ADW1000.pdf"].startswith("skipped:too_old:2003")
    assert "spec-ADW5000X.pdf" not in results                        # filtered by link text before download

    doc = db.fetch_one("SELECT * FROM knowledge_documents WHERE source LIKE %s", ("%owners-ADW5000X.pdf",))
    assert doc["review_status"] == "pending" and doc["active"] is False
    assert doc["permission"] == "manufacturer_public" and doc["published_year"] == 2019
    assert doc["doc_type"] in ("owners_manual", "troubleshooting") and "ADW5000X" in doc["model_numbers"]
    assert doc["service_category"] == "appliance" and doc["raw_storage_key"]

    q = SearchQuery("dishwasher not draining filter", ["dishwasher", "draining", "filter"], "appliance",
                    ["acme"], ["ADW5000X"])
    assert not any(e.document_id == str(doc["id"]) for e in search(q))     # pending: not used for answers
    db.execute("UPDATE knowledge_documents SET review_status = 'approved', active = true WHERE id = %s", (doc["id"],))
    assert search(q)[0].document_id == str(doc["id"])                       # approved: used, model-matched

    # Re-running is idempotent: nothing new to fetch, duplicates skipped by checksum.
    assert crawler.crawl("acme", max_pdfs=10, max_pages=10, fetcher=f)["ingested"] == 0


def test_robots_403_blocks_everything(monkeypatch):
    def handler(request):
        return httpx.Response(403 if request.url.path == "/robots.txt" else 200, content=b"<urlset/>")
    monkeypatch.setattr(crawler, "MIN_DELAY_S", 0)
    f = crawler.Fetcher()
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert not f.allowed("https://blocked.test/sitemap.xml")
    with pytest.raises(PermissionError):
        f.get("https://blocked.test/sitemap.xml")


def test_classifiers():
    assert crawler.classify_doc("INSTALLATION INSTRUCTIONS for your range") == "installation"
    assert crawler.classify_doc("", "Energy Guide") == "energy_guide"
    assert crawler.classify_category("Gas Range use and care", "appliance") == "gas"
    assert crawler.classify_category("Electric water heater owner's manual", "appliance") == "plumbing"
    assert "E24" not in crawler.model_numbers("Error E24 on model ADW5000X", None)
