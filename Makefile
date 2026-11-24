.PHONY: sync lint typecheck test hud-build hud-dev daemon doctor

sync:
	cd backend && uv sync --extra dev
	cd shared/ts && pnpm install
	cd hud && pnpm install

lint:
	cd backend && uv run ruff check src tests

typecheck:
	cd backend && uv run mypy src
	cd shared/ts && pnpm run typecheck
	cd hud && pnpm run typecheck

test:
	cd backend && uv run pytest -v

hud-build:
	cd hud && pnpm run build

hud-dev:
	cd hud && pnpm tauri dev

daemon:
	cd backend && uv run sirius --download-models

doctor:
	cd backend && uv run sirius --doctor
