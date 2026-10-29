"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret: str
    media_encryption_key: str
    media_root: Path
    max_upload_mb: int
    allowed_mime_prefixes: tuple[str, ...] = field(default=("image/", "video/", "application/pdf", "text/plain"))

    cloud_provider: str = "gemini"
    cloud_enabled: bool = True
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    gemini_embedding_model: str = "gemini-embedding-001"
    transcribe_audio: bool = True
    cloud_timeout_s: float = 45
    cloud_retries: int = 2

    local_enabled: bool = False
    local_base_url: str = "http://localhost:11434"
    local_model: str = "llama3.1:8b"
    local_timeout_s: float = 120

    embedding_provider: str = "hash"
    embedding_dim: int = 384

    max_clarification_rounds: int = 3
    min_evidence_passages: int = 1
    min_confidence: float = 0.55
    run_worker_in_process: bool = True


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        database_url=os.getenv("DATABASE_URL", "postgresql://fixbridge:fixbridge@localhost:5432/fixbridge"),
        secret=os.getenv("FIXBRIDGE_SECRET", "change-me"),
        media_encryption_key=os.getenv("MEDIA_ENCRYPTION_KEY", ""),
        media_root=Path(os.getenv("MEDIA_ROOT", "./data/media")),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
        cloud_provider=os.getenv("CLOUD_PROVIDER", "gemini").lower(),
        cloud_enabled=_bool("CLOUD_ENABLED", True),
        openai_api_key=os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        gemini_api_key=(os.getenv("FIXBRIDGE_GEMINI_KEY") or os.getenv("GEMINI_API_KEY")
                        or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_KEY", "")),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        gemini_embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        transcribe_audio=_bool("TRANSCRIBE_AUDIO", True),
        cloud_timeout_s=float(os.getenv("CLOUD_TIMEOUT_S", "45")),
        cloud_retries=int(os.getenv("CLOUD_RETRIES", "2")),
        local_enabled=_bool("LOCAL_ENABLED", False),
        local_base_url=os.getenv("LOCAL_BASE_URL", "http://localhost:11434"),
        local_model=os.getenv("LOCAL_MODEL", "llama3.1:8b"),
        local_timeout_s=float(os.getenv("LOCAL_TIMEOUT_S", "120")),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", "hash").lower(),
        embedding_dim=int(os.getenv("EMBEDDING_DIM", "384")),
        max_clarification_rounds=int(os.getenv("MAX_CLARIFICATION_ROUNDS", "3")),
        min_evidence_passages=int(os.getenv("MIN_EVIDENCE_PASSAGES", "1")),
        min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.55")),
        run_worker_in_process=_bool("RUN_WORKER_IN_PROCESS", True),
    )
