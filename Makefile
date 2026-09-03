.PHONY: up down reset logs migrate test seed eval eval-policy step4 demo fire-expired fire-issuer verify-audit

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

eval:
	docker compose exec -T api python -m eval.report --n 40000 --seed 42 --mode gate

eval-policy:
	docker compose exec -T api python -m eval.report --n 40000 --seed 42 --mode policy

step4:
	docker compose exec -T api python -m eval.step4

# demo helpers — fire failures at the live engine
fire-expired:
	curl -s -X POST localhost:8000/simulate/failure -H 'content-type: application/json' \
	  -d '{"cause":"CARD_EXPIRED"}' | python3 -m json.tool

fire-issuer:
	curl -s -X POST localhost:8000/simulate/failure -H 'content-type: application/json' \
	  -d '{"cause":"TD_ISSUER_DOWN"}' | python3 -m json.tool

verify-audit:
	curl -s localhost:8000/audit/verify | python3 -m json.tool

demo: up
	@echo ""
	@echo "  API docs  -> http://localhost:8000/docs"
	@echo "  Temporal  -> http://localhost:8080"
	@echo "  Try:  make fire-expired   (watch it refuse to retry)"
	@echo "        make fire-issuer    (watch the rail switch)"
	@echo "        make verify-audit"

triage-adversarial:
	docker compose exec -T api python -m eval.triage_eval --mode adversarial

triage-novel:
	docker compose exec -T api python -m eval.triage_eval --mode novel --sample 1200
