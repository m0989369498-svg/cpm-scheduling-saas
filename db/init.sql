-- =============================================================================
-- 企業級工程排程與自動化 SaaS (CPM / Critical Path)
-- db/init.sql  —  PostgreSQL 15 初始化腳本 (authoritative schema)
--
-- 本檔由 docker-entrypoint-initdb.d 在「全新資料卷」首次啟動時執行一次。
-- 內容：
--   1) 擴充套件 pgcrypto (gen_random_uuid)
--   2) public 核心業務表 (tenants / projects / tasks / task_dependencies)
--   3) erp_integration 服務管理 schema (無 RLS，由程式碼以 tenant_id 過濾)
--   4) 四張核心表的 Row Level Security (ENABLE + FORCE + 多租戶政策)
--   5) sync_event_log 索引
--   6) 種子資料 (TENT-9981 / PRJ-2026-TW-001 / T-01..T-03 + 依賴 + ERP 設定)
--
-- 多租戶隔離：應用程式以「表擁有者」連線，故 RLS 必須 FORCE，
-- 政策以 current_setting('app.current_tenant', true) 比對 tenant_id。
-- erp_integration.* 不啟用 RLS，讓跨租戶 worker 能掃描整個事件佇列。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. 擴充套件 (pgcrypto 提供 gen_random_uuid)
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- 1. PUBLIC 核心業務表 (RLS 保護)
-- =============================================================================

-- 租戶 (tenants) ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tenants (
    tenant_id   VARCHAR(50)  PRIMARY KEY,                 -- 租戶代碼
    name        VARCHAR(200),                             -- 租戶名稱
    region      VARCHAR(20)  NOT NULL DEFAULT 'TW',       -- 區域 (TW / CN)
    created_at  TIMESTAMPTZ  DEFAULT now()
);

-- 專案 (projects) ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.projects (
    project_id    VARCHAR(64)  PRIMARY KEY,               -- 專案代碼
    tenant_id     VARCHAR(50)  NOT NULL REFERENCES public.tenants(tenant_id),
    project_name  VARCHAR(255) NOT NULL,                  -- 專案名稱
    region        VARCHAR(20)  NOT NULL DEFAULT 'TW',     -- 區域 (TW / CN)
    created_at    TIMESTAMPTZ  DEFAULT now(),
    updated_at    TIMESTAMPTZ  DEFAULT now()
);

-- 任務 (tasks) — 內含 CPM 計算結果欄位 -----------------------------------------
CREATE TABLE IF NOT EXISTS public.tasks (
    id           BIGSERIAL    PRIMARY KEY,
    project_id   VARCHAR(64)  NOT NULL REFERENCES public.projects(project_id) ON DELETE CASCADE,
    tenant_id    VARCHAR(50)  NOT NULL,
    task_id      VARCHAR(100) NOT NULL,                   -- 任務代碼 (專案內唯一)
    task_name    VARCHAR(255) NOT NULL DEFAULT '',        -- 任務名稱
    duration     INT          NOT NULL DEFAULT 0 CHECK (duration >= 0),  -- 工期 (天)
    status       VARCHAR(20)  NOT NULL DEFAULT 'PENDING', -- PENDING/IN_PROGRESS/COMPLETED/DELAYED
    es           INT          DEFAULT 0,                  -- 最早開始 Earliest Start
    ef           INT          DEFAULT 0,                  -- 最早完成 Earliest Finish
    ls           INT          DEFAULT 0,                  -- 最晚開始 Latest Start
    lf           INT          DEFAULT 0,                  -- 最晚完成 Latest Finish
    float_time   INT          DEFAULT 0,                  -- 寬裕時間 / 總時差
    is_critical  BOOLEAN      DEFAULT FALSE,              -- 是否位於要徑/關鍵路徑
    created_at   TIMESTAMPTZ  DEFAULT now(),
    updated_at   TIMESTAMPTZ  DEFAULT now(),
    UNIQUE (project_id, task_id)
);

-- 任務依賴 (task_dependencies) — 前置任務關係 ----------------------------------
CREATE TABLE IF NOT EXISTS public.task_dependencies (
    id                   BIGSERIAL    PRIMARY KEY,
    project_id           VARCHAR(64)  NOT NULL,
    tenant_id            VARCHAR(50)  NOT NULL,
    task_id              VARCHAR(100) NOT NULL,           -- 後繼任務
    predecessor_task_id  VARCHAR(100) NOT NULL,           -- 前置任務
    UNIQUE (project_id, task_id, predecessor_task_id)
);

