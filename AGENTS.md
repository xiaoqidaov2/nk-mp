# Repository Guidelines

## Project Structure & Module Organization

This repository hosts a multi-tenant platform that monitors Nike China store prices, size-level
availability and promotions. The Python CLI scripts at the root are historical assets; the product
lives under `apps/`.

- `apps/api`: FastAPI service, probe worker, scheduler, SQLAlchemy models, Alembic migrations and `app/cli.py` operator commands.
- `apps/web`: React + TypeScript frontend (Vite, Ant Design, React Query).
- `docs`: architecture and frontend design notes. `requirements/`: product specifications, not dependency manifests.
- Root `nike_*.py`: legacy CLI scripts. `wmpf/`, `unpacked/`: research artifacts, never part of the production path.
- Root JSON/log files are runtime artifacts and stay untracked.

## Build, Test, and Development Commands

Docker Compose is the supported local runtime (WSL + Docker); do not run bare `docker` from PowerShell.

- `cp .env.example .env` and set `NIKE_MP_CREDENTIAL_KEY`: prepare local configuration.
- `docker compose up -d --build`: start `db`, `api`, `worker`, `scheduler`, `web`.
- `docker compose exec api python -m app.cli bootstrap --email you@example.com --password '...'`: create schema and first platform admin.
- `docker compose exec api pytest` and `ruff check .`: backend tests and lint.
- `cd apps/web && npm install && npm run dev`: frontend dev server; `npm run typecheck` and `npm test` for checks.

## Coding Style & Naming Conventions

Python: 4-space indent, `snake_case` functions, `PascalCase` models, uppercase module constants, type
hints on public functions. Ruff with `line-length = 110`. TypeScript: 2-space indent, functional
components, `PascalCase` component files, `camelCase` helpers, no `any`. Keep Chinese user-facing copy
UTF-8. Import web modules through the `@/` alias.

## Testing Guidelines

Backend tests live in `apps/api/tests` (pytest); frontend tests use Vitest and Testing Library
(`*.test.tsx`). Name tests by behaviour, not implementation. Changes must cover: unknown and untrusted
responses never become stock conclusions, price may be missing, GTIN structures are store-scoped, and
tenant isolation holds. Mock all HTTP; never call Nike endpoints from tests.

## Commit & Pull Request Guidelines

Commit subjects are imperative and scoped, e.g. `feat(api): add scan progress SSE`. PRs must describe the
behaviour change, list validation commands actually run, call out schema or state-format changes, and
include screenshots for UI work. State clearly when verification was not executed.

## Configuration & Service Boundaries

- Collection is API-only and read-only. Never introduce WeChat client control, adb, emulators, Frida, OCR or screenshots into the service path; use `cart_reviews` rather than cart writes.
- Treat empty, partial or untrusted review responses as `UNKNOWN`. They must not become a trusted baseline or produce restock/sold-out events.
- Never commit credentials, tokens, cookies or captured traffic. Credentials are encrypted with `NIKE_MP_CREDENTIAL_KEY`; without that key the API refuses to read them.
- The platform cannot list a store's full catalogue. UI copy must not imply otherwise.