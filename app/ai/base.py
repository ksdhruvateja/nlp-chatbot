"""Shared adapter types. Every provider takes a ModelRequest and returns a ModelResult."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class ModelRequest:
    system: str
    user: str
    images: list[dict] = field(default_factory=list)   # {"label": str, "jpeg_b64": str}
    json_schema: dict | None = None
    max_output_tokens: int = 3000
    temperature: float = 0.2


@dataclass
class ModelResult:
    provider: str
    model: str
    text: str
    data: dict
    input_tokens: int | None = None
    output_tokens: int | None = None


class AdapterError(Exception):
    """Raised for any provider failure. `retryable` drives the router's retry policy."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Adapter:
    provider: str = "base"
    model: str = ""

    def available(self) -> bool:
        raise NotImplementedError

    def generate(self, req: ModelRequest, timeout_s: float) -> ModelResult:
        raise NotImplementedError


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or leading prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AdapterError("model returned no JSON object", retryable=True)
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AdapterError(f"model returned invalid JSON: {exc}", retryable=True) from exc
    if not isinstance(data, dict):
        raise AdapterError("model returned JSON that is not an object", retryable=True)
    return data
