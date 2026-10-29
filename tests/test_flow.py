"""End-to-end flows through the API with fake model adapters."""
import json

from app import db
from app.pipeline import runner
from tests.conftest import FakeAdapter, jpeg_bytes, make_user, ref_for


def submit(client, cust, description, category="plumbing", files=None, key=None):
    headers = dict(cust["headers"])
    if key:
        headers["Idempotency-Key"] = key
    r = client.post("/api/repair-requests", headers=headers,
                    data={"property_id": cust["property_id"], "service_category": category,
                          "description": description}, files=files or [])
    assert r.status_code == 202, r.text
    return r.json()


def get(client, cust, rid):
    r = client.get(f"/api/repair-requests/{rid}", headers=cust["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def test_diy_plan_happy_path(client, customer, toilet_diagnosis):
    req = submit(client, customer, "My toilet keeps running and refills every few minutes by itself.",
                 files=[("files", ("toilet.jpg", jpeg_bytes(), "image/jpeg"))], key="abc-1")
    assert req["status"] == "received" and len(req["media"]) == 1
    # Idempotent re-submit returns the same request.
    again = submit(client, customer, "My toilet keeps running and refills every few minutes by itself.", key="abc-1")
    assert again["id"] == req["id"]

    cloud = FakeAdapter(toilet_diagnosis)
    assert runner.run_pending(cloud=cloud) == 1
    sent = cloud.calls[0]
    assert sent["images"] == 1                                       # resized image went to the model
    assert any("Toilet" in e["title"] for e in sent["payload"]["retrieved_evidence"])
    assert "address" not in json.dumps(sent["payload"]["property"])  # property data minimized

    res = get(client, customer, req["id"])
    assert res["status"] == "completed"
    r = res["result"]
    assert r["outcome"] == "diy_plan"
    assert r["likely_causes"][0]["likelihood"] == 0.8               # normalized from percent
    assert "K99" in r["evidence_check"]["invalid_refs"]             # hallucinated citation stripped
    plan = r["repair_plan"]
    assert plan["steps"][2]["verified"] is False
    assert plan["cost_estimate"]["low"] == 6.99 and plan["cost_estimate"]["high"] == 11.99
    assert any("supply" in p.lower() for p in plan["prerequisites"])   # water-damage prerequisite injected
    assert plan["sources"]

    # Phase 9 outcome -> passport + history
    r = client.post(f"/api/repair-requests/{req['id']}/outcome", headers=customer["headers"],
                    json={"resolution": "fixed_diy", "rating": 5, "parts_used": [{"name": "2in flapper"}],
                          "actual_cost": 7.5})
    assert r.status_code == 201 and r.json()["label"] == "user_reported"
    prop = client.get(f"/api/properties/{customer['property_id']}", headers=customer["headers"]).json()
    assert prop["property"]["passport"]["replaced_parts"][0]["name"] == "2in flapper"
    assert any(h["event_type"] == "repair" and h["source"] == "user_reported" for h in prop["service_history"])


def test_clarification_loop_then_limit(client, customer):
    def vague(payload):
        return {"summary": "Could be several things; need the model number.",
                "likely_causes": [{"cause": "Unknown drain blockage", "likelihood": 0.3}],
                "confidence": 0.3, "recommended_action": "clarify",
                "missing_information": ["Photo of the rating plate"]}
    req = submit(client, customer, "Water pooling at the bottom of my dishwasher after it runs.", "appliance")
    cloud = FakeAdapter(vague)
    runner.run_pending(cloud=cloud)
    res = get(client, customer, req["id"])
    assert res["status"] == "needs_clarification"
    qs = res["result"]["clarification"]["questions"]
    assert any(q["kind"] == "photo" for q in qs)

    # Answer round 1 with a photo
    r = client.post(f"/api/repair-requests/{req['id']}/clarifications", headers=customer["headers"],
                    data={"answers": json.dumps({qs[0]["id"]: "Label attached", "bogus": "ignored"})},
                    files=[("files", ("label.jpg", jpeg_bytes(), "image/jpeg"))])
    assert r.status_code == 202, r.text
    runner.run_pending(cloud=cloud)
    assert cloud.calls[-1]["payload"]["clarifications"][0]["answer"].endswith("Label attached</user_content>")
    assert get(client, customer, req["id"])["status"] == "needs_clarification"

    client.post(f"/api/repair-requests/{req['id']}/clarifications", headers=customer["headers"],
                data={"answers": json.dumps({f"r2_{qs[0]['id'][3:]}": "still unsure"})},
                files=[("files", ("more.jpg", jpeg_bytes(), "image/jpeg"))])
    runner.run_pending(cloud=cloud)
    res = get(client, customer, req["id"])
    # Retry limit reached -> explain limitation and offer appropriate next step
    assert res["status"] == "completed" and res["result"]["outcome"] == "professional"
    assert res["result"]["professional_recommendation"]["recommended_trade"]


def gfci_fn(prohibited=False):
    def fn(payload):
        ref = ref_for(payload, "Replacing a GFCI")
        steps = [{"title": "Turn off breaker", "detail": "Switch off the breaker and test with a voltage tester.",
                  "hazard_tags": ["electrical"], "evidence_refs": [ref]},
                 {"title": "Swap the GFCI outlet", "detail": "Move LINE and LOAD wires to the new outlet.",
                  "hazard_tags": ["electrical"], "evidence_refs": [ref]},
                 {"title": "Test and reset", "detail": "Press TEST then RESET.", "evidence_refs": [ref]}]
        if prohibited:
            steps.insert(1, {"title": "Check the panel", "detail": "Remove the panel cover and inspect the breaker.",
                             "evidence_refs": [ref]})
        return {"summary": "The GFCI outlet has failed and no longer resets.",
                "likely_causes": [{"cause": "Failed GFCI outlet", "likelihood": 0.7, "evidence_refs": [ref]}],
                "confidence": 0.75, "recommended_action": "diy", "hazards": ["electrical"],
                "proposed_steps": steps, "parts_needed": [{"part_type": "gfci_outlet"}]}
    return fn


def test_safety_warning_continue_removes_prohibited_steps(client, customer):
    req = submit(client, customer, "Bathroom GFCI outlet won't reset and the outlets are dead.", "electrical")
    runner.run_pending(cloud=FakeAdapter(gfci_fn(prohibited=True)))
    res = get(client, customer, req["id"])
    assert res["status"] == "awaiting_safety_choice"
    codes = {w["code"] for w in res["result"]["safety"]["warnings"]}
    assert {"WARN_ELECTRICAL", "PRO_PANEL"} <= codes

    # Must acknowledge all warnings
    r = client.post(f"/api/repair-requests/{req['id']}/safety-choice", headers=customer["headers"],
                    json={"choice": "continue", "acknowledged_warning_codes": ["WARN_ELECTRICAL"]})
    assert r.status_code == 422
    r = client.post(f"/api/repair-requests/{req['id']}/safety-choice", headers=customer["headers"],
                    json={"choice": "continue", "acknowledged_warning_codes": sorted(codes)})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["outcome"] == "diy_plan" and result["safety"]["acknowledged"] is True
    titles = [s["title"] for s in result["repair_plan"]["steps"]]
    assert "Check the panel" not in titles and len(titles) == 3
    assert "Non-contact voltage tester" in result["repair_plan"]["tools"]
    assert db.fetch_one("SELECT choice FROM safety_acknowledgements WHERE request_id = %s", (req["id"],))["choice"] == "continue"


def test_safety_warning_cancel(client, customer):
    req = submit(client, customer, "GFCI in the kitchen trips and won't reset at all.", "electrical")
    runner.run_pending(cloud=FakeAdapter(gfci_fn()))
    r = client.post(f"/api/repair-requests/{req['id']}/safety-choice", headers=customer["headers"],
                    json={"choice": "cancel"})
    assert r.json()["status"] == "cancelled" and r.json()["result"]["outcome"] == "cancelled"


def test_emergency_skips_ai(client, customer):
    req = submit(client, customer, "I smell gas near the water heater and the pilot is out.", "plumbing")
    cloud = FakeAdapter(lambda p: {})
    runner.run_pending(cloud=cloud)
    res = get(client, customer, req["id"])
    assert res["status"] == "blocked" and cloud.calls == []
    assert res["result"]["safety"]["warnings"][0]["code"] == "EMERGENCY_GAS_LEAK"
    assert res["result"]["professional_recommendation"]["urgency"] == "emergency"


def test_cloud_failure_falls_back_to_local(client, customer, toilet_diagnosis):
    req = submit(client, customer, "Toilet keeps running constantly, water flows into overflow tube.")
    cloud = FakeAdapter(fail="timeout")
    local = FakeAdapter(toilet_diagnosis, provider="local", model="llama-test")
    runner.run_pending(cloud=cloud, local=local)
    r = get(client, customer, req["id"])["result"]
    assert r["outcome"] == "diy_plan" and r["provider"] == "local"
    calls = db.fetch_all("SELECT provider, success FROM model_calls WHERE request_id = %s ORDER BY id", (req["id"],))
    assert [(c["provider"], c["success"]) for c in calls] == [("openai", False), ("local", True)]


def test_total_ai_failure_retries_then_fails(client, customer):
    req = submit(client, customer, "Faucet drips constantly from the spout overnight.")
    bad = FakeAdapter(fail="503")
    runner.run_pending(cloud=bad, local=FakeAdapter(fail="down", provider="local"))
    res = get(client, customer, req["id"])
    assert res["status"] == "processing" and res["stage"] == "waiting_retry"
    # Force remaining attempts to run now.
    for _ in range(3):
        db.execute("UPDATE jobs SET run_after = now() WHERE request_id = %s", (req["id"],))
        runner.run_pending(cloud=bad, local=FakeAdapter(fail="down", provider="local"))
    res = get(client, customer, req["id"])
    assert res["status"] == "failed" and res["result"]["outcome"] == "failure"
    assert db.fetch_one("SELECT 1 FROM alerts WHERE kind = 'job_failed'")


def test_authorization_and_upload_validation(client, customer):
    req = submit(client, customer, "Toilet keeps running constantly at night.")
    _, other = make_user("intruder@example.com")
    assert client.get(f"/api/repair-requests/{req['id']}", headers=other).status_code == 404
    assert client.get(f"/api/media/{req['media'][0]['id'] if req['media'] else '00000000-0000-0000-0000-000000000000'}",
                      headers=other).status_code == 404
    r = client.post("/api/repair-requests", headers=other, data={
        "property_id": customer["property_id"], "service_category": "plumbing", "description": "x" * 20})
    assert r.status_code == 404
    assert client.get("/api/me").status_code == 401
    r = client.post("/api/repair-requests", headers=customer["headers"], data={
        "property_id": customer["property_id"], "service_category": "plumbing", "description": "Toilet runs all day"},
        files=[("files", ("evil.jpg", b"MZ\x90\x00 not a jpeg", "image/jpeg"))])
    assert r.status_code == 400 and "does not match" in r.json()["detail"]


def test_media_is_encrypted_and_served_to_owner(client, customer):
    req = submit(client, customer, "Toilet runs, see the photo of the tank.",
                 files=[("files", ("t.jpg", jpeg_bytes(), "image/jpeg"))])
    m = db.fetch_one("SELECT storage_key FROM media WHERE request_id = %s", (req["id"],))
    from app.storage import get_store
    raw = (get_store().root / m["storage_key"]).read_bytes()
    assert not raw.startswith(b"\xff\xd8")                       # encrypted at rest
    r = client.get(f"/api/media/{req['media'][0]['id']}", headers=customer["headers"])
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8")


def test_admin_review_and_eval_dataset(client, customer, admin, toilet_diagnosis):
    req = submit(client, customer, "Toilet tank refills on its own every 10 minutes.")
    runner.run_pending(cloud=FakeAdapter(toilet_diagnosis))
    out = client.post(f"/api/repair-requests/{req['id']}/outcome", headers=customer["headers"],
                      json={"resolution": "fixed_diy", "rating": 4}).json()
    assert client.get("/api/admin/eval-dataset", headers=customer["headers"]).status_code == 403
    r = client.post(f"/api/admin/outcomes/{out['id']}/review", headers=admin,
                    json={"label": "verified", "approved_for_eval": True})
    assert r.status_code == 200
    rows = client.get("/api/admin/eval-dataset", headers=admin).json()
    assert any(row["request_id"] == req["id"] and row["label"] == "verified" for row in rows)
    m = client.get("/api/admin/metrics", headers=admin).json()
    assert m["model_calls"]


def test_ready_endpoint(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["database"] is True


def test_unselected_equipment_does_not_skew_context(client, customer, toilet_diagnosis):
    db.execute("""INSERT INTO equipment (property_id, service_category, kind, brand, model_number)
                  VALUES (%s, 'plumbing', 'water heater', 'Rheem', 'XE50T06ST45U1')""", (customer["property_id"],))
    submit(client, customer, "My toilet keeps running and refills every few minutes by itself.")
    cloud = FakeAdapter(toilet_diagnosis)
    runner.run_pending(cloud=cloud)
    payload = cloud.calls[0]["payload"]
    assert payload["equipment"] == [] and payload["known_facts"]["model_numbers"] == []
    assert not any("Rheem" in e["title"] for e in payload["retrieved_evidence"][:3])

    submit(client, customer, "The water heater makes no hot water at all since yesterday.")
    runner.run_pending(cloud=cloud)
    payload = cloud.calls[1]["payload"]
    assert payload["known_facts"]["model_numbers"] == ["XE50T06ST45U1"]
    assert payload["retrieved_evidence"][0]["title"].startswith("Rheem")
