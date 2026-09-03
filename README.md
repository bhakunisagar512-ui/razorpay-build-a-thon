# rzp-recovery — Decline-Taxonomy Recovery Engine

**Razorpay AI Buildathon · Track 3 (AI Revenue Recovery)**

Most failed-payment recovery is cause-blind: a payment fails, the system retries
it three times on a fixed schedule. That is the right action for roughly a third
of failures and actively harmful for another third — retrying an expired card or
a risk-blocked payment recovers nothing and degrades issuer trust.

This is the other approach. Diagnose the cause first, then execute the
intervention that fits it, under stopping rules, with every action and every
suppression written to a tamper-evident log — and measure the whole thing
against a holdout.

---

## Results

| Comparison | Net recovery per failure | 95% CI | Significant |
|---|---|---|---|
| vs. silent-retry baseline | **+25.3%** | [+₹286, +₹467] | yes |
| vs. realistic dunning baseline (retries **+ payment link**) | **+10.6%** | [+₹87, +₹319] | yes |
| with oracle labels (ceiling) | +41.8% | [+₹551, +₹737] | yes |

n = 40,000 synthetic failures, seed 42, deterministic 10% holdout.

**The second row is the number worth defending.** The first baseline never
contacts the customer, so part of that 25% is messaging asymmetry rather than
diagnosis. Real dunning systems send links. We report both.

**Harness validated first.** With both arms running the identical baseline,
uplift is statistically zero (CI [−₹172.68, +₹51.52]). That null run happened
*before* the policy engine was written. It is the reason the numbers above mean
anything.

---

## Reproduce in two commands

```bash
./setup.sh            # build, migrate, smoke test — no API keys needed
./demo.sh             # guided tour of every capability, narrated
```

Committed evaluation output lives in `eval/data/` so you can read the numbers
without waiting for a 40,000-row run. To regenerate them live: `./demo.sh --full`.

| URL | What |
|---|---|
| http://localhost:8000/docs | API, live |
| http://localhost:8080 | Temporal UI — workflows, timers, signals |

---

## The methodology argument

Building an eval after the engine tells you whether you like your own work.
Building it first tells you whether the work is any good. The order here was:

1. **Taxonomy** — canonical causes, and which are retryable at all.
2. **Causal generator** — sample the *hidden cause* first, then emit an
   *observable error code* from a distribution conditioned on it, with a
   deliberate collision zone that five causes share. A generator with a clean
   1:1 code→cause mapping lets the classifier grade its own homework.
3. **Gate run** — both arms on the naive baseline. CI must straddle zero.
4. **Policy engine**, then measured against that validated harness.

Step 4 lost the first time. Policy v1 issued a single retry on PSP timeouts
against the baseline's three, on a highly retryable cause, and came out behind.
The eval caught it; code review would not have. The fix — verify no silent
success first (never double-charge), then exponential backoff — is documented in
`policy.py` at the point of the change.

`eval/` imports `build_plan()` from `services/app/domain/policy.py`. The
evaluated policy and the served policy are the same function; there is no drift
between what was measured and what ships.

---

## How it works

**Detect.** `payment.failed` webhooks enter with HMAC signature verification and
`event_id` idempotency backed by a unique constraint. Duplicate deliveries are
rejected before anything is counted — the eval drops them too, because not doing
so silently inflates recovered rupees.

**Diagnose.** Three stages. Rules for the error tuples one cause dominates;
gradient boosting for the collision zone; an LLM for codes neither has seen.
Then a safety gate applied identically to all three: misclassification is
asymmetric, so if confidence is low or hard-decline probability mass is
non-trivial, the classifier returns `AMBIGUOUS` rather than guess. It abstains
16.7% of the time by design, and an LLM proposal gets no more trust than a rule hit.

Try it without side effects:

```bash
curl -s localhost:8000/diagnose -H 'content-type: application/json' \
  -d '{"gw_error_code":"GATEWAY_ERROR","gw_error_source":"gateway","gw_error_step":"authorization","method":"upi"}'
# -> AMBIGUOUS, stage=gated:low_conf, retry_count=0, plan=[payment_link]
```

**Decide.** `build_plan()` is a **pure function** — no I/O, no LLM, no clock
reads beyond its context argument. That purity is what makes it exhaustively
unit-testable, and testability is the entire argument for why a language model
is not permitted to make these decisions. The AI sits in the perception layer,
deliberately out of the layer that moves money.

