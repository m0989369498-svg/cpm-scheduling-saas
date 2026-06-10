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
    resource_demands JSONB    NULL,                       -- 每任務資源需求 e.g. {"crane":1,"manpower":15}
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

-- -----------------------------------------------------------------------------
-- 4.6 第二租戶 (中國大陸 CN)：TENT-CN-002 / PRJ-2026-CN-001 / C-01..C-02
--     供雙區域 (TW/CN) 示範與 dev sqlite 種子對齊。ERP 採用友 YONYOU_CN。
--       C-01 (土方开挖, 4d, 無前置, COMPLETED)  es0 ef4  ls0 lf4  float0 critical
--       C-02 (基础施工, 6d, 前置 C-01, IN_PROGRESS) es4 ef10 ls4 lf10 float0 critical
-- -----------------------------------------------------------------------------
INSERT INTO public.tenants (tenant_id, name, region)
VALUES ('TENT-CN-002', '示范建筑工程公司', 'CN')
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO public.projects (project_id, tenant_id, project_name, region)
VALUES ('PRJ-2026-CN-001', 'TENT-CN-002', '2026 示范建筑工程排程', 'CN')
ON CONFLICT (project_id) DO NOTHING;

INSERT INTO public.tasks
    (project_id, tenant_id, task_id, task_name, duration, status,
     es, ef, ls, lf, float_time, is_critical)
VALUES
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-01', '土方开挖', 4, 'COMPLETED',
        0,  4,  0,  4, 0, TRUE),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-02', '基础施工', 6, 'IN_PROGRESS',
        4, 10,  4, 10, 0, TRUE)
ON CONFLICT (project_id, task_id) DO NOTHING;

INSERT INTO public.task_dependencies
    (project_id, tenant_id, task_id, predecessor_task_id)
VALUES
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-02', 'C-01')
ON CONFLICT (project_id, task_id, predecessor_task_id) DO NOTHING;

INSERT INTO erp_integration.tenant_erp_config (tenant_id, erp_type, api_endpoint, is_active)
VALUES ('TENT-CN-002', 'YONYOU_CN', '', TRUE)
ON CONFLICT (tenant_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4.6b 雙塔平行工程示範 (資源衝突) — TENT-9981 / PRJ-2026-TW-PARALLEL
--      用以展示資源撫平 (resource leveling)：A/B 兩棟在 PA0 整備後「平行」施工，
--      PA1 與 PB1 同時各需吊車 (crane) 1 部，但專案吊車上限僅 1 部 => 必然衝突。
--      A 支 (PA1→PA2) 為要徑、B 支 (PB1→PB2) 較短而有正時差 (float)，故撫平
--      啟發法會把可移動的 B 支推遲、保護要徑 A 支。
--      依 forward/backward pass 預先計算 (專案總工期 = 12 天)：
--        PA0: es0  ef2  ls0  lf2  float0  critical   (場地整備)
--        PA1: es2  ef6  ls2  lf6  float0  critical   (A棟基礎)
--        PB1: es2  ef6  ls5  lf9  float3            (B棟基礎)
--        PA2: es6  ef11 ls6  lf11 float0  critical   (A棟結構)
--        PB2: es6  ef8  ls9  lf11 float3            (B棟結構)
--        PF : es11 ef12 ls11 lf12 float0  critical   (竣工驗收)
-- -----------------------------------------------------------------------------
INSERT INTO public.projects (project_id, tenant_id, project_name, region)
VALUES ('PRJ-2026-TW-PARALLEL', 'TENT-9981', '雙塔平行工程示範 (資源衝突)', 'TW')
ON CONFLICT (project_id) DO NOTHING;

INSERT INTO public.tasks
    (project_id, tenant_id, task_id, task_name, duration, status,
     es, ef, ls, lf, float_time, is_critical, resource_demands)
VALUES
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA0', '場地整備', 2, 'COMPLETED',
        0,  2,  0,  2, 0, TRUE,  '{"crane": 0, "manpower": 5}'::jsonb),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA1', 'A棟基礎', 4, 'IN_PROGRESS',
        2,  6,  2,  6, 0, TRUE,  '{"crane": 1, "manpower": 10}'::jsonb),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB1', 'B棟基礎', 4, 'PENDING',
        2,  6,  5,  9, 3, FALSE, '{"crane": 1, "manpower": 10}'::jsonb),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA2', 'A棟結構', 5, 'PENDING',
        6, 11,  6, 11, 0, TRUE,  '{"crane": 1, "manpower": 12}'::jsonb),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB2', 'B棟結構', 2, 'PENDING',
        6,  8,  9, 11, 3, FALSE, '{"crane": 1, "manpower": 8}'::jsonb),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PF',  '竣工驗收', 1, 'PENDING',
       11, 12, 11, 12, 0, TRUE,  '{"crane": 0, "manpower": 4}'::jsonb)
