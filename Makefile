# mailing-list-ai-check — developer tasks.
#
# Dev workflow is two terminals (the Vite dev server proxies /api to Flask):
#   terminal 1:  mail-ai-web                      # Flask API on :8050
#   terminal 2:  npm run dev --prefix frontend      # Vite on :5173 -> open this
# `make dev` prints this reminder. Use `make build` to produce frontend/dist,
# which `mail-ai-web` then serves directly (no Vite needed).

.PHONY: dev build test lint install-frontend

dev:
	@echo "Two-terminal dev workflow:"
	@echo "  terminal 1:  mail-ai-web                    # Flask API on http://127.0.0.1:8050"
	@echo "  terminal 2:  npm run dev --prefix frontend    # Vite dev server on http://localhost:5173"
	@echo ""
	@echo "Open http://localhost:5173 — it proxies /api to Flask, so no CORS setup is needed."

install-frontend:
	npm install --prefix frontend

build:
	npm run build --prefix frontend

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check .
