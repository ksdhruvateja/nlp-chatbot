"""Manufacturer manual crawler.

Discovers product pages from each manufacturer's official sitemaps, follows links to
manual PDFs on the manufacturer's own domains/asset hosts, and ingests owner's,
use-and-care, installation and service manuals from the last 15 years.

Rules this crawler always follows:
  * only domains listed for a source (official site + its asset CDN)
  * robots.txt is honoured, including Crawl-delay; a minimum delay applies regardless
  * identifies itself with a contactable User-Agent (CRAWLER_CONTACT)
  * downloaded manuals are ingested as review_status="pending": they are NOT used for
    customer answers until an admin approves them (licensing / quality check)

The frontier lives in the `crawl_urls` table so crawls are resumable and can run as a
long-lived worker (e.g. an ECS scheduled task): `python -m scripts.crawl_manuals`.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from . import db, ingestion
from .storage import get_store

log = logging.getLogger("fixbridge.crawler")

MIN_DELAY_S = 2.0
MAX_PDF_BYTES = 60 * 1024 * 1024
YEARS_BACK = 15
KEEP_DOC_TYPES = {"owners_manual", "use_and_care", "installation", "service", "troubleshooting"}


@dataclass(frozen=True)
class Source:
    name: str
    brand: str
    sitemaps: tuple[str, ...]
    page_domains: tuple[str, ...]          # domains whose HTML pages we fetch
    pdf_domains: tuple[str, ...]           # domains we download PDFs from (site + its asset CDN)
    product_url: str                       # regex a sitemap URL must match to count as a product page
    sitemap_filter: str = r"."             # regex for which nested sitemaps to follow
    model_from_url: str | None = None      # regex with one group extracting the model number
    default_category: str = "appliance"


# Add a Source per manufacturer. Each one needs checking by hand: sites differ a lot, and some
# (e.g. ones that load manuals with JavaScript or behind a model-number search) need a
# dedicated adapter instead of sitemap crawling.
SOURCES: dict[str, Source] = {
    "ge": Source(
        name="ge", brand="GE",
        sitemaps=("https://www.geappliances.com/xmlsitemap.php",),
        sitemap_filter=r"type=products",
        page_domains=("www.geappliances.com",),
        pdf_domains=("images.salsify.com", "products.geappliances.com", "www.geappliances.com"),
        product_url=r"/appliance/",
        model_from_url=r"-([A-Z0-9]{5,})/?$",
    ),
}

DOC_TYPES: tuple[tuple[str, str], ...] = (
    ("service", r"service manual|technical service|service guide|repair manual|wiring diagram"),
    ("troubleshooting", r"troubleshooting|error codes?|fault codes?|diagnostic"),
    ("installation", r"installation instructions|installation guide|install(ation)? manual"),
    ("use_and_care", r"use (and|&) care|use & care"),
    ("owners_manual", r"owner'?s manual|owner'?s guide|user manual|user guide|operating instructions"),
    ("spec_sheet", r"specification sheet|spec sheet|dimensions? guide|quick specs|planning guide"),
    ("energy_guide", r"energyguide|energy guide|estimated yearly energy"),
    ("warranty", r"^\W*(limited )?warranty"),
)

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("plumbing", r"water heater|garbage disposer|disposal|water softener|water filtration"),
    ("gas", r"\bgas (range|cooktop|oven|dryer|furnace|boiler|fireplace)\b|furnace|boiler"),
    ("hvac", r"air conditioner|heat pump|air handler|ductless|mini[- ]split|thermostat|dehumidifier|"
             r"humidifier|air purifier"),
    ("appliance", r"dishwasher|refrigerator|freezer|washer|dryer|range|oven|cooktop|microwave|ice maker|"
                  r"wine cooler|trash compactor|hood"),
)

_MODEL = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z][A-Z0-9-]{4,19}\b")
_YEAR = re.compile(r"(?:©|\(c\)|copyright|rev(?:ision|\.)?|printed in [a-z ]+)\s*(?:[a-z]+\s+)?((?:19|20)\d\d)", re.I)


class Fetcher:
    """HTTP client that respects robots.txt and per-host crawl delays."""

    def __init__(self, contact: str | None = None):
        contact = contact or os.getenv("CRAWLER_CONTACT", "admin@fixbridge.us")
        self.user_agent = f"FixBridgeManualBot/1.0 (+mailto:{contact})"
        self.client = httpx.Client(headers={"User-Agent": self.user_agent}, timeout=60, follow_redirects=True)
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.last_hit: dict[str, float] = {}

    def _robots(self, host: str, scheme: str) -> urllib.robotparser.RobotFileParser | None:
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(f"{scheme}://{host}/robots.txt", timeout=20)
                if r.status_code in (401, 403):
                    rp.disallow_all = True      # the site is refusing crawlers: respect that
                else:
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host]

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        rp = self._robots(u.netloc, u.scheme)
        return rp.can_fetch(self.user_agent, url) if rp else True

    def _wait(self, host: str, scheme: str) -> None:
        rp = self._robots(host, scheme)
        delay = max(MIN_DELAY_S, float((rp.crawl_delay(self.user_agent) if rp else None) or 0))
        elapsed = time.monotonic() - self.last_hit.get(host, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self.last_hit[host] = time.monotonic()

    def get(self, url: str, max_bytes: int | None = None) -> httpx.Response:
        u = urlparse(url)
        if not self.allowed(url):
            raise PermissionError("disallowed by robots.txt")
        self._wait(u.netloc, u.scheme)
        if max_bytes is None:
            return self.client.get(url)
        with self.client.stream("GET", url) as r:
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"file larger than {max_bytes // 1024 // 1024} MB")
                chunks.append(chunk)
            r._content = b"".join(chunks)  # noqa: SLF001 - buffered for the caller
            return r


# ---------------------------------------------------------------- classification helpers
def classify_doc(text: str, title: str = "") -> str:
    head = f"{title}\n{text[:4000]}".lower()
    for name, pattern in DOC_TYPES:
        if re.search(pattern, head, re.M):
            return name
    return "other"


def classify_category(text: str, default: str) -> str:
    head = text[:6000].lower()
    for name, pattern in CATEGORIES:
        if re.search(pattern, head):
            return name
    return default


def published_year(reader: PdfReader, text: str) -> int | None:
    """Year printed in the document (copyright / revision) wins; CDNs often regenerate PDFs, so the
    file's CreationDate is only a fallback."""
    this_year = date.today().year
    printed = [int(y) for y in _YEAR.findall(text[:20000]) if 1980 <= int(y) <= this_year]
    if printed:
        return max(printed)
    try:
        created = (reader.metadata or {}).get("/CreationDate") or ""
        if m := re.match(r"D:((?:19|20)\d\d)", str(created)):
            return int(m.group(1))
    except Exception:  # noqa: BLE001 - malformed metadata is common
        pass
    return None


