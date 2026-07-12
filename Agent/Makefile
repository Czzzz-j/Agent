UV ?= uv

.PHONY: up test eval benchmark migrate clean sync

up:
	docker compose up --build

sync:
	$(UV) sync --frozen

test:
	$(UV) run pytest -q

eval:
	$(UV) run python -m evaluation.cli run --suite offline

benchmark:
	$(UV) run python -m evaluation.cli benchmark

migrate:
	$(UV) run alembic upgrade head

clean:
	docker compose down --remove-orphans
	-$(UV) cache clean