| Cause | Intervention | Retries |
|---|---|---|
| Insufficient funds | Wait for the salary window (1st / 7th), then retry | yes |
| Issuer down | Immediate rail switch to UPI, zero delay | switch |
| PSP timeout | Verify no silent success, then 5m/1h/6h backoff | yes |
| Abandoned OTP | Payment link — the instrument was the friction | **no** |
| Card expired | Card-update link | **no** |
| Risk blocked | Human escalation | **never** |
| Ambiguous | Payment link — safe under every possible cause | **no** |

**Execute.** Durable Temporal workflows. Guards are re-evaluated immediately
before every step, never at plan time — the world moves during a 72-hour timer.
Stopping rules cover contact caps per payment and per customer per week, retry
caps, quiet hours (21:00–09:00 IST), and `order_paid_elsewhere`, the one most
systems forget. Every suppression is logged with the guard's name; the
suppression log is evidence, not a gap.

**Audit.** Hash-chained append-only ledger — each row hashes the previous row's
hash into its body. `GET /audit/verify` walks the chain and reports the sequence
number where it breaks.

---

## Tier 3 — LLM triage for unseen error codes

Rules and the GBM only know error tuples that appeared in training. Real
gateways emit a long tail: new codes after issuer integrations, bank-specific
strings, unmapped reason fields. Those land in `AMBIGUOUS` forever. Mapping an
unfamiliar code string onto a taxonomy is semantic work — the one thing a
language model does better than one-hot gradient boosting.

So there is an LLM in this system, in exactly one place, under three constraints:

1. **It is third in line.** Reached only when rules and the model have both
   abstained. It can never override a confident classical prediction.
2. **It proposes; the gate disposes.** Its output passes through the same
   hard-decline mass gate as every other stage. A confident retryable proposal
   on a row the model believes is a hard decline is rejected exactly as a bad
   rule hit would be.
3. **Every failure returns `AMBIGUOUS`** — no key, timeout, malformed JSON,
   unknown enum, low confidence. That is the current behaviour, so the safety
   floor cannot move down. The tier is monotonic.

**No API key required.** Without `LLM_API_KEY` the system runs as a two-stage
classifier with identical safety behaviour and every test still passes.

### Proving it offline

```bash
make triage-adversarial     # zero API calls
```

Eighteen ways an LLM can misbehave — hallucinated causes outside the taxonomy,
malformed JSON, prose instead of JSON, prompt-injection echoes, out-of-range
confidence, provider timeouts, and the important ones: well-formed but *unsafe*
proposals. All 18 land on `AMBIGUOUS`. Committed output: `eval/data/triage_adversarial.txt`.

### Measuring whether it helps

```bash
make triage-novel           # needs LLM_API_KEY
```

The test split's error tuples are rewritten into codes absent from training by
construction (asserted, not assumed), keeping ground truth. Same rows, same
model, same gate — the only difference is whether tier 3 is reachable. Reports
resolution rate, accuracy of resolved rows, and unsafe retries for both arms.

Note the control is not zero: `method` and `instrument_hint` still carry signal
when the code is unknown, so the classifier alone resolves a meaningful share.
The honest framing is that tier 3 recovers part of a degradation, not that it
rescues a helpless system.

Triage is cached on the observable tuple, so 1,200 rows cost roughly 16 API
calls — the experiment is free-tier friendly.

## The safety frontier

The metric that matters more than accuracy: how often a retry is fired at a true
hard decline. At the current operating point that is **8.21%**, and all of them
come from the rules stage rather than the model — hard declines leak into error
tuples that look retryable.

Tightening the gate reduces it, at a cost:

| Hard-decline mass limit | Unsafe retries | Abstain rate | Uplift |
|---|---|---|---|
| 0.25 (current) | 8.21% | 16.7% | +25.3% |
| 0.10 | 3.73% | 24.3% | +19.4% |
| 0.06 | 1.73% | 31.2% | +11.5% |

This is a business decision about what a false retry costs in issuer trust, not
a number to optimise blindly. We publish the curve rather than a flattering
point on it.

---

## Honest limitations

1. **Every number is simulation.** The customer response model
   (`eval/simulate.py`) is the single biggest assumption, so it is isolated in
   one file with every constant exposed. Disagree with them, change them,
   re-run. Distributions should be re-grounded in NPCI monthly data before any
   production claim.
2. **The classifier trains on the same generator that produces the test set**
   (split by committed hash, never trained on test). Real gateway logs would
   shift all metrics.
3. **`RISK_BLOCKED` is harder here than in reality.** We infer it from error
   codes; in production it is Razorpay's own first-party decision and never
   needs inference. Our unsafe-retry rate is therefore an upper bound.
