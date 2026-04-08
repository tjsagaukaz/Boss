# Boss Agent Guide

This repository is a local-first personal agent split between a Python backend and a native SwiftUI macOS app. Treat it as a long-lived system: prefer additive changes, preserve existing chat and SSE contracts, and keep runtime state backward compatible unless there is a clear migration path.

## Repo Layout

- `boss/`: Python backend runtime.
- `boss/api.py`: FastAPI surface for chat, permissions, memory, and system status.
- `boss/agents.py`: agent graph, model selection, handoffs, and tracing toggle.
- `boss/execution.py`: governed tool metadata, permission rules, pending approvals, and resume state.
- `boss/tools/`: local tools for macOS actions, memory, and research.
- `boss/memory/`: SQLite-backed knowledge store and system/project scanning.
- `boss/persistence/`: conversation history persistence.
- `BossApp/Sources/`: SwiftUI app, API client, chat state, markdown rendering, permissions UI.
- `start-server.sh`: local backend launcher for the FastAPI server.
- `docs/`: design notes and target architecture documents.

## Build And Run

### Backend

- Create or refresh the venv: `cd /Users/tj/boss && python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Run the API locally: `cd /Users/tj/boss && /Users/tj/boss/.venv/bin/python -m uvicorn boss.api:app --host 127.0.0.1 --port 8321`
- Quick launcher: `cd /Users/tj/boss && ./start-server.sh`
- Verify backend code: `cd /Users/tj/boss && python3 -m compileall boss`

### Frontend

- Build the macOS app: `cd /Users/tj/boss/BossApp && swift build`
- Run the built app: `cd /Users/tj/boss/BossApp && open .build/arm64-apple-macosx/debug/BossApp`

## Branch And Checkpoint Workflow

- Keep `main` as the known-good baseline.
- Start each focused task from `main` with `cd /Users/tj/boss && ./scripts/task_branch.sh <slug>`. The helper normalizes the slug and creates or switches to `boss/<slug>`.
- If the repository was freshly initialized, make the initial hygiene commit on `main` before creating task branches.
- Make small checkpoint commits on the task branch after each coherent step. Do not rewrite history; prefer another commit over amend/reset.
- Before a checkpoint you expect to keep, run the relevant verification commands below. Before merging back to `main`, run the full cross-stack set when the task touches both app and backend.

### Canonical Verification

- Backend syntax: `cd /Users/tj/boss && python3 -m compileall boss`
- Backend regression harness: `cd /Users/tj/boss && python3 -m unittest discover -s /Users/tj/boss/tests -p 'test_regression_harness.py'`
- Runtime health: `cd /Users/tj/boss && /Users/tj/boss/.venv/bin/python scripts/dev_doctor.py`
- Runtime smoke: `cd /Users/tj/boss && /Users/tj/boss/.venv/bin/python scripts/smoke_local.py`
- Frontend build: `cd /Users/tj/boss/BossApp && swift build`

## Coding Conventions

- Keep changes local-only. Do not add remote observability, hosted state, or cloud persistence outside the existing OpenAI API usage.
- Preserve API contracts and existing SwiftUI view-model structure unless the task explicitly requires a contract change.
- Prefer additive refactors to rewrites. When changing a persisted format, keep reads backward compatible.
- Inspect the actual installed SDK or package surface before changing provider, model, tracing, or tool-integration behavior.
- Put new runtime paths behind `boss.config.Settings` rather than hardcoding more filesystem locations.
- Use stdlib-first backend changes unless a new dependency is clearly justified.
- Keep SSE payloads stable. Add fields if needed, but avoid renaming or removing fields already used by the app.
- When touching the frontend, keep `APIClient`, `ChatViewModel`, and the existing surfaces aligned instead of introducing parallel state systems.

## Permission Model

- Tool calls are classified by execution type: `read`, `search`, `plan`, `edit`, `run`, `external`.
- `read`, `search`, and `plan` are auto-allowed.
- `edit`, `run`, and `external` require approval unless a stored rule already covers the scoped action.
- Permission rules live in `~/.boss/permissions.json`.
- Pending interrupted runs live in `~/.boss/pending_runs/` and must remain resumable across restarts.
- Permission decisions are exposed in the app and revocable through the permissions ledger.
- New tools should register clear human-readable scope labels so approval prompts and the ledger stay understandable.

## Definition Of Done

### Backend Changes

- `python3 -m compileall boss` passes from `/Users/tj/boss`.
- Any new config or file path is defined through `Settings` with sane local defaults.
- New logs, persistence, or observability remain local and do not change the chat flow.
- Endpoint changes are backward compatible or paired with the required frontend update.
- If runtime behavior changes, status or logging surfaces expose enough information to debug it locally.

### Frontend Changes

- `swift build` passes from `/Users/tj/boss/BossApp`.
- Empty, loading, and populated states are handled for any new surface.
- Existing navigation and chat behavior remain intact unless the task explicitly changes them.
- New API usage matches the backend contract that actually ships in this repo.

### Memory And Context Changes

- Existing history and knowledge data remain readable.
- Memory reads and writes are observable through local logs.
- New memory behavior documents what is injected, what is persisted, and when consolidation happens.