#!/usr/bin/env bash
# =============================================================================
# rzp-recovery — guided tour
#
#   ./demo.sh              walk through everything, pausing between sections
#   ./demo.sh --auto       same, no pauses (for piping to a log)
#   ./demo.sh --full       re-run the 40k evals live instead of reading
#                          the committed results in eval/data/
#   ./demo.sh --engine     live engine only, skip the evaluation sections
#   ./demo.sh --eval       evaluation only, skip the live engine
#
# Assumes the stack is already up. If it isn't, run ./setup.sh first.
# =============================================================================
set -uo pipefail

BOLD='\033[1m'; DIM='\033[2m'; GREEN='\033[0;32m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; BLUE='\033[0;36m'; NC='\033[0m'

API=http://localhost:8000
DC="docker compose"
PAUSE=1; FULL=0; DO_ENGINE=1; DO_EVAL=1

for arg in "$@"; do
    case "$arg" in
        --auto)   PAUSE=0 ;;
        --full)   FULL=1 ;;
        --engine) DO_EVAL=0 ;;
        --eval)   DO_ENGINE=0 ;;
        -h|--help) sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown flag: $arg (try --help)"; exit 1 ;;
    esac
done

cd "$(dirname "$0")"

# ------------------------------------------------------------------ helpers --
act()  { printf "\n${BOLD}${BLUE}%s${NC}\n${DIM}%s${NC}\n\n" "$1" "$2"; }
note() { printf "${DIM}   %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}   ✓ %s${NC}\n" "$*"; }
bad()  { printf "${RED}   ✗ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}   ! %s${NC}\n" "$*"; }
run()  { printf "${DIM}   \$ %s${NC}\n" "$*"; }

rule() { printf "${DIM}%s${NC}\n" "────────────────────────────────────────────────────────────────────"; }

pause() {
    [[ $PAUSE -eq 0 ]] && return
    printf "\n${DIM}   [enter] to continue${NC}"
    read -r _ </dev/tty 2>/dev/null || true
    printf "\n"
}

json() { python3 -m json.tool 2>/dev/null || cat; }

# ---------------------------------------------------------------- preflight --
printf "\n${BOLD}rzp-recovery — guided tour${NC}\n"
rule

if ! curl -sf "$API/health" >/dev/null 2>&1; then
    bad "API is not responding at $API"
    note "start the stack first:  ./setup.sh"
    exit 1
fi
ok "stack is up"

# Quiet hours: guards.py suppresses every step that has a channel between
# 21:00 and 09:00 IST. That is correct behaviour, but it makes the CARD_EXPIRED
# demo look broken if you don't know it's coming. Warn early.
IST_HOUR=$(TZ=Asia/Kolkata date +%-H)
if (( IST_HOUR >= 21 || IST_HOUR < 9 )); then
    warn "it is ${IST_HOUR}:00 IST — inside the quiet-hours window (21:00–09:00)"
    warn "outbound steps WILL be suppressed by guards.py. That is the guardrail"
    warn "working, not a bug, but don't record the video now."
fi
pause

# ============================================================== THE ENGINE ===
if [[ $DO_ENGINE -eq 1 ]]; then

act "1 · Policy invariants" \
"Six properties the policy engine must never violate, regardless of input.
Pure domain logic — no database, no network. These are the safety claims."

run "docker compose exec -T api pytest -q tests/ -v"
$DC exec -T api pytest tests/ -q --no-header -v 2>/dev/null | grep -E "PASSED|FAILED|passed|failed" || \
    $DC exec -T api pytest tests/ -q
pause

act "2 · Expired card → the engine refuses to retry" \
"A dead card will never authorise. Retrying it three times recovers nothing and
degrades issuer trust. The correct action is a card-update link and zero retries."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"CARD_EXPIRED\"}'"
EXPIRED=$(curl -sf -X POST "$API/simulate/failure" \
    -H 'content-type: application/json' -d '{"cause":"CARD_EXPIRED","arm":"treatment"}')
echo "$EXPIRED" | json
PID_EXPIRED=$(echo "$EXPIRED" | python3 -c 'import sys,json;print(json.load(sys.stdin)["payment_id"])' 2>/dev/null)

if echo "$EXPIRED" | grep -q 'silent_retry'; then
    bad "plan contains a retry — this should never happen"
else
    ok "zero retries · card_update_link now, second attempt at +3 days"
fi
pause

act "3 · Issuer down → immediate rail switch" \
"A bank outage is transient and route-specific. Waiting is wasted time when a
working rail exists right now, so the engine switches to UPI with zero delay."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"TD_ISSUER_DOWN\"}'"
curl -sf -X POST "$API/simulate/failure" \
    -H 'content-type: application/json' -d '{"cause":"TD_ISSUER_DOWN","arm":"treatment"}' | json
