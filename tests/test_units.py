import pytest

from app import ingestion
from app.ai.orchestrator import ProviderHealth, normalize, redact
from app.pipeline.context import extract_facts
from app.pipeline.retrieval import SearchQuery, search
from app.pipeline.safety import apply_acknowledgement, assess
from app.schemas import Diagnosis


def diag(steps, hazards=(), action="diy"):
    return Diagnosis.model_validate({
        "summary": "Test diagnosis summary", "confidence": 0.8, "recommended_action": action,
        "likely_causes": [{"cause": "Something", "likelihood": 0.7}], "hazards": list(hazards),
        "proposed_steps": [{"title": t, "detail": d} for t, d in steps]})


def test_ingestion_rejects_unapproved_and_detects_duplicates():
    meta = ingestion.DocumentMeta(title="X", source="s://x", permission="scraped", approved_by="",
                                  service_category="plumbing", version="1")
    with pytest.raises(ingestion.IngestionRejected):
        ingestion.ingest(b"# Heading\n\nSome text here that is long enough to keep.", "x.md", meta)
    meta.permission, meta.approved_by = "internal", "me"
    text = b"# Section A\n\nThis paragraph has enough words to become a passage for testing.\n"
    assert ingestion.ingest(text, "x.md", meta)["status"] == "ingested"
    assert ingestion.ingest(text, "x.md", meta)["status"] == "unchanged"
    with pytest.raises(ingestion.IngestionRejected):
        ingestion.ingest(text + b"changed", "x.md", meta)


def test_split_extracts_tables_and_sections():
    blocks = ingestion.extract_blocks([(1, "# Parts\n\n| Part | Size |\n|---|---|\n| Flapper | 2 in |\n\nBody text "
                                          "under the parts heading that goes on.")])
    assert any(b.is_table and "Flapper | 2 in" in b.text for b in blocks)
    assert all(b.section == "Parts" for b in blocks)


def test_retrieval_prefers_model_specific_passages():
    ev = search(SearchQuery("electric water heater no hot water element", ["element", "heater"], "plumbing",
                            ["rheem"], ["XE50T06ST45U1"]))
    assert ev[0].title.startswith("Rheem") and ev[0].match.get("model_match")
    other = search(SearchQuery("electric water heater no hot water", ["heater"], "plumbing", ["bradford white"], []))
    assert not any(e.title.startswith("Rheem") for e in other)        # other brands' manuals filtered out


def test_facts_detect_emergencies_and_models():
    f = extract_facts("Rheem XE50T06ST45U1 shows error code E24, and I smell gas in the garage")
    assert "XE50T06ST45U1" in f["model_numbers"] and "E24" in f["error_codes"]
    assert f["mentions_gas_smell"] and "rheem" in f["brands"]


@pytest.mark.parametrize("text", ["Jumper the thermal cutoff", "Remove the panel cover", "Recharge the AC freon",
                                  "Tighten the gas line fitting", "Cap the relief valve"])
def test_prohibited_steps_detected(text):
    a = assess(diag([("Do it", text), ("Safe step", "Wipe the seat clean")]), {}, "appliance")
    assert a.prohibited and a.level in ("warning_required", "high_risk")


def test_gas_line_work_is_high_risk_and_tenant_blocked():
    assert assess(diag([("Fix", "Tighten the gas line fitting")]), {}, "appliance").level == "high_risk"
    d = diag([("Swap outlet", "Move the wires to the new GFCI outlet")], hazards=["electrical"])
    a = assess(d, {}, "electrical")
    assert a.level == "warning_required"
    _, ok, _ = apply_acknowledgement(d, a, {"occupancy": "owner"})
    assert ok
    _, ok, reason = apply_acknowledgement(d, a, {"occupancy": "tenant"})
    assert not ok and "landlord" in reason


def test_safe_when_no_hazards():
    assert assess(diag([("Clean filter", "Rinse the dishwasher filter under warm water")]), {}, "appliance").level == "safe"


def test_normalize_and_redact():
    d = normalize({"summary": "Normalized summary", "causes": [{"cause": "abc", "likelihood": 60}],
                   "confidence": 70, "recommended_action": "call_professional",
                   "parts_needed": [{"part_type": "Anode Rod"}]})
    assert d.confidence == 0.7 and d.recommended_action == "professional" and d.parts_needed[0].part_type == "anode_rod"
    assert redact("email me a@b.com or 555-123-4567 at 12 Oak Street") == "email me [email] or [phone] at [address]"


def test_circuit_breaker_opens_and_half_opens():
    h = ProviderHealth()
    for _ in range(3):
        h.record(False, "boom")
    assert h.is_open()
    h.opened_at -= 120
    assert not h.is_open()


@pytest.mark.parametrize("text,expected", [
    ("No hot water from my electric water heater. Breaker is on, no leaks.", False),
    ("Water is dripping into the electrical panel from the ceiling", True),
    ("The bathroom outlet got wet after the tub overflowed", True),
])
def test_water_near_electric_detection(text, expected):
    assert extract_facts(text)["mentions_water_near_electric"] is expected


def test_simple_gas_tasks_allowed_with_warning():
    d = diag([("Turn the gas control knob to OFF", "Wait 5 minutes for gas to clear."),
              ("Relight the pilot", "Follow the lighting instructions on the label to relight the pilot."),
              ("Clean the flame sensor", "Remove the flame sensor and polish it with fine steel wool.")],
             hazards=["gas"])
    a = assess(d, {}, "gas")
    assert a.level == "warning_required"
    assert "WARN_GAS_APPLIANCE" in {w.code for w in a.warnings}
    _, ok, _ = apply_acknowledgement(d, a, {"occupancy": "owner"})
    assert ok


@pytest.mark.parametrize("title,detail", [
    ("Replace the thermocouple", "Unscrew the thermocouple nut from the gas control valve."),
    ("Replace the gas valve", "Disconnect the gas valve."),
    ("Remove the burner assembly", "Unbolt the burner manifold."),
])
def test_complex_gas_work_goes_to_professional(title, detail):
    d = diag([("Relight the pilot", "Use the label instructions."), (title, detail)], hazards=["gas"])
    a = assess(d, {}, "gas")
    assert a.level == "high_risk" and "PRO_GAS_COMPLEX" in {w.code for w in a.warnings}


def test_gas_smell_still_emergency():
    d = diag([("Relight the pilot", "Use the label instructions.")], hazards=["gas"])
    assert assess(d, {"mentions_gas_smell": True}, "gas").emergency
