import io
import json
import os
import tempfile

os.environ.update({
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "postgresql://fixbridge:fixbridge@localhost:5432/fixbridge_test"),
    "RUN_WORKER_IN_PROCESS": "false",
    "MEDIA_ROOT": tempfile.mkdtemp(prefix="fb-media-"),
    "CLOUD_PROVIDER": "openai", "OPENAI_API_KEY": "", "LOCAL_ENABLED": "false",
    "EMBEDDING_PROVIDER": "hash", "MIN_CONFIDENCE": "0.55", "MAX_CLARIFICATION_ROUNDS": "2",
    "CLOUD_RETRIES": "0",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import db  # noqa: E402
from app.ai.base import Adapter, AdapterError, ModelResult  # noqa: E402
from app.security import issue_token  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    with db.connection() as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from scripts import seed
    seed.main()
    yield
    db.close_pool()


@pytest.fixture(scope="session")
def client(database):
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_user(email: str, role: str = "customer") -> tuple[dict, dict]:
    user = db.fetch_one("""INSERT INTO users (email, role) VALUES (%s, %s)
                           ON CONFLICT (email) DO UPDATE SET role = excluded.role RETURNING *""", (email, role))
    return user, {"Authorization": f"Bearer {issue_token(user['id'])}"}


@pytest.fixture
def customer():
    import uuid
    user, headers = make_user(f"c-{uuid.uuid4().hex[:8]}@example.com")
    prop = db.fetch_one("""INSERT INTO properties (owner_id, name, passport) VALUES (%s, 'Test Home', %s)
                           RETURNING id""", (user["id"], db.jsonb({"occupancy": "owner"})))
    return {"user": user, "headers": headers, "property_id": str(prop["id"])}


@pytest.fixture
def admin():
    return make_user("admin-test@example.com", "admin")[1]


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), (120, 140, 160)).save(buf, "JPEG")
    return buf.getvalue()


class FakeAdapter(Adapter):
    """Returns canned diagnoses; `fn(payload) -> dict` can inspect the context it was sent."""

    def __init__(self, fn=None, provider="openai", model="fake-model", fail: str | None = None):
        self.fn, self.provider, self.model, self.fail = fn, provider, model, fail
        self.calls = []

    def available(self):
        return True

    def generate(self, req, timeout_s):
        payload = json.loads(req.user.split("<<CONTEXT_JSON>>", 1)[1])
        self.calls.append({"payload": payload, "images": len(req.images)})
        if self.fail:
            raise AdapterError(self.fail, retryable=True)
        data = self.fn(payload)
        return ModelResult(self.provider, self.model, json.dumps(data), data, 1200, 400)


def ref_for(payload, section_word: str) -> str:
    for e in payload["retrieved_evidence"]:
        if section_word.lower() in (e["section"] + e["content"]).lower():
            return e["ref"]
    return payload["retrieved_evidence"][0]["ref"]


@pytest.fixture
def toilet_diagnosis():
    def fn(payload):
        ref = ref_for(payload, "Replacing the Flapper")
        return {
            "summary": "Worn toilet flapper letting water leak from tank to bowl.",
            "likely_causes": [{"cause": "Worn or warped flapper", "likelihood": 80, "evidence_refs": [ref, "F:service_category"]}],
            "confidence": 0.82, "recommended_action": "diy", "hazards": ["water_damage"],
            "proposed_steps": [
                {"title": "Shut off water and drain tank", "detail": "Close the supply valve and flush.",
                 "evidence_refs": [ref]},
                {"title": "Replace the flapper", "detail": "Unhook chain, swap flapper, leave half inch slack.",
                 "tools": ["Sponge"], "materials": ["2 in. flapper"], "evidence_refs": [ref]},
                {"title": "Test", "detail": "Open the supply and test flush.", "evidence_refs": ["K99"]},
            ],
            "parts_needed": [{"part_type": "Toilet Flapper", "quantity": 1}],
            "skill_level": "beginner", "estimated_time_minutes": 20,
        }
    return fn


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    from app.ai import orchestrator
    orchestrator._health.clear()
    yield