-- 常用查詢索引 -----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_projects_tenant       ON public.projects(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project         ON public.tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant          ON public.tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_deps_project          ON public.task_dependencies(project_id);
CREATE INDEX IF NOT EXISTS idx_deps_tenant           ON public.task_dependencies(tenant_id);

-- =============================================================================
-- 2. ROW LEVEL SECURITY (多租戶隔離)
--    ENABLE + FORCE：因 App 以表擁有者身分連線，FORCE 才會對擁有者生效。
--    政策以 current_setting('app.current_tenant', true) 比對。
--    get_db() 會在交易內執行 set_config('app.current_tenant', :t, true)。
-- =============================================================================

-- tenants ----------------------------------------------------------------------
ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenants FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_tenants ON public.tenants;
CREATE POLICY tenant_isolation_tenants ON public.tenants
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- projects ---------------------------------------------------------------------
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_projects ON public.projects;
CREATE POLICY tenant_isolation_projects ON public.projects
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- tasks ------------------------------------------------------------------------
ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tasks FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_tasks ON public.tasks;
CREATE POLICY tenant_isolation_tasks ON public.tasks
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- task_dependencies ------------------------------------------------------------
ALTER TABLE public.task_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_dependencies FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_task_dependencies ON public.task_dependencies;
CREATE POLICY tenant_isolation_task_dependencies ON public.task_dependencies
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- =============================================================================
-- 3. ERP_INTEGRATION schema (服務管理；無 RLS)
--    跨租戶 worker 直接掃描事件佇列，故不啟用 RLS，由程式碼以 tenant_id 過濾。
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS erp_integration;

-- 租戶 ERP 設定 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp_integration.tenant_erp_config (
    tenant_id     VARCHAR(50)  PRIMARY KEY,               -- 對應 public.tenants.tenant_id
    erp_type      VARCHAR(20)  NOT NULL,                  -- SAP / DINGXIN_TW / YONYOU_CN
    api_endpoint  VARCHAR(255),                           -- ERP API 端點 (空 => 模擬成功)
    is_active     BOOLEAN      DEFAULT TRUE
);

-- 任務 <-> ERP WBS 對應 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp_integration.task_mapping (
    mapping_id        BIGSERIAL    PRIMARY KEY,
    tenant_id         VARCHAR(50)  NOT NULL REFERENCES erp_integration.tenant_erp_config(tenant_id),
    schedule_task_id  VARCHAR(100) NOT NULL,              -- 排程任務代碼
    erp_wbs_code      VARCHAR(100) NOT NULL,              -- ERP 端 WBS 代碼
    UNIQUE (tenant_id, schedule_task_id)
);

-- 同步事件日誌 (拋轉 ERP 佇列) -------------------------------------------------
CREATE TABLE IF NOT EXISTS erp_integration.sync_event_log (
    event_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    VARCHAR(50)  NOT NULL,
    mapping_id   BIGINT       REFERENCES erp_integration.task_mapping(mapping_id),
    sync_type    VARCHAR(50)  NOT NULL,                   -- e.g. SCHEDULE_PUSH
    payload      JSONB        NOT NULL,                   -- 拋轉內容 (標準化 canonical)
    status       VARCHAR(20)  NOT NULL DEFAULT 'PENDING', -- PENDING/SUCCESS/DEAD
    retry_count  INT          DEFAULT 0,
    last_error   TEXT,                                    -- 最後一次失敗訊息
    created_at   TIMESTAMPTZ  DEFAULT now(),
    updated_at   TIMESTAMPTZ  DEFAULT now()
);

-- worker 掃描用索引：WHERE status='PENDING' AND retry_count < max
CREATE INDEX IF NOT EXISTS idx_sync_status
    ON erp_integration.sync_event_log(status, retry_count);

-- =============================================================================
-- 4. 種子資料 (SEED)
--    示範租戶 TENT-9981 (TW) / 示範專案 PRJ-2026-TW-001 / 任務 T-01..T-03。
--    依賴與 contracts/sample_payload.json 一致：
--      T-01 (基地開挖, 5d, 無前置, COMPLETED)
--      T-02 (一樓鋼筋綁紮, 3d, 前置 T-01, IN_PROGRESS)
--      T-03 (一樓混凝土澆置, 2d, 前置 T-02, PENDING)
--    ON CONFLICT DO NOTHING => 對全新資料卷安全、且可重入。
-- =============================================================================

-- 4.1 租戶 -----------------------------------------------------------------------
INSERT INTO public.tenants (tenant_id, name, region)
VALUES ('TENT-9981', '示範營造工程顧問', 'TW')
ON CONFLICT (tenant_id) DO NOTHING;

-- 4.2 專案 -----------------------------------------------------------------------
INSERT INTO public.projects (project_id, tenant_id, project_name, region)
VALUES ('PRJ-2026-TW-001', 'TENT-9981', '2026 示範建案工程排程', 'TW')
ON CONFLICT (project_id) DO NOTHING;

-- 4.3 任務 (含初始 CPM 計算結果) ------------------------------------------------
--     依 forward/backward pass 預先計算 (專案總工期 = 10 天，全部為要徑)：
--       T-01: es0 ef5  ls0 lf5  float0  critical
--       T-02: es5 ef8  ls5 lf8  float0  critical
--       T-03: es8 ef10 ls8 lf10 float0  critical
INSERT INTO public.tasks
    (project_id, tenant_id, task_id, task_name, duration, status,
     es, ef, ls, lf, float_time, is_critical)
VALUES
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-01', '基地開挖',       5, 'COMPLETED',
        0,  5,  0,  5, 0, TRUE),
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-02', '一樓鋼筋綁紮',   3, 'IN_PROGRESS',
        5,  8,  5,  8, 0, TRUE),
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-03', '一樓混凝土澆置', 2, 'PENDING',
        8, 10,  8, 10, 0, TRUE)
