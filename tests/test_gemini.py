"""Provider contract tests use HTTP doubles; these do not claim live model quality."""
import json
from dataclasses import replace

import httpx
import numpy as np
import pytest

from app import db, embeddings
from app.ai.adapters import GeminiAdapter, LocalAdapter
from app.ai.base import AdapterError, ModelRequest
from app.config import get_settings
from app.pipeline import context, runner
from app.schemas import diagnosis_json_schema
from tests.conftest import jpeg_bytes
from tests.test_flow import get, submit


@pytest.fixture
def gemini_config(monkeypatch):
    monkeypatch.setenv("FIXBRIDGE_GEMINI_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("CLOUD_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("CLOUD_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def reply(data, finish="STOP"):
    return {"modelVersion": "gemini-2.5-flash-test-version",
            "candidates": [{"finishReason": finish, "content": {"parts": [{"text": json.dumps(data)}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50}}


def mock_http(monkeypatch, payload, status=200):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(status, json=payload)
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_gemini_structured_multimodal_contract(gemini_config, monkeypatch):
    calls = mock_http(monkeypatch, reply({"summary": "Test answer"}))
    schema = diagnosis_json_schema()
    result = GeminiAdapter().generate(ModelRequest("system", "problem",
        images=[{"label": "M1", "jpeg_b64": "jpeg-data"}], json_schema=schema), 30)
    url, args = calls[0]
    assert url.endswith("/gemini-2.5-flash:generateContent")
    assert "params" not in args and "key=" not in url
    assert args["headers"]["x-goog-api-key"] == "test-key-not-a-real-credential"
    body = args["json"]
    assert body["generationConfig"]["responseJsonSchema"] == schema
    assert body["contents"][0]["parts"][-1]["inlineData"] == {"mimeType": "image/jpeg", "data": "jpeg-data"}
    assert result.model == "gemini-2.5-flash-test-version"
    assert (result.input_tokens, result.output_tokens) == (100, 50)


@pytest.mark.parametrize("payload,retryable", [
    (reply({}, "MAX_TOKENS"), True),
    (reply({}, "SAFETY"), False),
    (reply({}, "RECITATION"), False),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, False),
    ({"candidates": []}, False),
    ({"candidates": [None]}, False),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}, True),
    ([], True),
])
def test_unusable_gemini_responses_are_controlled(gemini_config, monkeypatch, payload, retryable):
    mock_http(monkeypatch, payload)
    with pytest.raises(AdapterError) as err:
        GeminiAdapter().generate(ModelRequest("system", "problem"), 20)
    assert err.value.retryable is retryable


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False), (429, True), (503, True)])
def test_provider_errors_never_echo_content(gemini_config, monkeypatch, status, retryable):
    mock_http(monkeypatch, {"error": "private homeowner problem and secret-key"}, status)
    with pytest.raises(AdapterError) as err:
        GeminiAdapter().generate(ModelRequest("system", "problem"), 20)
    assert err.value.retryable is retryable
    assert "private homeowner" not in str(err.value) and "secret-key" not in str(err.value)


def test_non_json_and_timeout_are_retryable(gemini_config, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, text="not JSON"))
    with pytest.raises(AdapterError, match="invalid JSON"):
        GeminiAdapter().generate(ModelRequest("s", "p"), 10)
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("sensitive url with a key")
    monkeypatch.setattr(httpx, "post", timeout)
    with pytest.raises(AdapterError) as err:
        GeminiAdapter().generate(ModelRequest("s", "p"), 10)
    assert err.value.retryable and "sensitive" not in str(err.value)


def test_gemini_embeddings_use_query_document_tasks(gemini_config, monkeypatch):
    settings = replace(get_settings(), embedding_provider="gemini", embedding_dim=4)
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    calls = mock_http(monkeypatch, {"embeddings": [{"values": [0, 3, 4, 0]}]})
    document = embeddings.embed_many(["manual text"])[0]
    query = embeddings.embed("toilet running")
    assert np.isclose(np.linalg.norm(document), 1) and query == document
    tasks = [args["json"]["requests"][0]["taskType"] for _, args in calls]
    assert tasks == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
    assert all("gemini-embedding-001:batchEmbedContents" in url and "params" not in args
               for url, args in calls)


@pytest.mark.parametrize("vectors", [[], [{"values": [1, 2]}], [{"values": [0, 0, 0, 0]}],
                                     [{"values": [1, 2, 3, "NaN"]}]])
def test_bad_embedding_vectors_rejected(gemini_config, monkeypatch, vectors):
    mock_http(monkeypatch, {"embeddings": vectors})
    with pytest.raises(AdapterError, match="invalid embeddings"):
        embeddings._gemini_embed(["text"], 4, "RETRIEVAL_QUERY")


