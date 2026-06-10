"""API 路由套件（FastAPI routers）。

匯出各領域路由器：
  - schedule_router  無狀態 CPM 計算
  - projects_router  專案 CRUD + CPM 持久化
  - tasks_router     任務 CRUD（含拖曳改工期重算）
  - erp_router       ERP 拋轉（寫入 sync_event_log）
"""

from app.routers.schedule import router as schedule_router
from app.routers.projects import router as projects_router
from app.routers.tasks import router as tasks_router
from app.routers.erp import router as erp_router

__all__ = [
    "schedule_router",
    "projects_router",
    "tasks_router",
    "erp_router",
]
