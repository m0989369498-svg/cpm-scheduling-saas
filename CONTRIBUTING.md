# Contributing / 參與貢獻

Thanks for checking out this project! It's an open-source **demo / showcase** of a
CPM (Critical Path Method) engineering-scheduling SaaS. Contributions, issues, and
ideas are welcome.

歡迎參與!這是一個開源的 **CPM 工程排程 SaaS 體驗版**,歡迎提 issue、PR 與建議。

## Dev setup / 開發環境

The fastest path is the SQLite dev mode (no Docker / Postgres needed — works on
Windows-ARM64 too). See **README §4a**.

```bash
# Backend (SQLite dev mode)
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip ...
.venv/bin/python run_dev.py                      # http://localhost:8000/docs

# Frontend
cd frontend
npm install
VITE_API_BASE_URL=http://localhost:8000/api/v1 npm run dev   # http://localhost:5173
```

Or run the full stack with `docker compose up --build` (README §4).

## Tests / 測試 — keep CI green

```bash
cd backend && pytest          # backend unit + sqlite-mode tests
cd frontend && npm test       # vitest (jsdom) frontend tests
```

CI (GitHub Actions) runs backend tests against a real PostgreSQL (with RLS),
the Vite build + vitest, and a full `docker compose` end-to-end smoke. Please make
sure all three are green before opening a PR.

## Conventions

- Backend: FastAPI + async SQLAlchemy 2.0; keep the CPM engine in `app/core/`
  pure (no DB). Schema changes go through **Alembic** (`backend/migrations/`) and
  must keep `db/init.sql` + the ORM consistent.
- Multi-tenant: every new tenant-scoped table needs RLS (see README §9.1/§9.5).
- i18n: add new keys to **both** `TW` and `CN` (frontend `src/i18n/index.js` and,
  for anything used in PDFs/notifications, `backend/app/core/i18n.py`).
- Bilingual comments (繁中 / English) are welcome.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
