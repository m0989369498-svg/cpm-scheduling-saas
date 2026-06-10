# 企業級工程排程與自動化 SaaS (CPM / Critical Path)

> Enterprise engineering scheduling & automation SaaS for construction firms and engineering
> consultancies. Cross-strait (TW / CN) **multi-tenant** platform built around the
> **Critical Path Method (CPM / 要徑法 / 關鍵路徑法)**.

A drag-to-recalculate scheduling board (Gantt) backed by a pure-Python CPM engine, with
multi-tenant isolation via PostgreSQL **Row-Level Security (RLS)**, a pluggable **ERP
Anti-Corruption Layer (ACL)** (SAP / 鼎新 DINGXIN / 用友 YONYOU), PDF report generation, and
region-aware notifications (LINE for TW, DingTalk/釘釘 for CN).

---

## 1. Architecture Overview

```
                          ┌──────────────────────────────────────────────┐
   Browser (5173 / 8080)  │  React 18 + Vite + Zustand  (ScheduleBoard,    │
        │                 │  GanttChart, i18n TW/CN, axios client)         │
        │                 └──────────────────────────────────────────────┘
        ▼
┌────────────────────┐        ┌──────────────────────────────────────────────┐
│ gateway (nginx)    │  /api  │  backend (FastAPI, async SQLAlchemy 2.0)       │
│ :8080 → backend/   │───────▶│   /api/v1/...  routers: projects, tasks,       │
│        → frontend  │   /    │   schedule, erp  + CPM engine (pure fns)       │
└────────────────────┘        └───────────────┬───────────────┬──────────────┘
                                               │               │
                                  RLS session  │               │ enqueue PENDING
                          set_config(app.current_tenant)        │ sync_event_log
                                               ▼               ▼
                                  ┌────────────────────┐  ┌────────────────────┐
                                  │ PostgreSQL 15      │  │ worker (APScheduler)│
                                  │ public.* (RLS)     │◀─│ scans erp_integration│
                                  │ erp_integration.*  │  │ .sync_event_log →    │
                                  │ (no RLS, svc-mgd)  │  │ ERP ACL → push       │
                                  └────────────────────┘  └────────────────────┘
                                               ▲
                                  ┌────────────────────┐
                                  │ Redis 7 (cache/    │
                                  │ best-effort ping)  │
                                  └────────────────────┘
```

**Key ideas**

- **Multi-tenant isolation** — every core table (`tenants`, `projects`, `tasks`,
  `task_dependencies`) has RLS **enabled and FORCED**. The app connects as the table owner, so
  `FORCE` is required; each request opens a session that sets the
  `app.current_tenant` GUC (transaction-scoped) before any query runs.
- **ERP Anti-Corruption Layer** — an internal canonical model is translated per ERP
  (SAP / DINGXIN_TW / YONYOU_CN) so external ERP quirks never leak into the domain model.
- **Dual-region (TW / CN)** — `region` drives i18n (繁中 / 简中) and the notification channel
  (LINE vs DingTalk). The gateway shows how `tw.app.com` vs `cn.app.com` would route.
- **Async-everywhere** — FastAPI + SQLAlchemy 2.0 async (asyncpg) + async Redis + async httpx.

---

## 2. Tech Stack

| Layer      | Technology |
|------------|-----------|
| Backend    | Python 3.11, FastAPI, SQLAlchemy 2.0 (async / asyncpg), Pydantic v2 + pydantic-settings, redis (async), APScheduler, reportlab, httpx |
| Frontend   | React 18 + Vite, Zustand, axios (plain `.jsx`, no TypeScript) |
| Database   | PostgreSQL 15 (RLS, pgcrypto) |
| Cache/Queue| Redis 7 |
| Gateway    | nginx |
| Packaging  | Docker / docker-compose |

---

## 3. Prerequisites

- **Docker** + **Docker Compose** (v2) — for the one-command quickstart.
- For local development (optional): **Python 3.11**, **Node.js 20**, a local **PostgreSQL 15**
  and **Redis 7** (or just run those two via compose).

---

## 4. Quickstart (Docker)

```bash
# 1. Copy env defaults
cp .env.example .env

# 2. Build & start the whole stack
docker compose up --build
```

Then open:

| URL | What |
|-----|------|
| <http://localhost:8080>            | App via the **gateway** (frontend + `/api` to backend) |
| <http://localhost:8000/docs>       | Backend **OpenAPI / Swagger** docs |
| <http://localhost:8000/health>     | Backend health probe |