ON CONFLICT (project_id, task_id) DO NOTHING;

INSERT INTO public.task_dependencies
    (project_id, tenant_id, task_id, predecessor_task_id)
VALUES
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA1', 'PA0'),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB1', 'PA0'),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA2', 'PA1'),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB2', 'PB1'),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PF',  'PA2'),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PF',  'PB2')
ON CONFLICT (project_id, task_id, predecessor_task_id) DO NOTHING;

-- =============================================================================
-- 4.8 Phase 8 — 資源撫平 / 蒙地卡羅 新增表 (public schema，RLS ENABLE+FORCE)
-- -----------------------------------------------------------------------------
-- 決策：租戶排程資料 (資源上限 / 風險參數) 置於 public 並受 RLS 保護，
--       與 tasks / projects 的隔離方式一致 (而非置於 erp_integration)。
--       本區塊置於 GRANT 區塊之前，方能被「GRANT ON ALL TABLES」一併涵蓋。
-- =============================================================================

-- 4.8.1 專案資源上限 (project_resource_limits) --------------------------------
--   每專案對每種資源 (resource_type，例：crane / manpower) 的可用上限。
CREATE TABLE IF NOT EXISTS public.project_resource_limits (
    id            BIGSERIAL    PRIMARY KEY,
    project_id    VARCHAR(64)  NOT NULL REFERENCES public.projects(project_id) ON DELETE CASCADE,
    tenant_id     VARCHAR(50)  NOT NULL,
    resource_type VARCHAR(50)  NOT NULL,                  -- 資源類別 e.g. crane / manpower
    max_capacity  INT          NOT NULL CHECK (max_capacity >= 0),  -- 可用上限
    UNIQUE (project_id, resource_type)
);

-- 4.8.2 任務風險參數 (task_risk_parameters) — PERT 三點估計 -------------------
--   樂觀 / 最可能 / 悲觀工期供蒙地卡羅抽樣；criticality_index 為模擬回寫之要徑機率。
CREATE TABLE IF NOT EXISTS public.task_risk_parameters (
    id                    BIGSERIAL    PRIMARY KEY,
    project_id            VARCHAR(64)  NOT NULL REFERENCES public.projects(project_id) ON DELETE CASCADE,
    tenant_id             VARCHAR(50)  NOT NULL,
    task_id               VARCHAR(100) NOT NULL,
    optimistic_duration   INT          NOT NULL CHECK (optimistic_duration >= 0),  -- 樂觀工期 a
    most_likely_duration  INT          NOT NULL,                                    -- 最可能工期 m
    pessimistic_duration  INT          NOT NULL,                                    -- 悲觀工期 b
    criticality_index     REAL         NOT NULL DEFAULT 0.0,                        -- 要徑機率 [0,1]
    UNIQUE (project_id, task_id)
);

-- 常用查詢索引 -----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_resource_limits_project ON public.project_resource_limits(project_id);
CREATE INDEX IF NOT EXISTS idx_resource_limits_tenant  ON public.project_resource_limits(tenant_id);
CREATE INDEX IF NOT EXISTS idx_risk_params_project     ON public.task_risk_parameters(project_id);
CREATE INDEX IF NOT EXISTS idx_risk_params_tenant      ON public.task_risk_parameters(tenant_id);

-- 4.8.3 Row Level Security — 與 tasks / projects 相同的多租戶政策 -------------
ALTER TABLE public.project_resource_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_resource_limits FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_resource_limits ON public.project_resource_limits;
CREATE POLICY tenant_isolation_resource_limits ON public.project_resource_limits
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