4. **Expired cards with unknown tokens are irreducible** from codes alone —
   mitigated by plan design (low-confidence paths lead with a payment link,
   which is instrument-safe), not by classification.
5. **One loss surface, built deep.** Track 3 names several: checkout
   abandonment, failed mandates, B2B receivables, voice recovery. This covers
   failed payments end to end and instrumented. The architecture generalises —
   a receivables chaser is the same detect/diagnose/bounded-execute loop with a
   different cause enum — but that generalisation is untested.

---

## Known issues

Kept deliberately, and listed rather than hidden:

- `ledger.append()` reads the previous hash and inserts without a lock. Under
  concurrent appends the chain can fork silently. Fix is a `UNIQUE (prev_hash)`
  constraint so it fails loudly, or an advisory lock.
- The `order_settled` signal does not interrupt an in-flight timer; the workflow
  finishes sleeping before checking. Harmless (the `order_paid_elsewhere` guard
  catches it) but `workflow.wait_condition` is the correct shape.
- `assign_arm` is duplicated between `main.py` and `eval/generate.py` — drift
  risk on exactly the function that defines the holdout.
- Requires Python 3.11+ (`StrEnum`). Everything runs in the container, so this
  only bites if you invoke the eval on the host.

---

## Project structure

```
rzp-recovery/
├── setup.sh                  # one-shot build, migrate, smoke test
├── demo.sh                   # guided tour — every capability, narrated
├── Makefile
├── db/migrations/001_init.sql
├── services/app/
│   ├── main.py               # webhooks, /diagnose, /simulate, metrics, audit
│   ├── worker.py             # Temporal worker
│   ├── domain/               # THE CORE — pure, deterministic, fully tested
│   │   ├── taxonomy.py       #   canonical causes + retryability
│   │   ├── policy.py         #   build_plan(): (cause × context) -> plan
│   │   ├── guards.py         #   stopping rules, checked at EXECUTION time
│   │   └── ledger.py         #   hash-chained append-only audit log
│   ├── workflows/            # durable execution; all I/O in activities.py
│   └── adapters/
│       ├── diagnosis.py      # serving path for the classifier
│       ├── llm_triage.py     # tier 3 — proposes, never decides
│       ├── channels.py       # MockChannel (default) | live WhatsApp/SMS
│       └── razorpay_client.py
├── eval/                     # built BEFORE the engine — that is the point
│   ├── generate.py           # causal generator + adversarial noise
│   ├── simulate.py           # customer response model (the one assumption)
│   ├── classifier.py         # rules + GBM + hard-decline safety gate
│   ├── report.py             # gate/policy runs, bootstrap CIs
│   ├── step4.py              # oracle-vs-predicted, unsafe-retry audit
│   ├── triage_eval.py        # tier-3 adversarial + novel-code experiments
│   └── data/                 # committed results — read without re-running
└── tests/
    ├── test_policy.py        # the invariants that must never break
    └── test_triage.py        # tier-3 safety, offline, zero API calls
```

---

## Wiring real Razorpay test mode

1. Put test keys and the webhook secret in `.env`, set `CHANNEL_MODE=live`.
2. `ngrok http 8000`, then register `https://<id>.ngrok.io/webhooks/razorpay`
   in the dashboard for `payment.failed` and `payment.captured`.
3. Fail a test payment and watch it flow through the Temporal UI.

Real webhooks go through the classifier. `/simulate/failure` forces a specific
cause so the demo is deterministic — the only path that bypasses diagnosis, and
it is gated behind `DEMO_MODE`.


## Configuration

Everything runs with no keys at all. Each of these is optional:

| Variable | Default | Effect if unset |
|---|---|---|
| `LLM_API_KEY` | empty | Tier-3 triage disabled; two-stage classifier, identical safety |
| `LLM_PROVIDER` | `gemini` | `gemini` or `groq` |
| `LLM_MODEL` | `gemini-2.0-flash` | — |
| `LLM_TRIAGE_ENABLED` | `true` | Set `false` to disable tier 3 even with a key |
| `RAZORPAY_KEY_ID` / `_SECRET` | empty | Live rail switching stubbed; simulation unaffected |
| `RAZORPAY_WEBHOOK_SECRET` | empty | Real webhooks rejected; `/simulate/failure` unaffected |
| `CHANNEL_MODE` | `mock` | `live` sends real WhatsApp/SMS |
| `DEMO_MODE` | `true` | Enables unsigned `/simulate/failure` and 3600× time compression |