A demo tenant **`TENT-9981`** (region `TW`) and demo project **`PRJ-2026-TW-001`** (tasks
`T-01..T-03`) are seeded by `db/init.sql`, matching `contracts/sample_payload.json`.

The frontend defaults its tenant to `TENT-9981` / region `TW`, so the board is populated on first load.

---

## 5. Local Development (without full Docker)

Run only the infra you don't want to install locally:

```bash
docker compose up -d postgres redis
```

**Backend**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# point at the local infra (override the compose hostnames)
export DATABASE_URL="postgresql+asyncpg://cpm:cpm_password@localhost:5432/cpm_saas"
export REDIS_URL="redis://localhost:6379/0"

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Run the ERP worker separately:

```bash
cd backend
python -m app.erp.worker
```

**Frontend**

```bash
cd frontend
npm install
# talk directly to the backend (bypassing the gateway) during dev
VITE_API_BASE_URL=http://localhost:8000/api/v1 npm run dev
```

Vite dev server runs on <http://localhost:5173>.

---

## 6. Environment Variables

All variables live in `.env` (see `.env.example`). Backend field names are snake-case in
`app/config.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://cpm:cpm_password@postgres:5432/cpm_saas` | Async Postgres DSN (asyncpg). |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection (async client, best-effort cache). |
| `APP_ENV` | `development` | Environment marker. |
| `API_V1_PREFIX` | `/api/v1` | REST API prefix. |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | Comma-separated allowed origins (parsed to a list). |
| `DEFAULT_REGION` | `TW` | Fallback region when `X-Region` header is absent. |
| `LINE_CHANNEL_ACCESS_TOKEN` | _(empty)_ | LINE push token (TW notifications). Empty ⇒ no-op/log. |
| `DINGTALK_WEBHOOK_URL` | _(empty)_ | DingTalk/釘釘 webhook (CN notifications). Empty ⇒ no-op/log. |
| `ERP_SCAN_INTERVAL_SECONDS` | `300` | Worker poll interval for `sync_event_log`. |
| `ERP_MAX_RETRIES` | `5` | Max retries before a sync event is marked `DEAD`. |
| `VITE_API_BASE_URL` | `/api/v1` | **Frontend only** — axios base URL (use full backend URL in dev). |

---

## 7. REST API

Base prefix: `{API_V1_PREFIX}` (default `/api/v1`).

**Required headers on every `/api/v1/...` endpoint**

| Header | Required | Description |
|--------|----------|-------------|
| `X-Tenant-Id` | ✅ | Tenant id; drives RLS isolation. |
| `X-Region` | optional | `TW` or `CN`; defaults to `DEFAULT_REGION`. |

| Method | Path | Body | Returns | Notes |
|--------|------|------|---------|-------|
| `POST` | `/schedule/calculate` | `list[TaskDefinition]` | `list[TaskResult]` | Stateless CPM, no DB. |
| `GET` | `/projects` | — | `list[ProjectSummary]` | List tenant's projects. |
| `POST` | `/projects` | `ProjectCreate` | `ProjectOut` | Persist tasks+deps, run CPM, store results. |
| `GET` | `/projects/{project_id}` | — | `ProjectOut` | Load DAG; cached CPM results (recompute if missing). |
| `PUT` | `/projects/{project_id}` | `ProjectBase` | `ProjectOut` | Update project metadata. |
| `DELETE` | `/projects/{project_id}` | — | `{ok: true}` | Delete project (cascades tasks). |
| `GET` | `/projects/{project_id}/tasks` | — | `list[TaskResult]` | Tasks with CPM fields. |
| `POST` | `/projects/{project_id}/tasks` | `TaskCreate` | `ProjectOut` | Add task, recalc CPM. |
| `PUT` | `/projects/{project_id}/tasks/{task_id}` | `TaskUpdate` | `ProjectOut` | Update task, recalc CPM. |
| `PUT` | `/projects/{project_id}/tasks/{task_id}/duration` | `TaskDurationUpdate` | `ProjectOut` | **Drag-to-recalc**: set duration, recompute whole project. |
| `DELETE` | `/projects/{project_id}/tasks/{task_id}` | — | `ProjectOut` | Delete task + its deps, recalc. |
| `POST` | `/projects/{project_id}/erp/sync` | `ErpSyncRequest` | `{enqueued: N, event_ids: [...]}` | Enqueue `PENDING` rows (creates mappings on the fly if missing). |
| `GET` | `/projects/{project_id}/report` | — | `application/pdf` | Streaming PDF schedule report. |
| `GET` | `/health` | — | `{status: "ok"}` | **Unprefixed** health probe. |

