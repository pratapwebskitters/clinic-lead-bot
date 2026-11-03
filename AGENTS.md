# Project: Clinic Lead Bot

## Stack
Python 3.11, FastAPI, Pydantic v2, SQLite (sqlite3), httpx, pytest.

## Rules
- Read SPEC.md before any task.
- Work in small steps. Only do what the current prompt asks.
- Every new module needs pytest tests. Run `pytest` before finishing.
- Never hardcode secrets. Use environment variables via app/config.py.
- Never send real emails or Telegram messages in tests. Use the fixtures in tests/conftest.py.
- Save every lead to the DB BEFORE calling the LLM. Never lose a lead.
- Safety checks are code (app/services/safety.py), not prompt-only.
- Services are called as module attributes (e.g. `notify.send_telegram(...)`) so tests can monkeypatch them.
- Keep functions small and typed. No unused dependencies.
- After finishing, summarize what changed and what to test manually.

## Commands
- Run server: `uvicorn app.main:app --reload`
- Run tests: `pytest`
- Real-LLM quality test: `RUN_LLM_TESTS=1 pytest tests/test_quality.py -s`
