"""Provider adapters: OpenAI and Gemini (cloud), Ollama-compatible (local), and an
offline demo adapter for trying the full workflow without any model."""
from __future__ import annotations

import httpx
from urllib.parse import quote

from ..config import get_settings
from .base import Adapter, AdapterError, ModelRequest, ModelResult, parse_json


def _raise_for(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
    # Error bodies can echo prompts, media or credentials. Log status only.
    raise AdapterError(f"{provider} HTTP {resp.status_code}", retryable=retryable)


def _post(url: str, provider: str, timeout_s: float, **kwargs) -> dict:
    try:
        resp = httpx.post(url, timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)), **kwargs)
    except httpx.TimeoutException as exc:
        raise AdapterError(f"{provider} timeout after {timeout_s}s", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise AdapterError(f"{provider} connection error ({type(exc).__name__})", retryable=True) from exc
    _raise_for(resp, provider)
    try:
        data = resp.json()
    except ValueError as exc:
        raise AdapterError(f"{provider} returned invalid JSON", retryable=True) from exc
    if not isinstance(data, dict):
        raise AdapterError(f"{provider} returned a non-object response", retryable=True)
    return data


class OpenAIAdapter(Adapter):
    provider = "openai"

    def __init__(self):
        s = get_settings()
        self.key, self.model = s.openai_api_key, s.openai_model

    def available(self) -> bool:
        return bool(self.key)

    def generate(self, req: ModelRequest, timeout_s: float) -> ModelResult:
        content: list[dict] = [{"type": "text", "text": req.user}]
        for img in req.images:
            content.append({"type": "text", "text": f"[Image {img['label']}]"})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img['jpeg_b64']}", "detail": "auto"}})
        body = {"model": self.model, "temperature": req.temperature, "max_tokens": req.max_output_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": content}]}
        data = _post("https://api.openai.com/v1/chat/completions", self.provider, timeout_s,
                     headers={"Authorization": f"Bearer {self.key}"}, json=body)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise AdapterError("openai response missing content") from exc
        usage = data.get("usage", {})
        return ModelResult(self.provider, data.get("model", self.model), text, parse_json(text),
                           usage.get("prompt_tokens"), usage.get("completion_tokens"))


class GeminiAdapter(Adapter):
    provider = "gemini"

    def __init__(self):
        s = get_settings()
        self.key, self.model = s.gemini_api_key, s.gemini_model

    def available(self) -> bool:
        return bool(self.key)

    def generate_parts(self, system: str, parts: list[dict], schema: dict | None,
                       timeout_s: float, max_tokens: int = 3000,
                       temperature: float = 0.2) -> ModelResult:
        if not self.available():
            raise AdapterError("Gemini key is not configured", retryable=False)
        config = {"temperature": temperature, "maxOutputTokens": max_tokens,
                  "responseMimeType": "application/json"}
        if self.model.removeprefix("models/").startswith("gemini-2.5-flash"):
            # Other Gemini families may require different thinking controls.
            config["thinkingConfig"] = {"thinkingBudget": 0}
        if schema:
            config["responseJsonSchema"] = schema
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}], "generationConfig": config}
        model = quote(self.model.removeprefix("models/"), safe="")
        data = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                     self.provider, timeout_s, headers={"x-goog-api-key": self.key}, json=body)
        try:
            candidate = data["candidates"][0]
            finish = candidate.get("finishReason")
            if finish == "MAX_TOKENS":
                raise AdapterError("gemini output token limit reached", retryable=True)
            if finish != "STOP":
                raise AdapterError("gemini did not complete generation", retryable=False)
            text = "".join(p.get("text", "") for p in candidate["content"]["parts"]
                           if not p.get("thought"))
            if not text.strip():
                raise AdapterError("gemini returned empty content", retryable=True)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise AdapterError("gemini returned no usable candidate", retryable=False) from exc
        usage = data.get("usageMetadata") or {}
        return ModelResult(self.provider, data.get("modelVersion") or self.model, text, parse_json(text),
                           usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))

    def generate(self, req: ModelRequest, timeout_s: float) -> ModelResult:
        parts: list[dict] = [{"text": req.user}]
        for img in req.images:
            parts += [{"text": f"[Image {img['label']}]"},
                      {"inlineData": {"mimeType": "image/jpeg", "data": img["jpeg_b64"]}}]
        return self.generate_parts(req.system, parts, req.json_schema, timeout_s,
                                   req.max_output_tokens, req.temperature)