**Canonical payload contract** — `contracts/schema.json` (JSON Schema draft-07) and
`contracts/sample_payload.json` describe the standardized project payload
(`project_id`, `tenant_id`, `region`, `schedule_data[…]`, `erp_sync_config`).

Example — calculate a schedule:

```bash
curl -X POST http://localhost:8080/api/v1/schedule/calculate \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: TENT-9981' \
  -H 'X-Region: TW' \
  -d '[{"task_id":"T-01","duration":5,"predecessors":[]},
       {"task_id":"T-02","duration":3,"predecessors":["T-01"]},
       {"task_id":"T-03","duration":2,"predecessors":["T-02"]}]'
```

---

## 8. Domain Schemas (Pydantic v2)

Defined in `backend/app/schemas/schedule.py`:

- **`TaskDefinition`** — `task_id`, `task_name`, `duration` (≥0), `predecessors[]`, `status`.
- **`TaskResult(TaskDefinition)`** — adds CPM fields `es`, `ef`, `ls`, `lf`, `float_time`, `is_critical`.
- **`ProjectCreate`** / **`ProjectOut`** / **`ProjectSummary`** — project envelopes.
- **`TaskCreate`**, **`TaskUpdate`**, **`TaskDurationUpdate`**, **`ErpSyncRequest`**.

CPM fields: **ES** (Early Start), **EF** (Early Finish), **LS** (Late Start), **LF** (Late
Finish), **float_time** (寬裕時間 / 總時差), **is_critical** (要徑 / 關鍵路徑, `float_time == 0`).

Status values across the system: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `DELAYED`.

---

## 9. Architecture Notes

### 9.1 Row-Level Security (multi-tenant)