def model_numbers(text: str, hint: str | None) -> list[str]:
    found = {hint.upper()} if hint else set()
    head = text[:8000].upper()
    for m in _MODEL.findall(head):
        if len(found) >= 25:
            break
        if not re.fullmatch(r"[A-Z]+-?\d{1,3}|ISO\d+|UL\d+|[A-Z]{2}\d{2}", m):   # skip "E24", "UL123", etc.
            found.add(m)
    return sorted(found)


# ---------------------------------------------------------------- frontier
def _enqueue(url: str, source: str, kind: str, parent: str | None = None, context: dict | None = None) -> None:
    db.execute("""INSERT INTO crawl_urls (url, source, kind, discovered_from, context)
                  VALUES (%s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING""",
               (url, source, kind, parent, db.jsonb(context or {})))


def _claim(source: str, kinds: tuple[str, ...]) -> dict | None:
    return db.fetch_one("""
        UPDATE crawl_urls SET status = 'running', attempts = attempts + 1
        WHERE url = (SELECT url FROM crawl_urls WHERE source = %s AND kind = ANY(%s) AND
                       (status = 'pending' OR (status = 'failed' AND attempts < 3))
                     ORDER BY array_position(%s, kind), created_at FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING *""", (source, list(kinds), list(kinds)))


def _finish(url: str, status: str, http_status: int | None = None, result: str | None = None) -> None:
    db.execute("""UPDATE crawl_urls SET status = %s, http_status = %s, result = %s, fetched_at = now()
                  WHERE url = %s""", (status, http_status, (result or "")[:1000] or None, url))


def _host_ok(url: str, domains: tuple[str, ...]) -> bool:
    return urlparse(url).netloc.lower() in domains


# ---------------------------------------------------------------- handlers
def handle_sitemap(f: Fetcher, src: Source, row: dict) -> str:
    r = f.get(row["url"])
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = r.content
    if row["url"].endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    soup = BeautifulSoup(body, "xml")
    nested = [loc.text.strip() for loc in soup.select("sitemap > loc")]
    pages = [loc.text.strip() for loc in soup.select("url > loc")]
    for u in nested:
        if re.search(src.sitemap_filter, u) and _host_ok(u, src.page_domains):
            _enqueue(u, src.name, "sitemap", row["url"])
    n = 0
    for u in pages:
        if re.search(src.product_url, u) and _host_ok(u, src.page_domains):
            m = re.search(src.model_from_url, u) if src.model_from_url else None
            _enqueue(u, src.name, "page", row["url"], {"model": m.group(1) if m else None})
            n += 1
    return f"{len(nested)} sitemaps, {n} product pages"


