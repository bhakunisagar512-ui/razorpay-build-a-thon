#!/usr/bin/env bash
# =============================================================================
# rzp-recovery — restart & verify
#
#   ./restart.sh            recreate api+worker so .env changes take effect
#   ./restart.sh --build    rebuild images first (use after changing code)
#   ./restart.sh --triage   also run the tier-3 novel-code eval afterwards
#   ./restart.sh --all      --build and --triage together
#
# WHY NOT `docker compose restart`:
#   `restart` stops and starts the SAME container. Environment variables are
#   baked in at creation time, so a new LLM_API_KEY in .env is NOT picked up.
#   `up -d` detects the changed config and recreates the container, which is
#   what you actually want. This script does that.
#
# For a clean database use ./setup.sh --reset instead.
# =============================================================================
set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'
say()  { printf "${GREEN}[restart]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[restart]${NC} %s\n" "$*"; }
die()  { printf "${RED}[restart] ERROR:${NC} %s\n" "$*" >&2; exit 1; }
note() { printf "${DIM}          %s${NC}\n" "$*"; }

cd "$(dirname "$0")"

BUILD=0; TRIAGE=0
for arg in "$@"; do
    case "$arg" in
        --build)  BUILD=1 ;;
        --triage) TRIAGE=1 ;;
        --all)    BUILD=1; TRIAGE=1 ;;
        -h|--help) sed -n '3,17p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) die "unknown flag: $arg (try --help)" ;;
    esac
done

docker info >/dev/null 2>&1 || die "Docker daemon is not running."
[[ -f .env ]] || die "no .env found — run ./setup.sh first"

# ------------------------------------------------------------------ recreate --
if [[ $BUILD -eq 1 ]]; then
    say "rebuilding images and recreating containers..."
    docker compose up -d --build || die "build failed"
else
    say "recreating api + worker to pick up .env..."
    # --force-recreate guarantees a new container even if compose thinks the
    # config is unchanged (it does not hash .env values it did not interpolate).
    docker compose up -d --force-recreate --no-deps api worker || die "recreate failed"
fi

# ------------------------------------------------------------------- health --
say "waiting for api..."
tries=45
until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
    tries=$((tries - 1))
    [[ $tries -le 0 ]] && { docker compose logs --tail 40 api; die "api did not come back"; }
    sleep 2
done
say "api is up"

# ------------------------------------------------------------ tier-3 status --
say "checking tier-3 triage availability inside the container..."
STATUS=$(docker compose exec -T api python -c "
from services.app.adapters import llm_triage as T
print('AVAILABLE' if T.available() else 'DISABLED', T.PROVIDER, T.MODEL,
      'key_len=' + str(len(T.API_KEY)))
" 2>/dev/null) || warn "could not query triage status"

if [[ "$STATUS" == AVAILABLE* ]]; then
    say "tier 3 ACTIVE  ($STATUS)"
else
    warn "tier 3 DISABLED  ($STATUS)"
    note "the system runs as a two-stage classifier with identical safety behaviour"
    note "to enable: put a key in LLM_API_KEY in .env, then ./restart.sh"
fi

# ---------------------------------------------------------------- smoke test --
say "smoke test: firing a simulated CARD_EXPIRED failure..."
RESP=$(curl -sf -X POST localhost:8000/simulate/failure \
        -H 'content-type: application/json' \
        -d '{"cause":"CARD_EXPIRED","arm":"treatment"}') || die "simulate endpoint failed"
if echo "$RESP" | grep -q 'silent_retry'; then
    die "plan contains a retry on an expired card — something is very wrong"
fi
echo "$RESP" | grep -q 'card_update_link' \
    && say "engine correctly issued a card-update link (zero retries)" \
    || warn "unexpected plan shape: $RESP"

curl -sf localhost:8000/audit/verify | grep -q '"valid":true' \
    && say "audit chain verifies" \
    || warn "audit chain did not return valid=true"

# -------------------------------------------------------------- triage eval --
if [[ $TRIAGE -eq 1 ]]; then
    if [[ "$STATUS" == AVAILABLE* ]]; then
        say "running tier-3 novel-code experiment (this makes real API calls)..."
        echo
        docker compose exec -T api python -m eval.triage_eval \
            --mode novel --sample 1200 | tee eval/data/triage_novel.txt
        echo
        say "saved to eval/data/triage_novel.txt"
    else
        warn "skipping --triage: no API key, nothing to compare"
        note "the offline safety proof still runs: make triage-adversarial"
    fi
fi

echo
say "ready — http://localhost:8000/docs   ·   http://localhost:8080"
note "guided tour: ./demo.sh        clean database: ./setup.sh --reset"