- RLS is **enabled and FORCED** on `tenants`, `projects`, `tasks`, `task_dependencies`.
- **The app and worker connect as the non-superuser role `cpm_app`** (`LOGIN`,
  `NOSUPERUSER`, `NOBYPASSRLS`) — see [§9.5 Security / RLS role](#95-security--rls-role).
  This is what makes RLS actually isolate tenants: **superusers (and table owners) bypass
  RLS**, so connecting as a plain role is required, and `FORCE` covers the owner case too.
- Policy per table: `USING (tenant_id = current_setting('app.current_tenant', true))`
  with the same `WITH CHECK`.
- `app/database.py` `get_db` runs, inside the transaction and **before yielding**:

  ```python
  await session.execute(
      text("SELECT set_config('app.current_tenant', :t, true)"),
      {"t": tenant_id},
  )
  ```

  `is_local = true` scopes the GUC to the current transaction, which is safe with pooled
  connections. The session commits on success and rolls back on exception.

### 9.2 ERP Anti-Corruption Layer

- `erp/acl.py` defines a **canonical sync item** (`task_id`, `wbs_code`, `duration`, `status`,
  `dates`) and a base `ErpAdapter` with `translate(canonical) -> dict` and `push(payload)`
  (async httpx; empty endpoint ⇒ simulated success — no credentials needed in dev).
- `erp/adapters.py` provides `SapAdapter`, `DingxinAdapter` (鼎新), `YonyouAdapter` (用友),
  each with distinct field mappings (SAP WBS/NETWORK fields vs. the Chinese ERPs' own codes),
  plus `get_adapter(erp_type)` factory (`SAP`, `DINGXIN_TW`, `YONYOU_CN`; default simulate).
- The `erp_integration` schema has **no RLS** (service-managed; code filters by `tenant_id`).
  This deliberately lets the cross-tenant worker scan `sync_event_log` across all tenants.

### 9.3 Dual-Region (TW / CN)

- `region` (`TW` / `CN`) flows from the `X-Region` header through to:
  - **i18n** — backend `core/i18n.py` and frontend `src/i18n/index.js` share the **same keys**
    (繁中 for `TW`, 简中 for `CN`).
  - **Notifications** — region `CN` ⇒ DingTalk/釘釘; otherwise LINE.
- The gateway (`gateway/nginx.conf`) documents how `server_name tw.app.com` vs `cn.app.com`
  would route per region.

### 9.4 ERP Sync Worker

- Runs standalone: `python -m app.erp.worker` (its own service in compose).
- APScheduler `AsyncIOScheduler` fires every `ERP_SCAN_INTERVAL_SECONDS`.
- Each tick (`scan_once()`): open its **own** AsyncSession (does **not** set
  `app.current_tenant` — it only touches the no-RLS `erp_integration.*`), select
  `sync_event_log` rows with `status='PENDING' AND retry_count < ERP_MAX_RETRIES`, look up the
  tenant's `tenant_erp_config`, pick the adapter, translate + push.
  - **Success** ⇒ `status='SUCCESS'`.
  - **Failure** ⇒ increment `retry_count`, set `last_error`; stays `PENDING`, or becomes
    `DEAD` once `retry_count >= ERP_MAX_RETRIES`.
- `scan_once()` is idempotent and used by tests / manual runs.

### 9.5 Security / RLS role

Row-Level Security only isolates tenants if the connecting role is actually subject to it.
**PostgreSQL superusers (and a table's owner) bypass RLS** — even with
`ENABLE` + `FORCE ROW LEVEL SECURITY` — so the role used by the application matters as much
as the policies themselves.

- The Postgres image's `POSTGRES_USER=cpm` is created as a **superuser**. `cpm` is used
  **only** as the bootstrap / owner: `docker-entrypoint-initdb.d` runs `db/init.sql` as
  `cpm` to create the schema, RLS policies, seed data, and the application role.
- The **app and worker connect as `cpm_app`** — a dedicated role created by `init.sql` with
  `LOGIN NOSUPERUSER NOBYPASSRLS`. Because it is neither a superuser nor a table owner, the
  per-tenant policies are enforced for every query it runs. This is the fix for the original
  bug where connecting as `cpm` silently disabled tenant isolation.
- `cpm_app` is granted `SELECT/INSERT/UPDATE/DELETE` on `public.*` and `erp_integration.*`
  (plus sequence usage and matching `ALTER DEFAULT PRIVILEGES`), so it can do everything the
  app needs while remaining inside RLS. The `erp_integration.*` tables intentionally have no
  RLS, which is how the cross-tenant worker scans `sync_event_log`.
- Role / credential contract: DB `cpm_saas`; bootstrap owner `cpm` / `cpm_password`;
  app role `cpm_app` / `cpm_app_password`. App + worker DSN:
  `postgresql+asyncpg://cpm_app:cpm_app_password@postgres:5432/cpm_saas`
  (host `postgres` in compose, `localhost` in CI / local dev).

---

## 10. Running Tests

```bash
cd backend
pip install -r requirements.txt
pytest
```

- `tests/test_cpm_engine.py` — forward/backward pass, project duration, critical path, and
  error cases (cycle detection, unknown predecessor, empty input).
- `tests/test_api.py` — FastAPI endpoint smoke/integration tests.

### 10.1 CI (GitHub Actions)

A GitHub Actions pipeline runs on every push / pull request (on `ubuntu-latest`, Python 3.11
and Node 20 — Linux has prebuilt wheels for `asyncpg` / `httptools` / `reportlab`). It has
three parts:

- **Backend tests** — spins up **PostgreSQL** and **Redis** as service containers (the DB is
  initialized from `db/init.sql`, so the app connects as `cpm_app` and RLS is exercised for
  real), then runs `pytest` including the DB-backed integration / RLS-isolation tests. In CI
  the DSN host is `localhost` (`postgresql+asyncpg://cpm_app:cpm_app_password@localhost:5432/cpm_saas`).
- **Frontend build** — `npm ci` + `npm run build` to verify the Vite production build.
- **docker-compose e2e** — `docker compose up --build` brings up the full stack and smoke-tests
  it (e.g. the `/health` probe and an `/api/v1` request through the gateway).

---

## 11. Project Layout

```
backend/      FastAPI app, CPM engine, ORM, ERP ACL/worker, automation, tests
db/           init.sql (schema, RLS policies, seed data)
frontend/     React + Vite SPA (Gantt board, i18n, zustand store)
gateway/      nginx reverse proxy (/api → backend, / → frontend)
contracts/    schema.json (JSON Schema) + sample_payload.json
docker-compose.yml, .env.example, .gitignore, README.md
```

---

## 12. License & Notes

Internal/demo project. External ERP and notification integrations **mock the network when
credentials are absent** (empty token/endpoint ⇒ log + simulated success), so the full stack
runs end-to-end out of the box.