def test_real_adapter_runs_through_repair_pipeline(gemini_config, monkeypatch, client, customer, toilet_diagnosis):
    def post(url, **kwargs):
        payload = json.loads(kwargs["json"]["contents"][0]["parts"][0]["text"].split("<<CONTEXT_JSON>>", 1)[1])
        return httpx.Response(200, json=reply(toilet_diagnosis(payload)))
    monkeypatch.setattr(httpx, "post", post)
    req = submit(client, customer, "My toilet keeps running and refills every few minutes by itself.",
                 files=[("files", ("toilet.jpg", jpeg_bytes(), "image/jpeg"))])
    assert runner.run_pending() == 1
    result = get(client, customer, req["id"])["result"]
    assert result["outcome"] == "diy_plan" and result["provider"] == "gemini"
    assert result["model"] == "gemini-2.5-flash-test-version" and result["sources"]
    assert db.fetch_one("SELECT success FROM model_calls WHERE request_id = %s", (req["id"],))["success"]


def upload_video(client, customer):
    return submit(client, customer, "Please check my water heater; the video explains the problem.",
                  files=[("files", ("symptom.mp4", b"\0\0\0\x18ftyp" + b"x" * 30, "video/mp4"))])


def test_transcribed_emergency_stops_before_diagnosis(gemini_config, monkeypatch, client, customer):
    monkeypatch.setattr(context, "_video_frames_and_audio", lambda _: ([jpeg_bytes()], b"wav audio", 3))
    calls = mock_http(monkeypatch, reply({"transcript": "I smell gas near the water heater."}))
    req = upload_video(client, customer)
    assert runner.run_pending() == 1
    result = get(client, customer, req["id"])["result"]
    assert result["status"] == "blocked" and result["safety"]["emergency"]
    assert len(calls) == 1  # transcription, never a diagnostic model request
    media = db.fetch_one("SELECT derived FROM media WHERE request_id = %s", (req["id"],))
    assert media["derived"]["transcription_status"] == "completed"


def test_failed_transcription_keeps_video_frames(gemini_config, monkeypatch, client, customer):
    monkeypatch.setattr(context, "_video_frames_and_audio", lambda _: ([jpeg_bytes()], b"wav audio", 3))
    mock_http(monkeypatch, {"error": "provider unavailable"}, 503)
    req = upload_video(client, customer)
    ctx = context.prepare_context(req["id"])
    assert len(ctx.images) == 1
    assert any("Audio could not be transcribed" in w for w in ctx.warnings)
    assert ctx.audio_notes[0]["transcript"] is None
    db.execute("DELETE FROM jobs WHERE request_id = %s", (req["id"],))


def test_transcript_is_cited_and_redacted(gemini_config, monkeypatch, client, customer):
    from app.ai.orchestrator import build_request
    monkeypatch.setattr(context, "_video_frames_and_audio", lambda _: ([], b"wav audio", 3))
    mock_http(monkeypatch, reply({"transcript": "The Rheem XE50T06ST45U1 is cold. Email me at private@example.com."}))
    req = upload_video(client, customer)
    ctx = context.prepare_context(req["id"])
    assert "XE50T06ST45U1" in ctx.facts["model_numbers"]
    request, refs = build_request(ctx, [])
    assert "private@example.com" not in request.user
    payload = json.loads(request.user.split("<<CONTEXT_JSON>>", 1)[1])
    assert payload["audio_transcripts"][0]["ref"] in refs
    assert "[email]" in payload["audio_transcripts"][0]["text"]
    db.execute("DELETE FROM jobs WHERE request_id = %s", (req["id"],))


def test_ready_with_gemini_configured(gemini_config, client):
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["ai_available"]


def test_missing_local_weights_not_ready(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404, json={"error": "not found"}))
    assert LocalAdapter().ping() is False


def test_invalid_diagnosis_falls_back_without_logging_model_content(
        gemini_config, monkeypatch, client, customer, toilet_diagnosis):
    from tests.conftest import FakeAdapter
    mock_http(monkeypatch, reply({"summary": "private homeowner content"}))
    req = submit(client, customer, "My toilet keeps running after every flush.")
    assert runner.run_pending(local=FakeAdapter(toilet_diagnosis, provider="local")) == 1
    result = get(client, customer, req["id"])["result"]
    assert result["provider"] == "local"
    error = db.fetch_one("SELECT error FROM model_calls WHERE request_id = %s AND provider = 'gemini'",
                         (req["id"],))["error"]
    assert "schema validation failed" in error and "private homeowner content" not in error


def test_latest_alias_does_not_force_legacy_thinking_options(gemini_config, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    get_settings.cache_clear()
    calls = mock_http(monkeypatch, reply({"summary": "Test answer"}))
    GeminiAdapter().generate(ModelRequest("system", "problem"), 10)
    assert "thinkingConfig" not in calls[0][1]["json"]["generationConfig"]


def test_missing_key_live_check_does_not_report_success(monkeypatch, capsys):
    from scripts import check_ai
    for name in ("FIXBRIDGE_GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        assert check_ai.main() == 1
        assert "FAILED" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
