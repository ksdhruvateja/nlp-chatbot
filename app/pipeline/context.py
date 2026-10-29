"""Phase 3 - Context preparation.

Load property passport; extract text from uploaded documents (parsers, OCR if
available); retrieve equipment and previous reports; prepare images and videos
(resize, select frames, extract supported audio); collect known facts and visual
observations.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader

from .. import db
from ..storage import get_store

log = logging.getLogger(__name__)

MAX_IMAGE_EDGE = 1024
MAX_IMAGES_FOR_MODEL = 8
FRAMES_PER_VIDEO = 4
MAX_DOC_CHARS = 12_000

_MODEL_NUMBER = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{4,24}\b")
_ERROR_CODE = re.compile(r"\b(?:error|code|fault)\s*(?:code)?\s*[:#]?\s*([A-Z]{0,3}-?\d{1,3}[A-Z]?)\b", re.I)
_BRANDS = ("rheem", "a.o. smith", "ao smith", "bradford white", "whirlpool", "ge", "lg", "samsung", "bosch",
           "kohler", "moen", "delta", "carrier", "trane", "lennox", "goodman", "honeywell", "square d",
           "eaton", "leviton", "maytag", "frigidaire", "kenmore", "rinnai", "navien", "american standard")


@dataclass
class PreparedImage:
    media_id: str
    source: str            # "image" | "video_frame"
    jpeg_b64: str
    note: str = ""


@dataclass
class PreparedContext:
    request: dict
    property: dict
    equipment: list[dict]
    history: list[dict]
    facts: dict
    documents: list[dict] = field(default_factory=list)       # {media_id, filename, text}
    images: list[PreparedImage] = field(default_factory=list)
    audio_notes: list[dict] = field(default_factory=list)
    visual_observations: list[dict] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def text_for_search(self) -> str:
        parts = [self.request["description"]]
        parts += [f"{c['question']} {c['answer']}" for c in self.clarifications if c.get("answer")]
        parts += [e.get("kind", "") for e in self.equipment]
        parts += [d["text"][:1500] for d in self.documents]
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- media prep
def _resize_jpeg(data: bytes) -> tuple[bytes, dict]:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    original = img.size
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue(), {"original_size": original, "size": img.size}


def _video_frames_and_audio(data: bytes) -> tuple[list[bytes], bytes | None, float | None]:
    if not shutil.which("ffmpeg"):
        return [], None, None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.bin"
        src.write_bytes(data)
        duration = None
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=30)
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            pass
        frames: list[bytes] = []
        if duration:
            for i in range(FRAMES_PER_VIDEO):
                t = duration * (i + 0.5) / FRAMES_PER_VIDEO
                out = Path(tmp) / f"f{i}.jpg"
                subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                                "-q:v", "3", str(out)], capture_output=True, timeout=60)
                if out.exists():
                    frames.append(out.read_bytes())
        audio = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        "-t", "120", str(audio)], capture_output=True, timeout=120)
        audio_bytes = audio.read_bytes() if audio.exists() and audio.stat().st_size > 1024 else None
        return frames, audio_bytes, duration


def _extract_document_text(data: bytes, mime: str) -> tuple[str, str | None]:
    if mime == "text/plain":
        return data.decode("utf-8", "replace"), None
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(f"[page {i + 1}] {p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    if len(text.strip()) > 20 * len(reader.pages):
        return text, None
    try:  # scanned PDF -> OCR when tesseract is installed
        import pypdfium2 as pdfium
        import pytesseract
        pdf = pdfium.PdfDocument(data)
        ocr = "\n".join(f"[page {i + 1}] " + pytesseract.image_to_string(pdf[i].render(scale=2).to_pil())
                        for i in range(min(len(pdf), 20)))
        return ocr, "ocr"
    except ImportError:
        return text, "Document appears scanned; OCR is not installed so text may be incomplete"


def prepare_media(media_row: dict) -> dict:
    """Derive model-ready artifacts for one media row and persist them (idempotent)."""
    if media_row["derived"].get("prepared"):
        return media_row["derived"]
    store = get_store()
    data = store.get(media_row["storage_key"], media_row["sha256"])
    derived: dict = {"prepared": True}
    extracted_text = None
    try:
        if media_row["kind"] == "image":
            jpeg, info = _resize_jpeg(data)
            derived["preview_key"], _ = store.put(jpeg)
            derived.update(info)
        elif media_row["kind"] == "video":
            frames, audio, duration = _video_frames_and_audio(data)
            derived["duration_s"] = duration
            derived["frame_keys"] = [store.put(_resize_jpeg(f)[0])[0] for f in frames]
            if audio:
                derived["audio_key"], _ = store.put(audio)
            if not frames:
                derived["warning"] = "Could not extract frames from video"
        else:
            extracted_text, note = _extract_document_text(data, media_row["mime_type"])
            extracted_text = extracted_text[:200_000]
            if note:
                derived["note"] = note
    except Exception as exc:  # noqa: BLE001 - a bad file shouldn't sink the request
        log.warning("media prep failed for %s: %s", media_row["id"], exc)
        derived["warning"] = f"Could not process file: {type(exc).__name__}"
    db.execute("UPDATE media SET derived = %s, extracted_text = coalesce(%s, extracted_text) WHERE id = %s",
               (db.jsonb(derived), extracted_text, media_row["id"]))
    media_row["derived"] = derived
    if extracted_text is not None:
        media_row["extracted_text"] = extracted_text
    return derived


# ---------------------------------------------------------------- facts
def extract_facts(text: str) -> dict:
    upper = text.upper()
    lower = text.lower()
    return {
        "model_numbers": sorted(set(_MODEL_NUMBER.findall(upper)))[:10],
        "error_codes": sorted({m.upper() for m in _ERROR_CODE.findall(text)})[:10],
        "brands": sorted({b for b in _BRANDS if re.search(rf"\b{re.escape(b)}\b", lower)}),
        "mentions_gas_smell": bool(re.search(r"smell(s|ing)? (of )?(gas|rotten egg)|gas (smell|leak)", lower)),
        "mentions_burning_smell": bool(re.search(r"burn(ing|t)? smell|smell(s|ing)? (of )?burn|smoke", lower)),
        "mentions_sparks": bool(re.search(r"spark|arc(ing)?\b|scorch", lower)),
        "mentions_water_near_electric": bool(re.search(
            r"(water|leak\w*|flood\w*|drip\w*) (is )?(in|into|on|inside|onto|around|near|over) (the |an |my )?"
            r"(electrical |breaker |fuse )?(outlet|panel|breaker box|fuse box|wiring|light fixture|junction box)|"
            r"(outlet|panel|breaker box|fuse box|wiring|light fixture)s? (is |are |got )?(wet|flooded|submerged|"
            r"under water)", lower)),
        "mentions_co_alarm": bool(re.search(r"carbon monoxide|co alarm|co detector", lower)),
        "mentions_structural": bool(re.search(r"crack(ed|ing)? (in )?(foundation|beam|joist)|sagging|"
                                              r"ceiling (bulg|sag)", lower)),
        "mentions_asbestos_or_lead": bool(re.search(r"asbestos|lead paint|vermiculite", lower)),
    }


# ---------------------------------------------------------------- main entry
def prepare_context(request_id: str) -> PreparedContext:
    req = db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,))
    prop = db.fetch_one("SELECT * FROM properties WHERE id = %s", (req["property_id"],))
    equipment = db.fetch_all(
        """SELECT id, service_category, kind, brand, model_number, install_year, notes FROM equipment
           WHERE property_id = %s AND (service_category = %s OR id = %s) ORDER BY created_at""",
        (req["property_id"], req["service_category"], req["equipment_id"]))
    history = db.fetch_all(
        """SELECT event_type, summary, source, created_at FROM service_history
           WHERE property_id = %s AND (request_id IS NULL OR request_id <> %s)
           ORDER BY created_at DESC LIMIT 10""", (req["property_id"], request_id))
    clar = db.fetch_all("SELECT round, questions, answers FROM clarifications WHERE request_id = %s ORDER BY round",
                        (request_id,))
    clarifications = [{"round": c["round"], "question": q["question"], "answer": (c["answers"] or {}).get(q["id"])}
                      for c in clar for q in c["questions"]]

    ctx = PreparedContext(request=req, property=prop, equipment=equipment, history=history, facts={},
                          clarifications=clarifications)

    store = get_store()
    media_rows = db.fetch_all("SELECT * FROM media WHERE request_id = %s ORDER BY clarification_round DESC, created_at",
                              (request_id,))
    for m in media_rows:
        derived = prepare_media(m)
        if derived.get("warning"):
            ctx.warnings.append(f"{m['filename']}: {derived['warning']}")
        if derived.get("note"):
            ctx.warnings.append(f"{m['filename']}: {derived['note']}")
        if m["kind"] == "document" and m.get("extracted_text"):
            ctx.documents.append({"media_id": str(m["id"]), "filename": m["filename"],
                                  "text": m["extracted_text"][:MAX_DOC_CHARS]})
        elif m["kind"] == "image" and derived.get("preview_key"):
            ctx.images.append(PreparedImage(str(m["id"]), "image",
                                            base64.b64encode(store.get(derived["preview_key"])).decode(),
                                            m["filename"]))
        elif m["kind"] == "video":
            for i, key in enumerate(derived.get("frame_keys", [])):
                ctx.images.append(PreparedImage(str(m["id"]), "video_frame",
                                                base64.b64encode(store.get(key)).decode(),
                                                f"{m['filename']} frame {i + 1}"))
            if derived.get("audio_key"):
                ctx.audio_notes.append({"media_id": str(m["id"]), "filename": m["filename"],
                                        "transcript": derived.get("transcript")})
    ctx.images = ctx.images[:MAX_IMAGES_FOR_MODEL]

    customer_text = "\n".join([req["description"], *(str(c["answer"] or "") for c in clarifications),
                                *(d["text"][:1500] for d in ctx.documents)])
    # Only equipment the customer selected, or clearly mentioned, shapes facts and retrieval;
    # otherwise e.g. a water heater's model number would skew a toilet search.
    lowered = customer_text.lower()
    relevant = [e for e in equipment if e["id"] == req["equipment_id"] or e["kind"].lower() in lowered
                or (e["model_number"] and e["model_number"].lower() in lowered)]
    ctx.equipment = relevant
    facts = extract_facts(ctx.text_for_search())
    for e in relevant:
        if e["model_number"]:
            facts["model_numbers"] = sorted(set(facts["model_numbers"]) | {e["model_number"].upper()})
        if e["brand"]:
            facts["brands"] = sorted(set(facts["brands"]) | {e["brand"].lower()})
    facts["service_category"] = req["service_category"]
    facts["property_type"] = prop["property_type"]
    facts["year_built"] = prop["year_built"]
    facts["passport"] = prop["passport"]
    facts["image_count"] = len(ctx.images)
    facts["document_count"] = len(ctx.documents)
    ctx.facts = facts
    # Visual observations are recorded per image; the model adds its own reading of them.
    ctx.visual_observations = [{"media_id": i.media_id, "source": i.source, "label": i.note} for i in ctx.images]
    return ctx