def handle_page(f: Fetcher, src: Source, row: dict) -> str:
    r = f.get(row["url"])
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "lxml")
    title = (soup.title.string or "").strip() if soup.title else ""
    links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(row["url"], a["href"])
        if ".pdf" in href.lower().split("?")[0]:
            links[href] = a.get_text(" ", strip=True)
    # Some sites only reference PDFs in scripts/JSON; pick those up too.
    for href in re.findall(r"https?://[^\"'\s<>]+?\.pdf", r.text):
        links.setdefault(href, "")
    n = 0
    for href, label in links.items():
        if _host_ok(href, src.pdf_domains):
            # Skip obvious non-manuals by link text before downloading.
            if label and classify_doc("", label) in ("spec_sheet", "energy_guide", "warranty"):
                continue
            _enqueue(href, src.name, "pdf", row["url"], {**row["context"], "page_title": title, "link_text": label})
            n += 1
    return f"{n} pdf links"


def handle_pdf(f: Fetcher, src: Source, row: dict, min_year: int) -> str:
    r = f.get(row["url"], max_bytes=MAX_PDF_BYTES)
    data = r.content
    if r.status_code != 200 or not data.startswith(b"%PDF"):
        raise RuntimeError(f"not a PDF (HTTP {r.status_code})")
    import hashlib
    checksum = hashlib.sha256(data).hexdigest()
    if db.fetch_one("SELECT 1 FROM knowledge_documents WHERE checksum = %s", (checksum,)):
        return "skipped:duplicate"

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:6])
    ctx = row["context"] or {}
    doc_type = classify_doc(text, ctx.get("link_text", ""))
    if doc_type not in KEEP_DOC_TYPES:
        return f"skipped:{doc_type}"
    year = published_year(reader, text)
    if year and year < min_year:
        return f"skipped:too_old:{year}"
    if len(text.strip()) < 200:
        return "skipped:no_text (scanned; needs OCR)"

    raw_key, _ = get_store().put(data)
    category = classify_category(f"{ctx.get('page_title', '')}\n{text}", src.default_category)
    models = model_numbers(text, ctx.get("model"))
    title = (ctx.get("link_text") or ctx.get("page_title") or doc_type).strip()[:200]
    if ctx.get("model") and ctx["model"] not in title:
        title = f"{src.brand} {ctx['model']} - {title}"
    result = ingestion.ingest(data, "manual.pdf", ingestion.DocumentMeta(
        title=title, source=row["url"], permission="manufacturer_public",
        approved_by="crawler (pending review)", service_category=category, version=checksum[:12],
        publisher=src.brand, brand=src.brand, model_numbers=models, review_status="pending",
        source_url=row["url"], doc_type=doc_type, published_year=year, raw_storage_key=raw_key))
    return f"ingested:{doc_type}:{year or 'year?'}:{result['passages']} passages"


# ---------------------------------------------------------------- driver
def seed(source_name: str) -> None:
    src = SOURCES[source_name]
    for u in src.sitemaps:
        _enqueue(u, src.name, "sitemap")


def crawl(source_name: str, max_pdfs: int = 50, max_pages: int = 500, max_seconds: float | None = None,
          fetcher: Fetcher | None = None) -> dict:
    """Crawl until limits are hit or the frontier is empty. Safe to stop and resume."""
    src = SOURCES[source_name]
    f = fetcher or Fetcher()
    min_year = date.today().year - YEARS_BACK
    seed(source_name)
    stats = {"sitemaps": 0, "pages": 0, "pdfs": 0, "ingested": 0, "skipped": 0, "failed": 0}
    started = time.monotonic()
    while True:
        if max_seconds and time.monotonic() - started > max_seconds:
            break
        kinds = tuple(k for k, done in (("pdf", stats["pdfs"] >= max_pdfs), ("sitemap", False),
                                        ("page", stats["pages"] >= max_pages)) if not done)
        # Prefer PDFs (finish what was found), then sitemaps, then product pages.
        row = _claim(src.name, kinds) if kinds else None
        if not row:
            break
        try:
            if row["kind"] == "sitemap":
                stats["sitemaps"] += 1
                result = handle_sitemap(f, src, row)
            elif row["kind"] == "page":
                stats["pages"] += 1
                result = handle_page(f, src, row)
            else:
                stats["pdfs"] += 1
                result = handle_pdf(f, src, row, min_year)
                stats["ingested" if result.startswith("ingested") else "skipped"] += 1
            _finish(row["url"], "skipped" if result.startswith("skipped") else "done", 200, result)
            log.info("%s %s -> %s", row["kind"], row["url"], result)
        except PermissionError as exc:
            stats["blocked"] = stats.get("blocked", 0) + 1
            _finish(row["url"], "skipped", None, str(exc))
            log.warning("%s blocked: %s", row["url"], exc)
        except Exception as exc:  # noqa: BLE001 - record and move on
            stats["failed"] += 1
            _finish(row["url"], "failed", None, f"{type(exc).__name__}: {exc}")
            log.warning("%s %s failed: %s", row["kind"], row["url"], exc)
    return stats


def frontier_stats() -> list[dict]:
    return db.fetch_all("""SELECT source, kind, status, count(*) AS n FROM crawl_urls
                           GROUP BY source, kind, status ORDER BY source, kind, status""")