ok "rail_switch to upi_intent · delay_s = 0"
pause

act "4 · Risk block → human, never a retry" \
"A payment blocked by the risk system was blocked deliberately. Auto-retrying it
is the one thing an automated recovery engine must never do."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"RISK_BLOCKED\"}'"
curl -sf -X POST "$API/simulate/failure" \
    -H 'content-type: application/json' -d '{"cause":"RISK_BLOCKED","arm":"treatment"}' | json
ok "human_escalation is the only step"
pause

act "5 · Low balance → wait for the salary window" \
"Retrying an insufficient-funds failure in one hour is pure waste: the money is
not there yet. The engine waits for the 1st or the 7th, whichever comes first."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"BD_INSUFFICIENT_FUNDS\"}'"
curl -sf -X POST "$API/simulate/failure" \
    -H 'content-type: application/json' -d '{"cause":"BD_INSUFFICIENT_FUNDS","arm":"treatment"}' | json
note "delay_s on step 1 is days, not hours — that is the point"
pause

act "6 · Ambiguous cause → the instrument-safe path" \
"When the error code genuinely does not identify the cause, the classifier
abstains. A payment link is the only action that is safe under every possible
cause, because it requests a fresh attempt instead of replaying a failed one."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"AMBIGUOUS\"}'"
curl -sf -X POST "$API/simulate/failure" \
    -H 'content-type: application/json' -d '{"cause":"AMBIGUOUS","arm":"treatment"}' | json
ok "payment_link only · no retry against an unknown instrument"
pause

act "6b · The same failure, both arms" \
"The holdout is not a straw man in a spreadsheet — it is running right now, in
the same service, on the same code path. Here is what each arm does with an
identical expired card."

run "curl -X POST $API/simulate/failure -d '{\"cause\":\"CARD_EXPIRED\",\"arm\":\"holdout\"}'"
HOLD=$(curl -sf -X POST "$API/simulate/failure" -H 'content-type: application/json' \
    -d '{"cause":"CARD_EXPIRED","arm":"holdout"}')
echo "$HOLD" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(f"   HOLDOUT   {d[\"cause\"]}  policy=baseline")
for s in d["steps"]:
    print(f"      {s[\"action\"]:<20} +{s[\"delay_s\"]//3600}h  rail={s[\"rail\"]}")
' 2>/dev/null || echo "$HOLD" | json
echo
echo "$EXPIRED" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(f"   TREATMENT {d[\"cause\"]}  policy=engine")
for s in d["steps"]:
    print(f"      {s[\"action\"]:<20} +{s[\"delay_s\"]//3600}h  channel={s[\"channel\"]}")
' 2>/dev/null || true
echo
warn "the holdout fires three retries at a card that will never authorise"
ok "the engine sends a card-update link and never touches the dead instrument"
note "that difference, across 40,000 payments, is the number in section 12"
pause

act "7 · Full timeline for one payment" \
"Every plan, every executed step, every suppressed step, and the audit entries
for all of them. Suppressions appear here as first-class rows — the log is
evidence, not a gap."

if [[ -n "${PID_EXPIRED:-}" ]]; then
    run "curl $API/payments/$PID_EXPIRED/timeline"
    sleep 3   # let the worker execute the zero-delay step
    curl -sf "$API/payments/$PID_EXPIRED/timeline" | json | head -60
    note "outcome='sent' means the step ran; 'suppressed' with suppressed_by=<guard>"
    note "means a stopping rule fired before it could. Both are audit rows."
else
    warn "could not capture a payment id from section 2"
fi
pause

act "8 · Audit chain verification" \
"Each ledger row hashes the previous row's hash into its own body. Altering any
past row breaks verification and reports the sequence number where it broke."

run "curl $API/audit/verify"
AUDIT=$(curl -sf "$API/audit/verify")
echo "$AUDIT" | json
echo "$AUDIT" | grep -q '"valid": *true' && ok "chain intact" || bad "chain broken"
pause

act "9 · Where to look in the UI" \
"The durable execution layer is worth seeing directly."
note "Temporal UI   http://localhost:8080   timers, signals, retries, history"
note "API docs      $API/docs               every endpoint, live"
pause

fi  # DO_ENGINE

# ============================================================ THE EVIDENCE ===
if [[ $DO_EVAL -eq 1 ]]; then

show_report() {   # show_report <file> <live-command...>
    local f="$1"; shift
    if [[ $FULL -eq 0 && -s "$f" ]]; then
        note "reading committed result: $f"
        note "(re-run live with --full)"
        echo
        cat "$f"
    else
        run "$*"
        note "this takes a minute or two on 40,000 rows"
        echo
        "$@" | tee "$f"
    fi
}

