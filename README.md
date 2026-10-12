# A2Z Co-Pilot: From Live Visibility to Predictive Deal Intervention

a lean working MVP showing how a live manager dashboard can become an operational co-pilot: it notices when a deal is stalling, explains why using evidence, and hands the manager a few options to review. a human decides, and everything is logged.

## The problem

a live dashboard shows managers what is happening in every deal. the harder problem is that managers can't watch it all day, and deals die quietly while an advisor is stuck on the F&I menu, waiting on credit, or recalculating a payment for the fifth time. visibility is only useful if someone is looking at the right deal at the right moment.

## What this MVP does

```
[ Simulated event ledger ]   state entries, product toggles, payment recalcs, clicks
            |
            v
[ Rule engine (deterministic) ]   elapsed time in state, idle time, toggle and recalc counts
            |                      every rule records observed values vs. thresholds
     stall flagged?
      |          |
     no         yes
      |          |
 no LLM call     v
            [ LLM drafts 2-3 options ]   explains the evidence, never decides
                     |
                     v
            [ Manager approves or dismisses ]
                     |
                     v
            [ Audit log ]   flag, suggestion, decision
```

The detector decides *whether* a deal is stalled. The LLM only explains the evidence and proposes review steps. That split keeps the alerting auditable and cheap: no stall, no model call.

## Demo scenarios

| Scenario | What it shows |
|---|---|
| A. F&I menu payment loop | VSC toggled on and off repeatedly while the payment sits over the customer's target |
| B. Credit submitted, advisor idle | Credit went out, advisor looked at the callback and stips, then went quiet |
| C. LTV ceiling breach | Back-end products push the deal past the lender's LTV ceiling |
| D. Desking recalculation loop | Repeated payment recalculations without converging |
| E. Healthy deal | A normal session. The detector stays quiet and no LLM call is made |
| F. Near-miss | Looks like A but stays just under both thresholds, so no alert fires |

Scenarios E and F matter as much as A to D. A detector is only credible if it also knows when to stay silent.

## How detection works

Each rule is data: a workflow state plus conditions on computed metrics. A rule matches only when all of its conditions are met, and every evaluation records the observed value, the threshold, and whether each condition passed. The evidence report passed to the LLM contains the actual numbers that triggered the rule, not just a label.

| Rule | State | Conditions (placeholder thresholds) |
|---|---|---|
| F_I_MENU_PRODUCT_TOGGLE_LOOP | F&I menu | 20+ min in state and 4+ VSC toggles |
| LTV_CEILING_BREACH | F&I menu | 15+ min in state and LTV above the lender ceiling |
| CREDIT_IDLE_STANDSTILL | Credit submitted | 15+ min in state and 10+ min idle |
| DESKING_RECALC_LOOP | Desking | 25+ min in state and 5+ payment recalculations |

Time handling: elapsed time is measured from `state_entered_at` to an explicit `as_of` timestamp, not from the first to the last event in the ledger. The event-window duration is tracked as a separate metric. All timestamps are full ISO datetimes, so midnight rollover works.

## Guardrails

- Suggestions go to the manager only. Nothing is sent to the customer or the lender.
- The model is told to use only facts from the evidence report, to invent no lender decisions or dollar amounts, and never to suggest extending the term, changing the rate, adding products, or obscuring disclosures. Options are review steps.
- Model output is validated before display. If the call fails or the response is unusable, the manager sees a plain "service unavailable, review manually" message, the flag still stands, and diagnostics go to the logs only.
- Every flag, suggestion, failure, and manager decision is written to the audit log.

## What is simulated vs. real

- **Simulated:** the telemetry. Events come from scripted scenarios, replayed against a fixed `as_of` time.
- **Placeholders:** every threshold. They are starting baselines, to be calibrated on real dealership event logs.
- **Real:** the rule engine, the evidence report, the LLM call with validation and error handling, the approval flow, and the audit log.
- **Not built yet:** a trained model. There is no real event data to train on, so this MVP uses transparent rules rather than pretending otherwise.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
export GROQ_API_KEY=your_key_here  # or paste it in the sidebar
streamlit run app.py
```

Inference runs on Groq using `openai/gpt-oss-120b`.

## Scale strategy: AWS target state

1. **Telemetry capture:** the dealer workstation frontend streams state transitions and interaction events into Amazon Kinesis Data Streams. EventBridge Pipes or Lambda consumers pick them up from there.
2. **State analysis:** serverless Lambda functions maintain per-deal state in Amazon DynamoDB (`state_entered_at`, toggle and recalc counters, last action time) and run the rule engine on each event and on a timer for idle detection.
3. **Alert bus:** flagged deals are published to Amazon EventBridge, which fans out to the agentic layer and to the audit store.
4. **Agentic layer:** high-risk triggers route into Amazon Bedrock AgentCore, running the same constrained, structured-output prompt used here. The agent returns options for the manager, never customer-facing actions.
5. **Delivery and audit:** alerts are pushed to the manager's dashboard in real time (for example over API Gateway WebSockets). Manager decisions and outcomes are written to DynamoDB, with an S3 archive for replay.

## Phase 2, with real logs

- Replace hand-set thresholds with per-state hazard (survival) models conditioned on deal attributes such as credit tier, deal complexity, and advisor experience, trained on A2Z's actual event history.
- Add an evaluation harness: precision and recall of stall flags against labeled outcomes, plus how early an alert fires relative to when a customer walks.
- Aggregate flags into a group-level view: which products and stages stall most, and where advisors need coaching.

## What I would need from A2Z to run a pilot

A stream of state transitions and interaction events for one store and one workflow, such as the F&I menu. Time-in-stage and F&I PVR are the natural pilot metrics.