ALTER TABLE public.task_risk_parameters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_risk_parameters FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_risk_params ON public.task_risk_parameters;
CREATE POLICY tenant_isolation_risk_params ON public.task_risk_parameters
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- 4.8.4 Phase 8 種子資料 (示範資源需求 / 上限 / 風險參數) ----------------------
--   以 cpm (owner) 身分執行 => 繞過 RLS；冪等 (ON CONFLICT / WHERE NOT EXISTS)。
--   PRJ-2026-TW-001：T-01/T-02/T-03 三任務並行時可能超出吊車 (crane) / 人力 (manpower) 上限。
--   PRJ-2026-CN-001：C-01/C-02 給予合理示範值。
-- 任務資源需求 (回填至 public.tasks.resource_demands) --------------------------
UPDATE public.tasks SET resource_demands = '{"crane": 1, "manpower": 10}'::jsonb
    WHERE project_id = 'PRJ-2026-TW-001' AND task_id = 'T-01' AND resource_demands IS NULL;
UPDATE public.tasks SET resource_demands = '{"crane": 2, "manpower": 15}'::jsonb
    WHERE project_id = 'PRJ-2026-TW-001' AND task_id = 'T-02' AND resource_demands IS NULL;
UPDATE public.tasks SET resource_demands = '{"crane": 2, "manpower": 8}'::jsonb
    WHERE project_id = 'PRJ-2026-TW-001' AND task_id = 'T-03' AND resource_demands IS NULL;
UPDATE public.tasks SET resource_demands = '{"crane": 1, "manpower": 12}'::jsonb
    WHERE project_id = 'PRJ-2026-CN-001' AND task_id = 'C-01' AND resource_demands IS NULL;
UPDATE public.tasks SET resource_demands = '{"crane": 2, "manpower": 18}'::jsonb
    WHERE project_id = 'PRJ-2026-CN-001' AND task_id = 'C-02' AND resource_demands IS NULL;

-- 專案資源上限 ----------------------------------------------------------------
INSERT INTO public.project_resource_limits (project_id, tenant_id, resource_type, max_capacity)
VALUES
    ('PRJ-2026-TW-001', 'TENT-9981',   'crane',    2),
    ('PRJ-2026-TW-001', 'TENT-9981',   'manpower', 20),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'crane',    2),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'manpower', 20),
    -- 雙塔平行示範：吊車上限刻意僅 1 部 => PA1 與 PB1 平行時必然超載 (crane=2 > 1)。
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'crane',    1),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'manpower', 20)
ON CONFLICT (project_id, resource_type) DO NOTHING;

-- 任務風險參數 (PERT 三點估計) ------------------------------------------------
INSERT INTO public.task_risk_parameters
    (project_id, tenant_id, task_id, optimistic_duration, most_likely_duration, pessimistic_duration)
VALUES
    ('PRJ-2026-TW-001', 'TENT-9981',   'T-01', 3, 5, 9),
    ('PRJ-2026-TW-001', 'TENT-9981',   'T-02', 2, 3, 7),
    ('PRJ-2026-TW-001', 'TENT-9981',   'T-03', 1, 2, 5),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-01', 2, 4, 8),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-02', 4, 6, 11),
    -- 雙塔平行示範 (PERT 三點估計：a / m / b)。
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA0', 1, 2, 4),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA1', 3, 4, 8),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB1', 2, 4, 7),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA2', 4, 5, 10),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB2', 1, 2, 5),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PF',  1, 1, 2)
ON CONFLICT (project_id, task_id) DO NOTHING;

-- =============================================================================
-- 4.7 應用登入帳號表 app_users (public schema，「不」啟用 RLS)
-- -----------------------------------------------------------------------------
-- 登入 (POST /auth/login) 在尚未建立 tenant 情境前依 username 查此表，故此表
-- 必須「不受 RLS 保護」(否則 app.current_tenant 未設定時查詢會被過濾為空)。
-- 本表「不」在此種任何資料列：帳號由應用啟動時以 passlib 雜湊冪等種入
-- (避免在 SQL 中寫死預雜湊密碼)。此表置於 GRANT 區塊之前，方能被
-- 「GRANT ON ALL TABLES IN SCHEMA public」一併涵蓋。
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.app_users (
    id             BIGSERIAL    PRIMARY KEY,
    tenant_id      VARCHAR(50)  NOT NULL REFERENCES public.tenants(tenant_id),
    username       VARCHAR(150) NOT NULL UNIQUE,           -- 登入帳號 (全域唯一)
    password_hash  VARCHAR(255) NOT NULL,                  -- passlib pbkdf2_sha256
    region         VARCHAR(20)  NOT NULL DEFAULT 'TW',     -- 區域 (TW / CN)
    is_active      BOOLEAN      DEFAULT TRUE,
    created_at     TIMESTAMPTZ  DEFAULT now()
);
-- 注意：刻意「不」對 app_users ENABLE ROW LEVEL SECURITY。