class LocalAdapter(Adapter):
    """Locally deployed model behind an Ollama-compatible /api/chat endpoint."""
    provider = "local"

    def __init__(self):
        s = get_settings()
        self.base, self.model, self.enabled = s.local_base_url.rstrip("/"), s.local_model, s.local_enabled

    def available(self) -> bool:
        return self.enabled

    def ping(self, timeout_s: float = 3) -> bool:
        try:
            # A reachable server is insufficient when the configured weights are absent.
            response = httpx.post(f"{self.base}/api/show", json={"model": self.model}, timeout=timeout_s)
            return response.status_code == 200 and isinstance(response.json().get("model_info"), dict)
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def generate(self, req: ModelRequest, timeout_s: float) -> ModelResult:
        msg = {"role": "user", "content": req.user}
        if req.images:
            msg["images"] = [img["jpeg_b64"] for img in req.images]
        body = {"model": self.model, "stream": False, "format": req.json_schema or "json",
                "options": {"temperature": req.temperature, "num_predict": req.max_output_tokens},
                "messages": [{"role": "system", "content": req.system}, msg]}
        data = _post(f"{self.base}/api/chat", self.provider, timeout_s, json=body)
        text = data.get("message", {}).get("content", "")
        return ModelResult(self.provider, self.model, text, parse_json(text),
                           data.get("prompt_eval_count"), data.get("eval_count"))


class DemoAdapter(Adapter):
    """Offline, deterministic stand-in (CLOUD_PROVIDER=demo). It does NOT reason about the
    problem; it assembles a diagnosis from the top retrieved passages so the rest of the
    pipeline (safety, plans, clarifications, UI) can be exercised without API keys."""
    provider = "demo"
    model = "evidence-template-v1"

    def available(self) -> bool:
        return True

    def generate(self, req: ModelRequest, timeout_s: float) -> ModelResult:
        import json
        import re
        payload = json.loads(req.user.split("<<CONTEXT_JSON>>", 1)[1])
        evidence = payload["retrieved_evidence"]
        facts = payload["known_facts"]
        if not evidence:
            data = {"summary": "There is not enough matching repair knowledge to diagnose this yet.",
                    "likely_causes": [{"cause": "Unknown - more information needed", "likelihood": 0.2}],
                    "confidence": 0.2, "recommended_action": "clarify",
                    "missing_information": ["Brand and model number of the affected equipment",
                                            "A clear photo of the problem area"]}
        else:
            top = evidence[0]
            # Prefer a procedural passage (numbered steps) among the top hits for the step list.
            proc = next((e for e in evidence[:6] if e["title"] == top["title"]
                         and re.search(r"(^|\s)1\.\s", e["content"])), top)
            items = [i.strip() for i in re.split(r"(?:^|\s)\d+\.\s", proc["content"]) if len(i.strip()) > 15]
            steps = items[1:] if len(items) > 1 and not re.match(r"\d+\.", proc["content"]) else items
            text_top = (top["content"] + " " + proc["content"]).lower()
            hazards = sorted({tag for tag, words in (("electrical", ("breaker", "volt", "wiring", "wires")),
                                                      ("gas", ("gas valve", "burner", "pilot")),
                                                      ("scald", ("drain the tank", "hot water")),
                                                      ("water_damage", ("supply valve", "shutoff", "supply line")))
                              if any(w in text_top for w in words)})
            data = {"summary": f"Most relevant guidance: '{top['title']}' - {top['section'] or 'general'}.",
                    "likely_causes": [{"cause": (e["section"] or e["title"])[:120],
                                       "likelihood": round(max(0.2, 0.7 - 0.15 * i), 2),
                                       "reasoning": e["content"][:200], "evidence_refs": [e["ref"]]}
                                      for i, e in enumerate(evidence[:3])],
                    "confidence": 0.6 if facts.get("model_numbers") or len(evidence) >= 2 else 0.45,
                    "recommended_action": "diy",
                    "hazards": hazards,
                    "missing_information": [] if facts.get("model_numbers") else ["Equipment model number"],
                    "uncertainty_notes": ["Demo adapter: assembled from retrieved passages, not model reasoning."],
                    "proposed_steps": [{"title": s_.split(".")[0][:80], "detail": s_, "evidence_refs": [proc["ref"]],
                                        "hazard_tags": hazards} for s_ in steps[:8]] or
                                      [{"title": "Follow the referenced manual section", "detail": proc["content"],
                                        "evidence_refs": [proc["ref"]]}],
                    "skill_level": "intermediate", "estimated_time_minutes": 60}
        text = json.dumps(data)
        return ModelResult(self.provider, self.model, text, data, None, None)


def cloud_adapter() -> Adapter | None:
    name = get_settings().cloud_provider
    return {"openai": OpenAIAdapter, "gemini": GeminiAdapter, "demo": DemoAdapter}.get(name, lambda: None)()
