.PHONY: run test check smoke automation backup postgres-preflight postgres-up reset-demo
run:
	uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

test:
	pytest -q

check:
	python -m py_compile app/*.py
	node --check static/app.js
	pytest -q

smoke:
	python scripts/smoke_test.py

automation:
	python scripts/run_automation.py

backup:
	python scripts/backup_sqlite.py

postgres-preflight:
	python scripts/postgres_preflight.py

postgres-up:
	docker compose -f docker-compose.postgres.yml up --build

reset-demo:
	rm -f euas.db euas.db-shm euas.db-wal
