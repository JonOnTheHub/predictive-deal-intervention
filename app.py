import json
import logging
import operator
import os
import uuid
from datetime import datetime

import streamlit as st
from groq import Groq


# =========================================================
# 1. CONFIG
# =========================================================

MODEL = "openai/gpt-oss-120b"
ACTION_EVENTS = {"product_toggle", "payment_recalc", "ui_click"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("a2z_copilot")

st.set_page_config(layout="wide", page_title="A2Z Co-Pilot MVP")

st.session_state.setdefault("alert", None)
st.session_state.setdefault("audit_log", [])


# =========================================================
# 2. CREDENTIALS
# =========================================================

env_key = os.environ.get("GROQ_API_KEY", "")

with st.sidebar:
    st.header("🔑 API Credentials")
    api_key_input = st.text_input(
        "Enter Groq API Key:",
        value=env_key,
        type="password",
        help="Get an API key from the Groq Console.",
    )
    st.caption("The key is used at runtime only and is not stored.")


def initialize_groq_client(api_key: str):
    if not api_key:
        return None
    try:
        return Groq(api_key=api_key)
    except Exception:
        logger.exception("Groq client initialization failed")
        return None


client = initialize_groq_client(api_key_input)

if not client:
    st.sidebar.warning("Missing or invalid API key. Add your Groq key above.")


# =========================================================
# 3. DETECTION RULES (placeholder thresholds)
# =========================================================
# Each rule is data: a workflow state plus conditions on computed metrics.
# A rule matches only when ALL of its conditions are met.

RULES = [
    {
        "code": "F_I_MENU_PRODUCT_TOGGLE_LOOP",
        "state": "F_I_MENU",
        "summary": "Advisor keeps toggling the VSC on and off without moving the deal forward.",
        "conditions": [
            {"metric": "elapsed_in_state_min", "op": ">=", "threshold": 20},
            {"metric": "vsc_product_event_count", "op": ">=", "threshold": 4},
        ],
    },
    {
        "code": "LTV_CEILING_BREACH",
        "state": "F_I_MENU",
        "summary": "Deal is above the lender's LTV ceiling and the advisor has stalled.",
        "conditions": [
            {"metric": "elapsed_in_state_min", "op": ">=", "threshold": 15},
            {"metric": "ltv_over_ceiling_pts", "op": ">", "threshold": 0},
        ],
    },
    {
        "code": "CREDIT_IDLE_STANDSTILL",
        "state": "CREDIT_SUBMITTED",
        "summary": "Credit was submitted and the advisor has gone idle.",
        "conditions": [
            {"metric": "elapsed_in_state_min", "op": ">=", "threshold": 15},
            {"metric": "idle_min", "op": ">=", "threshold": 10},
        ],
    },
    {
        "code": "DESKING_RECALC_LOOP",
        "state": "DESKING_FINANCE",
        "summary": "Advisor keeps recalculating payments without converging on a structure.",
        "conditions": [
            {"metric": "elapsed_in_state_min", "op": ">=", "threshold": 25},
            {"metric": "payment_recalc_count", "op": ">=", "threshold": 5},
        ],
    },
]

OPS = {">=": operator.ge, ">": operator.gt}


# =========================================================
# 4. DETERMINISTIC STREAM PROCESSING
# =========================================================

def parse_ts(value: str) -> datetime:
    """Full ISO timestamps, so date rollover (midnight) is handled correctly."""
    return datetime.fromisoformat(value)


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def fmt_delta(delta):
    if delta is None:
        return None
    sign = "+" if delta >= 0 else "-"
    return f"{sign}${abs(delta)}"


def compute_metrics(events: list, entered: datetime, now: datetime, deal: dict) -> dict:
    """
    elapsed_in_state_min: now - state_entered_at (the real time in state)
    event_window_min:     first event to last event (kept separately on purpose)
    idle_min:             now - last advisor action (or state entry if none)
    """
    actions = [e for e in events if e["event_type"] in ACTION_EVENTS]
    last_action = parse_ts(actions[-1]["timestamp"]) if actions else entered

    if len(events) > 1:
        window = minutes_between(
            parse_ts(events[0]["timestamp"]), parse_ts(events[-1]["timestamp"])
        )
    else:
        window = 0.0

    current_pay = deal.get("current_monthly_payment")
    target_pay = deal.get("target_monthly_payment")
    pay_delta = (
        current_pay - target_pay
        if current_pay is not None and target_pay is not None
        else None
    )

    current_ltv = deal.get("current_ltv")
    max_ltv = deal.get("max_ltv")
    ltv_over = (
        current_ltv - max_ltv
        if current_ltv is not None and max_ltv is not None
        else None
    )

    return {
        "elapsed_in_state_min": round(minutes_between(entered, now), 1),
        "idle_min": round(minutes_between(last_action, now), 1),
        "event_window_min": round(window, 1),
        "vsc_product_event_count": sum(
            1
            for e in events
            if e["event_type"] == "product_toggle" and e.get("target_product") == "VSC"
        ),
        "payment_recalc_count": sum(
            1 for e in events if e["event_type"] == "payment_recalc"
        ),
        "payment_vs_target": fmt_delta(pay_delta),
        "current_ltv": current_ltv,
        "max_ltv": max_ltv,
        "ltv_over_ceiling_pts": ltv_over,
    }


def evaluate_rules(state: str, metrics: dict) -> list:
    """Run every rule for this state and record exactly why each did or didn't match."""
    results = []

    for rule in RULES:
        if rule["state"] != state:
            continue

        conditions = []
        for cond in rule["conditions"]:
            observed = metrics.get(cond["metric"])
            met = observed is not None and OPS[cond["op"]](observed, cond["threshold"])
            conditions.append(
                {
                    "metric": cond["metric"],
                    "op": cond["op"],
                    "threshold": cond["threshold"],
                    "observed": observed,
                    "met": met,
                }
            )

        matched = all(c["met"] for c in conditions)

        if matched:
            explanation = (
                f"{rule['summary']} Triggered because "
                + "; ".join(
                    f"{c['metric']} {c['observed']} {c['op']} {c['threshold']}"
                    for c in conditions
                )
                + "."
            )
        else:
            explanation = "Not triggered. Unmet: " + "; ".join(
                f"{c['metric']} is {c['observed']} (needs {c['op']} {c['threshold']})"
                for c in conditions
                if not c["met"]
            )

        results.append(
            {
                "rule": rule["code"],
                "matched": matched,
                "risk": "HIGH" if matched else "LOW",
                "explanation": explanation,
                "conditions": conditions,
            }
        )

    return results


def analyze_deal(scenario: dict) -> dict:
    """Build the full evidence report for one deal at a defined 'now'."""
    entered = parse_ts(scenario["state_entered_at"])
    now = parse_ts(scenario["as_of"])

    # Only events inside [state entry, now] belong to this state's analysis.
    events = sorted(
        (e for e in scenario["ledger"] if entered <= parse_ts(e["timestamp"]) <= now),
        key=lambda e: parse_ts(e["timestamp"]),
    )

    metrics = compute_metrics(events, entered, now, scenario["deal_metrics"])
    rule_results = evaluate_rules(scenario["state"], metrics)
    triggered = [r for r in rule_results if r["matched"]]

    return {
        "deal_state": scenario["state"],
        "state_entered_at": scenario["state_entered_at"],
        "as_of": scenario["as_of"],
        "is_anomaly_detected": bool(triggered),
        "risk": "HIGH" if triggered else "LOW",
        "metrics": metrics,
        "triggered_rules": triggered,
        "rules_evaluated": rule_results,
        "recent_events": events[-5:],
    }


# =========================================================
# 5. LLM LAYER (explains and proposes, never decides)
# =========================================================

SYSTEM_INSTRUCTION = """
You are a decision-support assistant for a dealership desk manager.
A rule-based detector has already flagged a stalled deal. Explain what the
evidence shows and give the manager 2-3 options to review. You do not decide,
and nothing you write is shown to the customer.

Rules:
- Use ONLY facts in the evidence report. Do not invent lender decisions,
  dollar amounts, customer details, or approval conditions. If the report does
  not say why the deal is stuck, say what the evidence shows and recommend the
  manager check.
- Options are review steps for the manager, such as checking lender status,
  reviewing stips with the advisor, revisiting the customer's payment ceiling
  openly with the customer, or comparing product tiers.
- Never suggest extending the term, changing the rate or APR, adding or
  padding products, removing or obscuring disclosures, or any action taken on
  the customer's behalf without the manager.
- Use dealership terms (desk, stips, lender, back-end, LTV) where the evidence
  supports them.

Respond with ONLY a JSON object, no markdown fences:
{"reason": "one sentence", "evidence": ["short fact from the report"],
 "options": [{"label": "short title", "detail": "one sentence"}]}
Give 2 or 3 options.
""".strip()


def parse_suggestion(raw):
    """Validate the model output. Returns a clean dict, or None if unusable."""
    if not raw:
        return None

    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    reason = data.get("reason")
    options = data.get("options")
    if not isinstance(reason, str) or not reason.strip() or not isinstance(options, list):
        return None

    clean_options = [
        {"label": o["label"].strip(), "detail": o["detail"].strip()}
        for o in options[:3]
        if isinstance(o, dict)
        and isinstance(o.get("label"), str)
        and isinstance(o.get("detail"), str)
    ]
    if len(clean_options) < 2:
        return None

    evidence = data.get("evidence")
    evidence = [x for x in evidence if isinstance(x, str)] if isinstance(evidence, list) else []

    return {"reason": reason.strip(), "evidence": evidence, "options": clean_options}


def generate_manager_intervention(evidence: dict) -> dict:
    """
    Returns {"ok": True, "suggestion": {...}} or {"ok": False, "error": "..."}.
    The error is safe to show a manager; diagnostics go to the logs only.
    """
    if not client:
        return {"ok": False, "error": "No Groq API key configured."}

    payload = {
        "deal_state": evidence["deal_state"],
        "as_of": evidence["as_of"],
        "metrics": evidence["metrics"],
        "triggered_rules": evidence["triggered_rules"],
        "recent_events": evidence["recent_events"],
    }

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {
                    "role": "user",
                    "content": "Evidence report:\n" + json.dumps(payload, indent=2),
                },
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content
    except Exception:
        logger.exception("Groq request failed")
        return {"ok": False, "error": "The intervention service is unavailable."}

    suggestion = parse_suggestion(raw)
    if suggestion is None:
        logger.error("Unusable model output: %r", raw)
        return {"ok": False, "error": "The intervention service returned an unusable response."}

    return {"ok": True, "suggestion": suggestion}


# =========================================================
# 6. SIMULATED SCENARIOS (raw telemetry, no diagnoses)
# =========================================================

DAY = "2026-10-12"


def at(clock: str) -> str:
    return f"{DAY}T{clock}"


def ev(clock: str, event_type: str, **extra) -> dict:
    return {"timestamp": at(clock), "event_type": event_type, **extra}


SCENARIOS = {
    "A: F&I menu payment loop": {
        "deal_id": "DEAL-8831",
        "note": "Advisor cycles the VSC on and off while the payment sits over the customer's target.",
        "state": "F_I_MENU",
        "state_entered_at": at("10:02:00"),
        "as_of": at("10:27:00"),
        "deal_metrics": {"target_monthly_payment": 600, "current_monthly_payment": 685},
        "ledger": [
            ev("10:02:00", "state_entry"),
            ev("10:05:12", "product_toggle", target_product="VSC", action="add"),
            ev("10:07:43", "product_toggle", target_product="VSC", action="remove"),
            ev("10:10:08", "product_toggle", target_product="VSC", action="add"),
            ev("10:13:21", "product_toggle", target_product="VSC", action="remove"),
            ev("10:17:34", "payment_recalc"),
            ev("10:22:05", "product_toggle", target_product="VSC", action="add"),
        ],
    },
    "B: Credit submitted, advisor idle": {
        "deal_id": "DEAL-1104",
        "note": "Credit went out, the advisor looked at the callback and stips tab, then went quiet.",
        "state": "CREDIT_SUBMITTED",
        "state_entered_at": at("14:15:00"),
        "as_of": at("14:33:00"),
        "deal_metrics": {"target_monthly_payment": 450, "current_monthly_payment": 450},
        "ledger": [
            ev("14:15:00", "state_entry"),
            ev("14:16:10", "ui_click", element="view_callback"),
            ev("14:17:00", "ui_click", element="stips_tab"),
        ],
    },
    "C: LTV ceiling breach": {
        "deal_id": "DEAL-5541",
        "note": "Back-end products pushed the deal past the lender's LTV ceiling.",
        "state": "F_I_MENU",
        "state_entered_at": at("11:00:00"),
        "as_of": at("11:19:00"),
        "deal_metrics": {
            "target_monthly_payment": 500,
            "current_monthly_payment": 575,
            "current_ltv": 132,
            "max_ltv": 115,
        },
        "ledger": [
            ev("11:00:00", "state_entry"),
            ev("11:04:15", "product_toggle", target_product="VSC", action="add"),
            ev("11:06:22", "product_toggle", target_product="GAP", action="add"),
        ],
    },
    "D: Desking recalculation loop": {
        "deal_id": "DEAL-9902",
        "note": "Advisor recalculates the payment again and again without converging.",
        "state": "DESKING_FINANCE",
        "state_entered_at": at("16:40:00"),
        "as_of": at("17:12:00"),
        "deal_metrics": {"target_monthly_payment": 700, "current_monthly_payment": 765},
        "ledger": [
            ev("16:40:00", "state_entry"),
            ev("16:43:10", "payment_recalc"),
            ev("16:46:15", "payment_recalc"),
            ev("16:51:22", "payment_recalc"),
            ev("16:55:40", "payment_recalc"),
            ev("17:01:10", "payment_recalc"),
        ],
    },
    "E: Healthy deal (should NOT alert)": {
        "deal_id": "DEAL-2210",
        "note": "Normal F&I session: quick, within payment target, inside the LTV ceiling.",
        "state": "F_I_MENU",
        "state_entered_at": at("13:00:00"),
        "as_of": at("13:09:00"),
        "deal_metrics": {
            "target_monthly_payment": 600,
            "current_monthly_payment": 598,
            "current_ltv": 98,
            "max_ltv": 115,
        },
        "ledger": [
            ev("13:00:00", "state_entry"),
            ev("13:03:30", "product_toggle", target_product="VSC", action="add"),
            ev("13:05:10", "product_toggle", target_product="GAP", action="add"),
            ev("13:06:00", "payment_recalc"),
        ],
    },
    "F: Near-miss (just under threshold)": {
        "deal_id": "DEAL-3377",
        "note": "Looks like scenario A but stays just under both thresholds, so the detector stays quiet.",
        "state": "F_I_MENU",
        "state_entered_at": at("15:00:00"),
        "as_of": at("15:19:00"),
        "deal_metrics": {
            "target_monthly_payment": 600,
            "current_monthly_payment": 640,
            "current_ltv": 104,
            "max_ltv": 115,
        },
        "ledger": [
            ev("15:00:00", "state_entry"),
            ev("15:04:00", "product_toggle", target_product="VSC", action="add"),
            ev("15:08:30", "product_toggle", target_product="VSC", action="remove"),
            ev("15:14:10", "product_toggle", target_product="VSC", action="add"),
        ],
    },
}


# =========================================================
# 7. ALERT STATE, DECISIONS, AUDIT LOG
# =========================================================

def log_event(alert: dict, event: str, detail: str) -> None:
    st.session_state.audit_log.append(
        {
            "time": datetime.now().isoformat(timespec="seconds"),
            "alert_id": alert["alert_id"],
            "deal_id": alert["deal_id"],
            "event": event,
            "detail": detail,
        }
    )


def clear_alert() -> None:
    st.session_state.alert = None


def decide(decision: str, option_label=None) -> None:
    alert = st.session_state.alert
    if alert is None or alert["decision"] is not None:
        return
    alert["decision"] = {"decision": decision, "option": option_label}
    log_event(alert, f"MANAGER_{decision}", option_label or "-")


def run_pipeline(scenario_name: str) -> None:
    scenario = SCENARIOS[scenario_name]
    evidence = analyze_deal(scenario)

    alert = {
        "alert_id": uuid.uuid4().hex[:8],
        "scenario": scenario_name,
        "deal_id": scenario["deal_id"],
        "evidence": evidence,
        "result": None,
        "decision": None,
    }
    st.session_state.alert = alert

    # Detection and intervention are separate: no anomaly means no LLM call.
    if not evidence["is_anomaly_detected"]:
        log_event(alert, "CHECKED_NO_ANOMALY", "all rules below threshold")
        return

    codes = ", ".join(r["rule"] for r in evidence["triggered_rules"])
    log_event(alert, "STALL_FLAGGED", codes)

    with st.spinner("Drafting options for the manager..."):
        result = generate_manager_intervention(evidence)

    alert["result"] = result
    if result["ok"]:
        log_event(alert, "SUGGESTION_GENERATED", result["suggestion"]["reason"])
    else:
        log_event(alert, "SUGGESTION_FAILED", result["error"])


# =========================================================
# 8. RENDERING
# =========================================================

def render_rules(rules: list) -> None:
    for rule in rules:
        status = "🔴 MATCHED" if rule["matched"] else "⚪ not matched"
        st.markdown(f"**{rule['rule']}** — {status} (risk {rule['risk']})")
        st.caption(rule["explanation"])
        st.table(
            [
                {
                    "condition": f"{c['metric']} {c['op']} {c['threshold']}",
                    "observed": str(c["observed"]),
                    "met": "yes" if c["met"] else "no",
                }
                for c in rule["conditions"]
            ]
        )


def render_alert(alert: dict) -> None:
    evidence = alert["evidence"]

    st.subheader("Step 1: Detector (deterministic rules)")

    if evidence["is_anomaly_detected"]:
        codes = ", ".join(r["rule"] for r in evidence["triggered_rules"])
        st.error(f"Stall flagged on {alert['deal_id']}. Risk {evidence['risk']}: {codes}")
    else:
        st.success(
            f"{alert['deal_id']}: no anomaly detected. Workflow is within configured "
            "thresholds, so the LLM was not called."
        )

    with st.expander("Why the detector decided this", expanded=evidence["is_anomaly_detected"]):
        render_rules(evidence["rules_evaluated"])

    with st.expander("Full evidence report (JSON)"):
        st.json(evidence)

    if not evidence["is_anomaly_detected"]:
        return

    st.subheader("Step 2: Options for the manager")
    result = alert["result"]

    if not result or not result["ok"]:
        error = result["error"] if result else "No result available."
        st.error(
            f"{error} No suggestion was generated. The flag above still stands, "
            "so review this deal manually."
        )
    else:
        suggestion = result["suggestion"]
        st.warning(f"⚠️ {suggestion['reason']}")

        if suggestion["evidence"]:
            st.markdown("**Evidence cited**")
            for fact in suggestion["evidence"]:
                st.markdown(f"- {fact}")

        st.markdown("**Options (for manager review)**")
        for i, option in enumerate(suggestion["options"], start=1):
            st.markdown(f"{i}. **{option['label']}**: {option['detail']}")

    st.subheader("Step 3: Manager decision")

    if alert["decision"] is not None:
        decision = alert["decision"]
        if decision["decision"] == "APPROVED":
            st.success(f"Approved and logged: {decision['option']}")
        else:
            st.info("Dismissed and logged.")
        return

    st.caption(
        "Nothing is sent to the customer or the lender. Decisions are logged for the audit trail."
    )

    if result and result["ok"]:
        for i, option in enumerate(result["suggestion"]["options"], start=1):
            st.button(
                f"Approve option {i}: {option['label']}",
                key=f"approve_{alert['alert_id']}_{i}",
                on_click=decide,
                args=("APPROVED", option["label"]),
            )

    st.button(
        "Dismiss alert",
        key=f"dismiss_{alert['alert_id']}",
        on_click=decide,
        args=("DISMISSED",),
    )


# =========================================================
# 9. PAGE LAYOUT
# =========================================================

st.title("🚀 A2Z Co-Pilot: Predictive Deal Intervention")
st.caption(
    "MVP prototype. Simulated telemetry replay -> rule-based stall detection -> "
    "LLM-drafted options -> manager approval and audit log. "
    "Thresholds are placeholder baselines to calibrate on real A2Z event logs."
)
st.divider()

col1, col2 = st.columns(2)

with col1:
    st.header("🛒 Upstream Telemetry")

    scenario_name = st.selectbox(
        "Select a simulated deal:",
        list(SCENARIOS.keys()),
        key="scenario_choice",
        on_change=clear_alert,
    )
    scenario = SCENARIOS[scenario_name]
    st.caption(scenario["note"])

    st.json(
        {
            "deal_id": scenario["deal_id"],
            "state": scenario["state"],
            "state_entered_at": scenario["state_entered_at"],
            "as_of": scenario["as_of"],
            "deal_metrics": scenario["deal_metrics"],
            "raw_events": scenario["ledger"],
        }
    )

    run_clicked = st.button("Process event stream", type="primary", key="run_pipeline")

with col2:
    st.header("🚨 Desk Manager Dashboard")

    if run_clicked:
        run_pipeline(scenario_name)

    if st.session_state.alert:
        render_alert(st.session_state.alert)
    else:
        st.write("Awaiting upstream event stream...")

st.divider()
st.subheader("📋 Audit Log")

if st.session_state.audit_log:
    st.dataframe(list(reversed(st.session_state.audit_log)))
else:
    st.caption("No events yet. Process a deal to start the log.")