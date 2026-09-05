.PHONY: up down reset logs migrate test seed \
        eval eval-gate eval-policy eval-strong step4 demo \
        fire-expired fire-issuer fire-expired-holdout verify-audit \
        diagnose-expired diagnose-ambiguous \
        triage-adversarial triage-novel

up:
	docker compose up -d --build
	sleep 5 && $(MAKE) migrate

down:
	docker compose down

reset:
	docker compose down -v && $(MAKE) up

logs:
	docker compose logs -f api worker

migrate:
	docker compose exec -T api python -m services.app.migrate

test:
	docker compose exec -T api pytest -q

seed:
	docker compose exec -T api python -m eval.report --n 5000 --seed 42 --mode gate

# --- evaluation ---
eval-gate:
	docker compose exec -T api python -m eval.report --n 40000 --seed 42 --mode gate

eval: eval-gate

eval-policy:
	docker compose exec -T api python -m eval.report --n 40000 --seed 42 --mode policy

eval-strong:
	docker compose exec -T api python -m eval.report --n 40000 --seed 42 --mode policy --baseline strong

step4:
	docker compose exec -T api python -m eval.step4

# --- demo helpers: fire failures at the live engine ---
# arm is forced so the demo is deterministic; without it assign_arm()
# hashes the payment id and ~1 in 10 runs lands in the holdout.
fire-expired:
	curl -s -X POST localhost:8000/simulate/failure -H 'content-type: application/json' \
	  -d '{"cause":"CARD_EXPIRED","arm":"treatment"}' | python3 -m json.tool

fire-issuer:
	curl -s -X POST localhost:8000/simulate/failure -H 'content-type: application/json' \
	  -d '{"cause":"TD_ISSUER_DOWN","arm":"treatment"}' | python3 -m json.tool

fire-expired-holdout:
	curl -s -X POST localhost:8000/simulate/failure -H 'content-type: application/json' \
	  -d '{"cause":"CARD_EXPIRED","arm":"holdout"}' | python3 -m json.tool

verify-audit:
	curl -s localhost:8000/audit/verify | python3 -m json.tool

# --- diagnosis only, no side effects ---
diagnose-expired:
	curl -s localhost:8000/diagnose -H 'content-type: application/json' \
	  -d '{"gw_error_code":"BAD_REQUEST_ERROR","gw_error_source":"customer","gw_error_step":"payment","method":"card"}' \
	  | python3 -m json.tool

diagnose-ambiguous:
	curl -s localhost:8000/diagnose -H 'content-type: application/json' \
	  -d '{"gw_error_code":"GATEWAY_ERROR","gw_error_source":"gateway","gw_error_step":"authorization","method":"upi"}' \
	  | python3 -m json.tool

# --- tier 3 ---
triage-adversarial:
	docker compose exec -T api python -m eval.triage_eval --mode adversarial

triage-novel:
	docker compose exec -T api python -m eval.triage_eval --mode novel --sample 1200

demo: up
	@echo ""
	@echo "  API docs  -> http://localhost:8000/docs"
	@echo "  Temporal  -> http://localhost:8080"
	@echo "  Try:  make fire-expired            (watch it refuse to retry)"
	@echo "        make fire-expired-holdout    (same dead card, baseline arm)"
	@echo "        make fire-issuer             (watch the rail switch)"
	@echo "        make verify-audit"