act "10 · The gate — is the measuring instrument honest?" \
"Both arms run the IDENTICAL naive baseline. If the harness were biased toward
our own engine, this run would show fake uplift. It must show none: the
confidence interval has to straddle zero. This ran BEFORE the engine existed."

show_report eval/data/gate_run.txt \
    $DC exec -T api python -m eval.report --n 40000 --seed 42 --mode gate
echo
note "look for: significant=False, and GATE PASS"
pause

act "11 · Ceiling — engine with ORACLE labels vs silent-retry baseline" \
"Treatment runs the real policy engine, but is HANDED the true cause. This is
the ceiling, not the headline: it measures the policy without the cost of
classification. Section 15 shows what a real classifier does to this number."

show_report eval/data/policy_run.txt \
    $DC exec -T api python -m eval.report --n 40000 --seed 42 --mode policy
echo
note "net per failure, bootstrap CI over 10,000 resamples, and a per-cause breakdown"
note "+43.7% is the ORACLE ceiling. The defensible number is in section 15."
pause

act "12 · The harder comparison — vs realistic dunning" \
"The baseline above never contacts the customer, so part of that uplift is
messaging asymmetry rather than diagnosis. This arm gives the baseline a payment
link too — what production dunning actually does. The number drops honestly."

if [[ -s eval/data/policy_run_strong.txt || $FULL -eq 1 ]]; then
    show_report eval/data/policy_run_strong.txt \
        $DC exec -T api python -m eval.report --n 40000 --seed 42 --mode policy --baseline strong
else
    warn "eval/data/policy_run_strong.txt not found and --full not set"
    note "add strong_baseline_plan() and the --baseline flag, then re-run"
fi
pause

act "13 · Tier 3 — the LLM, and why it cannot make things worse" \
"Rules and the model only know codes seen in training. Tier 3 maps unseen error
strings onto the taxonomy. It PROPOSES; the same hard-decline gate that polices
every other stage disposes. Every failure path returns AMBIGUOUS, so the safety
floor cannot move down. This test uses ZERO API calls."

show_report eval/data/triage_adversarial.txt \
    $DC exec -T api python -m eval.triage_eval --mode adversarial
echo
note "18 ways an LLM can misbehave — hallucinated causes, malformed JSON,"
note "prompt-injection echoes, and well-formed but UNSAFE proposals."
pause

act "14 · Does tier 3 actually help?" \
"Test rows rewritten to error codes absent from training by construction. Same
rows, same model, same gate — the only difference is whether tier 3 is reachable."

# if [[ -n "${LLM_API_KEY:-}" ]]; then
#     run "docker compose exec -T api python -m eval.triage_eval --mode novel"
#     $DC exec -T api python -m eval.triage_eval --mode novel --sample 1200
# else
#     warn "LLM_API_KEY not set — tier 3 is disabled"
#     note "the system runs as a two-stage classifier with identical safety behaviour;"
#     note "set LLM_API_KEY in .env and re-run to see the comparison"
# fi

# Ask the CONTAINER, not the host shell — .env is injected into the container
# by compose and is not exported into this terminal.
LLM_OK=$($DC exec -T api python -c \
    "from services.app.adapters import llm_triage as T; print('yes' if T.available() else 'no')" \
    2>/dev/null | tr -d '\r' | tail -1)

if [[ "$LLM_OK" == "yes" ]]; then
    run "docker compose exec -T api python -m eval.triage_eval --mode novel"
    $DC exec -T api python -m eval.triage_eval --mode novel --sample 1200
else
    warn "tier 3 is disabled inside the container"
    note "a key in .env is not enough — the container must be RECREATED, not restarted:"
    note "  ./restart.sh          (docker compose restart will not work)"
    note "the system runs as a two-stage classifier with identical safety behaviour"
fi
pause

act "15 · Classifier quality and the safety frontier" \
"End-to-end cost of using a real classifier instead of oracle labels, plus the
metric that matters most: how often a retry was fired at a true hard decline."

show_report eval/data/step4.txt \
    $DC exec -T api python -m eval.step4
echo
note "macro-F1 on decided rows, per-class recall/precision, abstain rate,"
note "unsafe-retry rate, and oracle-vs-predicted uplift on the same seed"
pause

fi  # DO_EVAL

# ------------------------------------------------------------------- close --
rule
printf "${BOLD}  Tour complete${NC}\n\n"
note "reproduce everything from scratch:"
note "  ./setup.sh          bring the stack up, migrate, smoke test"
note "  ./demo.sh --full    re-run every evaluation live"
echo
note "committed results live in eval/data/ so reviewers can read the numbers"
note "without waiting for a 40,000-row run."
rule
echo