ON CONFLICT (project_id, task_id) DO NOTHING;

-- 4.4 任務依賴 ------------------------------------------------------------------
INSERT INTO public.task_dependencies
    (project_id, tenant_id, task_id, predecessor_task_id)
VALUES
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-02', 'T-01'),
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-03', 'T-02')
ON CONFLICT (project_id, task_id, predecessor_task_id) DO NOTHING;

-- 4.5 ERP 設定 (鼎新 DINGXIN_TW；api_endpoint 留空 => 拋轉時模擬成功) ----------
INSERT INTO erp_integration.tenant_erp_config (tenant_id, erp_type, api_endpoint, is_active)
VALUES ('TENT-9981', 'DINGXIN_TW', '', TRUE)
ON CONFLICT (tenant_id) DO NOTHING;

-- =============================================================================
-- 5. 應用程式連線角色 cpm_app (NON-SUPERUSER) — RLS 真正生效的關鍵
-- -----------------------------------------------------------------------------
-- 安全性重點 (root cause)：
--   docker-compose 的 POSTGRES_USER=cpm 在 postgres image 會被建立為「超級使用者」。
--   PostgreSQL 的超級使用者「永遠繞過 (BYPASS)」Row-Level Security —— 即使資料表
--   設了 ENABLE + FORCE ROW LEVEL SECURITY 也一樣。若 App / Worker 以 cpm 連線，
--   上面所有多租戶隔離政策都形同虛設 (跨租戶資料會外洩)。
--
--   修正方式：cpm 僅作為 bootstrap / owner，由 docker-entrypoint-initdb.d 執行本檔
--   (建表、RLS、種子資料) 並建立 cpm_app；App 與 Worker 一律以「非超級使用者」角色
--   cpm_app (LOGIN, NOSUPERUSER, NOBYPASSRLS) 連線，RLS 政策才會真正套用到它們。
--   種子資料以 cpm 身分執行 (繞過 RLS)，故本區塊放在所有資料表 / RLS / SEED 之後，
--   與 SEED 的先後順序無關。
-- =============================================================================
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cpm_app') THEN CREATE ROLE cpm_app LOGIN PASSWORD 'cpm_app_password' NOSUPERUSER NOBYPASSRLS; END IF; END $$;
GRANT USAGE ON SCHEMA public, erp_integration TO cpm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cpm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA erp_integration TO cpm_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cpm_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA erp_integration TO cpm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cpm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA erp_integration GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cpm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cpm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA erp_integration GRANT USAGE, SELECT ON SEQUENCES TO cpm_app;

-- =============================================================================
-- 完成。RLS 已啟用且 FORCE；App/Worker 以 cpm_app (非超級使用者) 連線使 RLS 生效；
-- erp_integration 由服務層管理 (無 RLS)。
-- =============================================================================