-- =============================================================================
-- 4.9 Phase 9 — 進度追蹤 / 實獲值管理 (EVM) 新增表 (public schema，RLS ENABLE+FORCE)
-- -----------------------------------------------------------------------------
-- 決策：進度 (task_progress) 與基準線 (project_baselines) 為租戶排程資料，
--       置於 public 並受 RLS 保護，與 tasks / projects 的隔離方式一致。
--       本區塊置於 GRANT 區塊之前，方能被「GRANT ON ALL TABLES」一併涵蓋。
-- =============================================================================

-- 4.9.1 任務進度 (task_progress) — 預算 / 完成度 / 實際成本 ---------------------
--   每任務一列；EVM 以 budget=PV/EV 基準、percent_complete 求 EV、actual_cost=AC。
CREATE TABLE IF NOT EXISTS public.task_progress (
    id                BIGSERIAL    PRIMARY KEY,
    project_id        VARCHAR(64)  NOT NULL REFERENCES public.projects(project_id) ON DELETE CASCADE,
    tenant_id         VARCHAR(50)  NOT NULL,
    task_id           VARCHAR(100) NOT NULL,
    budget            DOUBLE PRECISION NOT NULL DEFAULT 0,                 -- 任務預算 (BAC 組成)
    percent_complete  INT          NOT NULL DEFAULT 0 CHECK (percent_complete BETWEEN 0 AND 100),  -- 完成百分比
    actual_cost       DOUBLE PRECISION NOT NULL DEFAULT 0,                 -- 實際成本 (AC)
    actual_start_day  INT          NULL,                                   -- 實際開始 (相對第 0 天)
    actual_finish_day INT          NULL,                                   -- 實際完成 (相對第 0 天)
    updated_at        TIMESTAMPTZ  DEFAULT now(),
    UNIQUE (project_id, task_id)
);

-- 4.9.2 專案基準線 (project_baselines) — 排程 + 預算快照 (PV 基準) --------------
--   snapshot 形狀：{"project_duration":int,
--                   "tasks":[{"task_id","es","ef","duration","budget"}]}。
--   允許多條；最新 (max created_at / max id) 為作用中基準。
CREATE TABLE IF NOT EXISTS public.project_baselines (
    id          BIGSERIAL    PRIMARY KEY,
    project_id  VARCHAR(64)  NOT NULL REFERENCES public.projects(project_id) ON DELETE CASCADE,
    tenant_id   VARCHAR(50)  NOT NULL,
    name        VARCHAR(120) NOT NULL DEFAULT 'baseline',
    snapshot    JSONB        NOT NULL,                                     -- 排程 + 預算快照
    created_at  TIMESTAMPTZ  DEFAULT now()
);

-- 常用查詢索引 -----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_task_progress_project   ON public.task_progress(project_id);
CREATE INDEX IF NOT EXISTS idx_task_progress_tenant    ON public.task_progress(tenant_id);
CREATE INDEX IF NOT EXISTS idx_baselines_project       ON public.project_baselines(project_id);
CREATE INDEX IF NOT EXISTS idx_baselines_tenant        ON public.project_baselines(tenant_id);

-- 4.9.3 Row Level Security — 與 tasks / projects 相同的多租戶政策 -------------
ALTER TABLE public.task_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_progress FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_task_progress ON public.task_progress;
CREATE POLICY tenant_isolation_task_progress ON public.task_progress
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

