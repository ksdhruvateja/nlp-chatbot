"""Live Gemini check: real generation, image input, transcription and query embeddings.

Run explicitly with a configured Gemini key: python -m scripts.check_ai.
No database changes. This sends synthetic test content and incurs provider usage.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import wave

from PIL import Image, ImageDraw

from app.ai.adapters import GeminiAdapter
from app.ai.base import AdapterError, ModelRequest
from app.ai.transcription import transcribe
from app.config import get_settings
from app.embeddings import _gemini_embed
from app.schemas import Diagnosis, diagnosis_json_schema


def run() -> dict:
    settings = get_settings()
    adapter = GeminiAdapter()
    if not adapter.available():
        raise AdapterError("Set FIXBRIDGE_GEMINI_KEY securely before running the live check", retryable=False)
    image = Image.new("RGB", (640, 320), "white")
    ImageDraw.Draw(image).text((20, 70), "MODEL: FB-1234", fill="black", font_size=45)
    output = io.BytesIO()
    image.save(output, "JPEG")
    request = ModelRequest(
        system="You are a home-repair assistant. Return JSON matching the schema. Use only the "
               "provided evidence. If this evidence cannot identify a cause, ask for clarification.",
        user="A homeowner says an appliance stopped working. Read the equipment model label in the image "
             "and include its exact model number in uncertainty_notes. No repair manual or diagnostic "
             "evidence is available. Do not invent a diagnosis or repair procedure. "
             "Use recommended_action=clarify, low confidence, and list the missing evidence.",
        images=[{"label": "synthetic equipment label", "jpeg_b64": base64.b64encode(output.getvalue()).decode()}],
        json_schema=diagnosis_json_schema(), temperature=0)
    response = adapter.generate(request, settings.cloud_timeout_s)
    diagnosis = Diagnosis.model_validate(response.data)
    if diagnosis.recommended_action != "clarify" or diagnosis.proposed_steps or not diagnosis.missing_information:
        raise AssertionError("Live model did not request clarification for insufficient repair evidence")
    if "FB-1234" not in json.dumps(diagnosis.model_dump()):
        raise AssertionError("Live model did not read the synthetic label")
    _gemini_embed(["A toilet runs after flushing"], settings.embedding_dim, "RETRIEVAL_QUERY")
    silence = io.BytesIO()
    with wave.open(silence, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 32000)
    if transcribe(silence.getvalue()):
        raise AssertionError("Live model invented speech in a silent audio clip")
    return {"status": "passed", "provider": response.provider, "model": response.model,
            "checks": ["structured diagnosis", "image label reading", "insufficient evidence clarification",
                       "Gemini query embedding", "silent audio transcription"],
            "input_tokens": response.input_tokens, "output_tokens": response.output_tokens}


def main() -> int:
    try:
        print(json.dumps(run(), indent=2))
        return 0
    except (AdapterError, AssertionError, ValueError) as exc:
        print(f"Live AI check FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
