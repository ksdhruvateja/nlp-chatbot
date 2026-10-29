"""Phase 6 - Evidence validation and safety.

Validate JSON schema and required fields -> verify evidence references and source
applicability -> check evidence sufficiency and uncertainty -> enough reliable evidence?
  no  -> identify missing details -> generate targeted questions and photo requests
  yes -> run deterministic safety rules -> safety assessment:
         safe             -> safe for DIY guidance
         warning required -> show specific warning and risks -> customer choice
         high risk/unsafe -> block DIY, offer safe alternatives or professional assistance

The safety rules are deliberately deterministic (regex + tag based) so that no model
output can talk its way past them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..config import get_settings
from ..schemas import ClarificationQuestion, Diagnosis, SafetyWarning, Step
from .retrieval import Evidence

# ---------------------------------------------------------------- rule tables
@dataclass(frozen=True)
class Rule:
    code: str
    title: str
    message: str
    risks: tuple[str, ...]
    tags: tuple[str, ...] = ()            # hazard tags that trigger the rule
    pattern: str | None = None            # regex over step text that triggers the rule
    requires_ack: bool = True             # False = advisory: shown, but no acknowledgement needed


# Emergencies: stop everything and tell the customer what to do right now.
EMERGENCY_FACTS: dict[str, SafetyWarning] = {
    "mentions_gas_smell": SafetyWarning(
        code="EMERGENCY_GAS_LEAK", title="Possible gas leak", severity="critical",
        message="Leave the building now. Do not operate switches, appliances or open flames. From outside, call "
                "your gas utility's emergency line or 911.",
        risks=["Explosion", "Fire", "Asphyxiation"]),
    "mentions_co_alarm": SafetyWarning(
        code="EMERGENCY_CO", title="Carbon monoxide alarm", severity="critical",
        message="Get everyone, including pets, into fresh air immediately and call 911. Do not re-enter until "
                "emergency responders say it is safe.",
        risks=["Carbon monoxide poisoning", "Death"]),
    "mentions_sparks": SafetyWarning(
        code="EMERGENCY_ARCING", title="Sparking or scorching electrical component", severity="critical",
        message="If it is safe to reach, switch off the circuit at the breaker. Do not touch the device. Call a "
                "licensed electrician; if there is smoke or flame, call 911.",
        risks=["Electrical fire", "Electric shock"]),
    "mentions_water_near_electric": SafetyWarning(
        code="EMERGENCY_WATER_ELECTRIC", title="Water near electrical equipment", severity="critical",
        message="Do not stand in water or touch the equipment. If you can reach the main breaker from a dry "
                "location, switch it off; otherwise call your utility or an electrician.",
        risks=["Electrocution", "Electrical fire"]),
}

# Prohibited: these steps are never given as DIY instructions.
PROHIBITED: tuple[Rule, ...] = (
    Rule("PRO_GAS_LINE", "Gas piping work", "Gas line connections, fittings and valves must be handled by a "
         "licensed gas fitter or your utility.", ("Gas leak", "Explosion", "Carbon monoxide"),
         pattern=r"gas (line|pipe|piping|fitting|connector|valve|regulator)|tighten.{0,20}gas|(re)?pipe the gas|"
                 r"gas supply (line|connection)|flare fitting"),
    Rule("PRO_PANEL", "Electrical panel interior", "Work inside the breaker panel, on the service entrance or "
         "meter requires a licensed electrician.", ("Electrocution", "Arc flash", "Fire"),
         pattern=r"(remove|open|take off).{0,25}(panel|dead ?front) cover|inside the (breaker |electrical |service )?"
                 r"panel|service (entrance|drop|conductors)|bus ?bar|meter (base|socket)|install (a )?(new )?breaker|"
                 r"replace (the )?breaker"),
    Rule("PRO_LIVE_WORK", "Working on energized circuits", "Never work on circuits that are energized.",
         ("Electrocution",), pattern=r"(while|with) (the )?(power|circuit|breaker) (on|live|energi[sz]ed)|"
                                      r"live (wire|circuit)s?|hot work"),
    Rule("PRO_BYPASS", "Bypassing safety devices", "Safety devices (thermal cutoffs, pressure switches, "
         "flame sensors, GFCI/AFCI, door interlocks) must never be bypassed or jumpered.",
         ("Fire", "Explosion", "Shock"), pattern=r"bypass|jumper (the|a|across)|jump (the|out)|disable (the )?"
         r"(safety|limit|interlock|thermal|cutoff|sensor)|tape (down|over) (the )?(switch|interlock)"),
    Rule("PRO_REFRIGERANT", "Refrigerant handling", "Handling refrigerant requires EPA Section 608 "
         "certification.", ("Frostbite", "Environmental harm", "Legal liability"), tags=("refrigerant",),
         pattern=r"refrigerant|recharge (the )?(system|ac|freon)|freon|add r-?(22|410a|32)"),
    Rule("PRO_STRUCTURAL", "Structural modification", "Load-bearing changes require an engineer and permit.",
         ("Collapse",), tags=("structural",),
         pattern=r"load[- ]bearing|cut (the |a )?(joist|beam|stud|truss|rafter)|remove (the |a )?(wall|beam|post)"),
    Rule("PRO_ASBESTOS", "Asbestos or lead disturbance", "Suspected asbestos or lead must be tested and abated "
         "by certified professionals before disturbing.", ("Cancer", "Lead poisoning"), tags=("asbestos", "lead"),
         pattern=r"sand(ing)? (the )?(old )?paint|scrape (the )?(popcorn|acoustic)|vermiculite|asbestos"),
    Rule("PRO_TPR_PLUG", "Plugging a relief valve", "Never plug or cap a temperature/pressure relief valve.",
         ("Tank explosion",), pattern=r"(plug|cap|block) (the )?(t&p|tpr|relief|pressure relief)"),
)

# Warnings: allowed after the customer acknowledges specific risks.
WARNINGS: tuple[Rule, ...] = (
    Rule("WARN_ELECTRICAL", "Electrical shock risk", "Turn off the circuit at the breaker and confirm it is dead "
         "with a non-contact voltage tester before touching any wiring.", ("Electric shock", "Fire if miswired"),
         tags=("electrical", "high_voltage"), pattern=r"\b(wires?|wiring|outlet|receptacle|light switch|wall switch|switch (box|plate)|"
                                                       r"terminals?|gfci|heating element|thermostat)\b"),
    Rule("WARN_GAS_APPLIANCE", "Gas appliance", "Only follow the manufacturer's lighting/reset instructions. If "
         "you smell gas at any point, stop and leave the building.", ("Gas leak", "Carbon monoxide"),
         tags=("gas", "carbon_monoxide"), pattern=r"pilot|burner|igniter|gas control"),
    Rule("WARN_SCALD", "Scalding water", "Let water cool and open a hot tap before draining. Water heater "
         "water can exceed 120°F/49°C.", ("Burns", "Scalding"), tags=("scald",),
         pattern=r"(drain|flush)(ing)? (the )?(water heater|heater tank|hot water tank)|t&p|"
                 r"temperature (and|&) pressure relief"),
    Rule("WARN_FALL", "Fall hazard", "Use a stable ladder with a helper. Avoid roofs that are wet, steep or "
         "more than one storey.", ("Falls", "Serious injury"), tags=("fall",),
         pattern=r"\b(ladder|roof|gutter|attic joist)"),
    Rule("WARN_WATER", "Water damage", "Shut off the water supply and have towels and a bucket ready.",
         ("Flooding", "Property damage"), tags=("water_damage",),
         pattern=r"(disconnect|remove|loosen).{0,30}(supply line|water line|compression nut|drain line)",
         requires_ack=False),
    Rule("WARN_MOLD", "Mold exposure", "Wear an N95 respirator, gloves and eye protection; areas larger than "
         "10 sq ft need professional remediation.", ("Respiratory irritation",), tags=("mold", "sewage")),
)

HIGH_RISK_CATEGORIES: set[str] = set()   # whole categories where DIY guidance is never offered

# Gas work is DIY only when EVERY step is one of these simple, manufacturer-documented tasks.
# Anything else on a gas appliance (valves, thermocouples, gas controls, burner removal,
# connectors, venting changes) sends the whole job to a professional.
SIMPLE_GAS_TASKS: tuple[tuple[str, str], ...] = (
    ("relight_pilot", r"(re)?light(ing)? (the )?pilot|pilot.{0,30}(relight|light|lighting instructions)"),
    ("furnace_filter", r"(replace|change|clean|install|remove).{0,30}(air |furnace |hvac )?filter"),
    ("flame_sensor", r"(clean|remove|reinstall|polish).{0,30}flame sensor|flame sensor.{0,30}(clean|steel wool|emery)"),
    ("burner_caps", r"(clean|wash|scrub|unclog|clear).{0,30}(burner (caps?|ports?|heads?|grates?)|grates?)|"
                    r"(burner (caps?|ports?|heads?)|grates?).{0,30}(clean|wash|soak|dry|reseat|align)"),
    ("thermostat", r"thermostat.{0,40}(batter(y|ies)|setting|mode|set to|heat mode|schedule)|"
                   r"(replace|change).{0,20}batter(y|ies)"),
    ("power_reset", r"(furnace|service|power) switch|(check|reset).{0,20}(breaker|power)|turn (the )?power"),
    ("vent_clear", r"(clear|remove).{0,30}(snow|leaves|debris|ice|nest).{0,40}(vent|intake|exhaust|flue cap)"),
    ("gas_off", r"turn (the )?(gas )?(control|knob|valve) (knob )?to (off|pilot)|set .{0,20}to (off|pilot)"),
    ("prep", r"^(wait|let .{0,30}cool|unplug|consult|read|check the (label|manual)|locate|open (the )?(door|panel "
             r"door|access door)|close (the )?(door|access)|turn (off|on) (the )?(appliance|furnace|heater)|"
             r"observe|confirm|test|verify)"),
)


def is_gas_job(diag: Diagnosis, service_category: str) -> bool:
    tags = set(diag.hazards) | {t.lower() for s in diag.proposed_steps for t in s.hazard_tags}
    return service_category == "gas" or bool({"gas", "carbon_monoxide"} & tags)


def simple_gas_task(step: Step) -> str | None:
    text = f"{step.title}. {step.detail}".lower()
    for name, pattern in SIMPLE_GAS_TASKS:
        if re.search(pattern, step.title.lower()) or (name != "prep" and re.search(pattern, text)):
            return name
    return None


@dataclass
class EvidenceCheck:
    valid: bool
    errors: list[str] = field(default_factory=list)
    invalid_refs: list[str] = field(default_factory=list)
    inapplicable_refs: list[str] = field(default_factory=list)
    supported_refs: list[str] = field(default_factory=list)
    unsupported_causes: list[str] = field(default_factory=list)
    sufficient: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class SafetyAssessment:
    level: Literal["safe", "warning_required", "high_risk"]
    warnings: list[SafetyWarning] = field(default_factory=list)
    prohibited: list[dict] = field(default_factory=list)        # {step_index, code, title}
    emergency: bool = False

    def to_dict(self) -> dict:
        return {"level": self.level, "emergency": self.emergency,
                "warnings": [w.model_dump() for w in self.warnings], "prohibited_steps": self.prohibited}


# ---------------------------------------------------------------- evidence validation
def validate_evidence(diag: Diagnosis, evidence: list[Evidence], media_refs: dict[str, str],
                      facts: dict) -> EvidenceCheck:
    s = get_settings()
    check = EvidenceCheck(valid=True)
    by_ref = {e.ref: e for e in evidence}
    user_models = set(facts.get("model_numbers") or [])

    def ref_ok(ref: str) -> bool:
        if ref in by_ref:
            # Applicability: a model-specific passage must match the user's model when we know it.
            models = set(by_ref[ref].models)
            if user_models and models and not (user_models & models):
                check.inapplicable_refs.append(ref)
                return False
            return True
        if ref in media_refs:
            return True
        if ref.startswith("F:") and ref[2:] in facts:
            return True
        check.invalid_refs.append(ref)
        return False

    for cause in diag.likely_causes:
        good = [r for r in cause.evidence_refs if ref_ok(r)]
        cause.evidence_refs = good
        if not good:
            check.unsupported_causes.append(cause.cause)
        check.supported_refs += good
    for step in diag.proposed_steps:
        step.evidence_refs = [r for r in step.evidence_refs if ref_ok(r)]
        check.supported_refs += step.evidence_refs
    check.supported_refs = sorted(set(check.supported_refs))
    check.invalid_refs = sorted(set(check.invalid_refs))
    check.inapplicable_refs = sorted(set(check.inapplicable_refs))

    if diag.recommended_action == "diy" and not diag.proposed_steps:
        check.errors.append("DIY recommended but no steps provided")
    check.valid = not check.errors

    knowledge_refs = [r for r in check.supported_refs if r.startswith("K")]
    top = max(diag.likely_causes, key=lambda c: c.likelihood)
    if diag.recommended_action == "clarify":
        check.reasons.append("Model requested clarification")
    if diag.confidence < s.min_confidence:
        check.reasons.append(f"Confidence {diag.confidence:.2f} below threshold {s.min_confidence:.2f}")
    if len(knowledge_refs) < s.min_evidence_passages and diag.recommended_action == "diy":
        check.reasons.append("Diagnosis is not supported by enough approved repair knowledge")
    if not top.evidence_refs:
        check.reasons.append("Most likely cause has no verifiable evidence")
    if not check.valid:
        check.reasons += check.errors
    check.sufficient = not check.reasons
    return check


# ---------------------------------------------------------------- clarification
def generate_questions(diag: Diagnosis, check: EvidenceCheck, facts: dict, round_no: int) -> list[ClarificationQuestion]:
    qs: list[ClarificationQuestion] = []

    def add(qid, question, kind="text", reason="", choices=None):
        if not any(q.id == qid for q in qs):
            qs.append(ClarificationQuestion(id=f"r{round_no}_{qid}", question=question, kind=kind,
                                            reason=reason, choices=choices or []))

    for i, item in enumerate(diag.missing_information[:4]):
        low = item.lower()
        if any(w in low for w in ("photo", "picture", "image", "label", "nameplate", "rating plate")):
            add(f"m{i}", f"Please upload a photo: {item}", "photo", "The model needs to see this")
        else:
            add(f"m{i}", f"Could you tell us: {item}?", reason="Needed to narrow down the cause")
    if not facts.get("model_numbers"):
        add("model", "Please upload a clear photo of the equipment's rating plate / model-number label.", "photo",
            "Brand and model let us match the right manual")
    if not facts.get("image_count"):
        add("photo", "Please upload a photo or short video of the problem area.", "photo",
            "Visual evidence greatly improves accuracy")
    if check.unsupported_causes and len(qs) < 3:
        add("when", "When did the problem start, and did anything change right before (storm, repair, power "
                    "outage, new appliance)?", reason="Helps confirm or rule out likely causes")
        add("freq", "Is the problem constant or intermittent?", "choice", choices=["Constant", "Intermittent",
                                                                                   "Only under certain conditions"])
    return qs[:5]


# ---------------------------------------------------------------- safety rules
def _step_text(step: Step) -> str:
    return f"{step.title} {step.detail} {' '.join(step.tools)} {' '.join(step.materials)}".lower()


def emergency_check(facts: dict) -> SafetyAssessment | None:
    hits = [w for key, w in EMERGENCY_FACTS.items() if facts.get(key)]
    if hits:
        return SafetyAssessment("high_risk", hits, emergency=True)
    return None


def assess(diag: Diagnosis, facts: dict, service_category: str) -> SafetyAssessment:
    emergency = emergency_check(facts)
    if emergency:
        return emergency

    hazards = set(diag.hazards) | {t.lower() for s in diag.proposed_steps for t in s.hazard_tags}
    prohibited: list[dict] = []
    triggered_prohibited: dict[str, Rule] = {}
    for i, step in enumerate(diag.proposed_steps):
        text = _step_text(step)
        for rule in PROHIBITED:
            if (rule.pattern and re.search(rule.pattern, text)) or (set(rule.tags) & {t.lower() for t in step.hazard_tags}):
                prohibited.append({"step_index": i, "code": rule.code, "title": step.title})
                triggered_prohibited[rule.code] = rule
                break

    warnings: dict[str, SafetyWarning] = {}
    all_text = " ".join(_step_text(s) for s in diag.proposed_steps)
    for rule in WARNINGS:
        if (set(rule.tags) & hazards) or (rule.pattern and re.search(rule.pattern, all_text)):
            warnings[rule.code] = SafetyWarning(code=rule.code, title=rule.title, message=rule.message,
                                                risks=list(rule.risks),
                                                severity="warning" if rule.requires_ack else "advisory")
    for code, rule in triggered_prohibited.items():
        warnings[code] = SafetyWarning(code=code, title=rule.title, message=rule.message, risks=list(rule.risks),
                                       severity="high")
    if facts.get("mentions_asbestos_or_lead"):
        r = next(r for r in PROHIBITED if r.code == "PRO_ASBESTOS")
        warnings[r.code] = SafetyWarning(code=r.code, title=r.title, message=r.message, risks=list(r.risks),
                                         severity="high")

    gas_complex: list[str] = []
    if is_gas_job(diag, service_category):
        gas_complex = [st.title for st in diag.proposed_steps if not simple_gas_task(st)]
        if gas_complex:
            warnings["PRO_GAS_COMPLEX"] = SafetyWarning(
                code="PRO_GAS_COMPLEX", title="Gas appliance repair beyond simple maintenance", severity="high",
                message="Only simple tasks (relighting a pilot per the label, filters, flame-sensor cleaning, "
                        "burner caps) are offered as DIY on gas appliances. This repair needs a licensed gas "
                        "technician: " + "; ".join(gas_complex[:3]),
                risks=["Gas leak", "Carbon monoxide", "Fire"])
        elif diag.proposed_steps:
            warnings.setdefault("WARN_GAS_APPLIANCE", SafetyWarning(
                code="WARN_GAS_APPLIANCE", title="Gas appliance", severity="warning",
                message=next(r.message for r in WARNINGS if r.code == "WARN_GAS_APPLIANCE"),
                risks=["Gas leak", "Carbon monoxide"]))

    remaining = len(diag.proposed_steps) - len(prohibited)
    gas_line_work = any(p["code"] == "PRO_GAS_LINE" for p in prohibited)
    if (service_category in HIGH_RISK_CATEGORIES or gas_line_work or gas_complex or diag.recommended_action == "professional"
            or (diag.proposed_steps and remaining == 0)):
        return SafetyAssessment("high_risk", list(warnings.values()), prohibited)
    if prohibited or any(w.severity != "advisory" for w in warnings.values()):
        return SafetyAssessment("warning_required", list(warnings.values()), prohibited)
    return SafetyAssessment("safe", list(warnings.values()), [])


def apply_acknowledgement(diag: Diagnosis, assessment: SafetyAssessment, passport: dict) -> tuple[Diagnosis, bool, str]:
    """After the customer chooses to continue: remove prohibited guidance and decide
    whether what remains is still safe and permitted. Returns (filtered, permitted, reason)."""
    banned = {p["step_index"] for p in assessment.prohibited}
    kept = [s for i, s in enumerate(diag.proposed_steps) if i not in banned]
    filtered = diag.model_copy(update={"proposed_steps": kept}, deep=True)
    if not kept:
        return filtered, False, "Every repair step requires a licensed professional."
    if banned and len(banned) / len(diag.proposed_steps) > 0.5:
        return filtered, False, "Most of the repair requires licensed work; partial DIY could leave it unsafe."
    if passport.get("occupancy") == "tenant" and any(w.code in ("WARN_ELECTRICAL", "WARN_GAS_APPLIANCE")
                                                     for w in assessment.warnings):
        return filtered, False, "Tenants should ask their landlord to arrange electrical or gas work."
    if any(w.severity == "critical" for w in assessment.warnings):
        return filtered, False, "This situation is an emergency."
    return filtered, True, ""


def safe_alternatives(diag: Diagnosis | None, assessment: SafetyAssessment, service_category: str) -> dict:
    trade = {"electrical": "licensed electrician", "gas": "licensed gas fitter / HVAC technician",
             "hvac": "HVAC technician", "plumbing": "licensed plumber", "roofing": "roofing contractor",
             "structural": "structural engineer or general contractor", "appliance": "appliance repair technician"
             }.get(service_category, "qualified professional")
    codes = {w.code for w in assessment.warnings}
    if "PRO_REFRIGERANT" in codes:
        trade = "EPA 608-certified HVAC technician"
    if "PRO_PANEL" in codes or "EMERGENCY_ARCING" in codes:
        trade = "licensed electrician"
    if "PRO_GAS_LINE" in codes or "EMERGENCY_GAS_LEAK" in codes:
        trade = "licensed gas fitter or your gas utility"
    safe_actions = ["Keep the area clear and prevent others from using the affected equipment."]
    if service_category in ("electrical", "appliance"):
        safe_actions.append("Switch off the affected circuit at the breaker if you can do so from a dry location.")
    if service_category in ("plumbing",):
        safe_actions.append("Close the nearest shutoff valve (or main water shutoff) to limit water damage.")
    if service_category in ("gas", "hvac"):
        safe_actions.append("If you smell gas, leave immediately and call the gas utility from outside.")
    safe_actions.append("Take photos of the problem and equipment labels to share with the professional.")
    return {"recommended_trade": trade, "urgency": "emergency" if assessment.emergency else
            ("soon" if assessment.level == "high_risk" else "when convenient"),
            "safe_actions_now": safe_actions,
            "what_to_tell_the_pro": diag.summary if diag else None,
            "book_via_fixbridge": True}