ALTER TABLE public.project_baselines ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_baselines FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_baselines ON public.project_baselines;
CREATE POLICY tenant_isolation_baselines ON public.project_baselines
    USING       (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK  (tenant_id = current_setting('app.current_tenant', true));

-- 4.9.4 Phase 9 種子資料 (示範預算 / 進度 / 基準線) ----------------------------
--   以 cpm (owner) 身分執行 => 繞過 RLS；冪等 (ON CONFLICT / WHERE NOT EXISTS)。
--   PRJ-2026-TW-001：刻意造出「落後 + 超支」(behind + over-budget) 的示範：
--     budgets T-01=50000 / T-02=30000 / T-03=20000 (BAC=100000)；
--     progress T-01 100%/ac55000、T-02 40%/ac20000、T-03 0%/ac0。
--     於 data_date=8：SPI<1 (落後)、CPI<1 (超支) => EVM 與風險預警有意義。
--   PRJ-2026-CN-001 / PRJ-2026-TW-PARALLEL：給予合理 (健康) 示範值。

-- 任務進度 (預算 / 完成度 / 實際成本) ------------------------------------------
INSERT INTO public.task_progress
    (project_id, tenant_id, task_id, budget, percent_complete, actual_cost,
     actual_start_day, actual_finish_day)
VALUES
    -- PRJ-2026-TW-001 (落後 + 超支)
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-01', 50000, 100, 55000, 0, 6),
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-02', 30000,  40, 20000, 6, NULL),
    ('PRJ-2026-TW-001', 'TENT-9981', 'T-03', 20000,   0,     0, NULL, NULL),
    -- PRJ-2026-CN-001 (健康示範值)
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-01', 40000, 100, 38000, 0, 4),
    ('PRJ-2026-CN-001', 'TENT-CN-002', 'C-02', 60000,  50, 30000, 4, NULL),
    -- PRJ-2026-TW-PARALLEL (健康示範值)
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA0', 10000, 100, 10000, 0, 2),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA1', 40000, 100, 39000, 2, 6),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB1', 35000,  75, 26000, 2, NULL),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PA2', 50000,  20, 11000, 6, NULL),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PB2', 20000,   0,     0, NULL, NULL),
    ('PRJ-2026-TW-PARALLEL', 'TENT-9981', 'PF',  15000,   0,     0, NULL, NULL)
ON CONFLICT (project_id, task_id) DO NOTHING;

-- 專案基準線 (snapshot：依各任務 es/ef/duration 與上方預算組成) -----------------
--   僅在該專案尚無任何基準線時插入 (WHERE NOT EXISTS) => 冪等且不重複堆疊。
INSERT INTO public.project_baselines (project_id, tenant_id, name, snapshot)
SELECT 'PRJ-2026-TW-001', 'TENT-9981', 'baseline',
    '{"project_duration": 10, "tasks": [
        {"task_id": "T-01", "es": 0, "ef": 5,  "duration": 5, "budget": 50000},
        {"task_id": "T-02", "es": 5, "ef": 8,  "duration": 3, "budget": 30000},
        {"task_id": "T-03", "es": 8, "ef": 10, "duration": 2, "budget": 20000}
    ]}'::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM public.project_baselines WHERE project_id = 'PRJ-2026-TW-001'
);

INSERT INTO public.project_baselines (project_id, tenant_id, name, snapshot)
SELECT 'PRJ-2026-CN-001', 'TENT-CN-002', 'baseline',
    '{"project_duration": 10, "tasks": [
        {"task_id": "C-01", "es": 0, "ef": 4,  "duration": 4, "budget": 40000},
        {"task_id": "C-02", "es": 4, "ef": 10, "duration": 6, "budget": 60000}
    ]}'::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM public.project_baselines WHERE project_id = 'PRJ-2026-CN-001'
);

INSERT INTO public.project_baselines (project_id, tenant_id, name, snapshot)
SELECT 'PRJ-2026-TW-PARALLEL', 'TENT-9981', 'baseline',
    '{"project_duration": 12, "tasks": [
        {"task_id": "PA0", "es": 0,  "ef": 2,  "duration": 2, "budget": 10000},
        {"task_id": "PA1", "es": 2,  "ef": 6,  "duration": 4, "budget": 40000},
        {"task_id": "PB1", "es": 2,  "ef": 6,  "duration": 4, "budget": 35000},
        {"task_id": "PA2", "es": 6,  "ef": 11, "duration": 5, "budget": 50000},
        {"task_id": "PB2", "es": 6,  "ef": 8,  "duration": 2, "budget": 20000},
        {"task_id": "PF",  "es": 11, "ef": 12, "duration": 1, "budget": 15000}
    ]}'::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM public.project_baselines WHERE project_id = 'PRJ-2026-TW-PARALLEL'
);

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
