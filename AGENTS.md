# Repository Guidelines

## Project Structure & Module Organization

This workspace contains Python scripts monitoring Nike China store prices, size-level availability, promotions, and new arrivals. The proposed web frontend/backend remains in requirements.

- Root `nike_*.py`: `nike_store_probe.py` queries availability/prices; `nike_auth.py` handles authentication; `nike_zero_touch.py` orchestrates monitoring.
- `requirements/`: product specifications and design reviews, not dependency manifests. Start with `00-project-context.md`.
- `wmpf/`: bundled debugger; TypeScript in `src/`, Electron in `UI/`, bridge utilities in `server/`, screenshots in `images/`.
- `unpacked/`, `WACloud.js/`, `WAServiceMainContext.js/`: extracted reference artifacts; avoid routine edits.
- Root JSON/logs/screenshots are runtime/research artifacts; `watch.txt` lists watched product codes.

## Build, Test, and Development Commands

Use Python 3.12; run these from the workspace root:

- `python -m pip install requests "httpx[http2]"`: install core monitoring dependencies.
- `python -m py_compile nike_auth.py nike_store_probe.py nike_zero_touch.py`: check syntax without API calls.
- `python nike_store_probe.py --help`: inspect query options.
- `python nike_store_probe.py --store 3752 --code IO5031-600`: query one store/product using local credentials; updates state.
- `python nike_zero_touch.py --loop 3600`: run hourly monitoring.

For debugger maintenance, run `cd wmpf`, then `npm install` (also builds), `npm run build`, or `npm run ui`. Windows and Node.js 18+ are required; builds use `xcopy`.

## Coding Style & Naming Conventions

Match existing four-space indentation. Use Python `snake_case`, uppercase constants, and descriptive names. Preserve UTF-8 Chinese messages. Follow adjacent TypeScript/JavaScript conventions; TypeScript enables strict checking. No formatter or linter is configured.

## Testing Guidelines

No automated suite or coverage threshold is configured; `wmpf/server` has a placeholder failing test command. Captured `*test*.json` files are not tests. Run syntax/help checks for changed scripts. Place future regression tests in `tests/test_<module>.py`, mocking HTTP and credentials. Cover empty responses, retries, store-specific GTINs, and state diffs.

## Commit & Pull Request Guidelines

This checkout lacks Git metadata; commit conventions cannot be verified. Use imperative subjects, e.g., `Fix empty review handling`. PRs should explain behavior changes, validation commands/results, related requirements/issues, and state-format changes. Include screenshots for UI changes.

## Configuration & Service Boundaries

Preserve the API-only service design; client/debugger automation must not become production dependencies. Prefer `cart_reviews`; legacy `nike_store_oracle.py` modifies carts. Treat failed/empty reviews as unknown. Never commit tokens, cookies, or authentication dumps. Sanitize captured responses before sharing.
