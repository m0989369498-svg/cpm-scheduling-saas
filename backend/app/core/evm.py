"""實獲值管理引擎（Earned Value Management, EVM）。

純函式模組：不接觸資料庫、不依賴任何外部狀態，方便單元測試與重複呼叫。
依 PMI（Project Management Institute）標準公式，以「基準線任務（baseline
tasks）」與「實際進度（progress）」於某一資料截止日（data date）計算
PV / EV / AC 及衍生績效指標，並產出計畫價值（PV）累積 S 曲線與各任務拆解。

公開 API：
    compute_evm(baseline_tasks, progress, data_date) -> EvmResult

輸入：
    baseline_tasks  list[dict]，每筆含 {task_id, es, duration, budget}。
    progress        dict[task_id -> {percent_complete:int, actual_cost:float}]；
                    缺漏的任務視為 0% / AC 0。
    data_date       資料截止日（status date，0-based 整數）。

PMI 公式：
    BAC = sum(budget)
    每任務計畫完成比例 pf：
        duration <= 0 且 data_date < es -> pf = 0（尚未到開始日的零工期任務）
        duration <= 0 且 data_date >= es -> pf = 1（已到開始日的里程碑視為完成）
        否則 pf = clamp((data_date - es) / duration, 0, 1)
    PV  = sum(budget * pf)
    EV  = sum(budget * percent_complete / 100)
    AC  = sum(actual_cost)
    SV  = EV - PV ; CV = EV - AC
    SPI = EV / PV（PV > 0，否則 None）；CPI = EV / AC（AC > 0，否則 None）
    EAC = BAC / CPI（CPI 存在且 > 0，否則 None）
    ETC = EAC - AC（EAC 不為 None，否則 None）
    VAC = BAC - EAC（EAC 不為 None，否則 None）
    TCPI = (BAC - EV) / (BAC - AC)（分母 BAC - AC != 0，否則 None）
    pv_curve = 對 day 0..project_duration 累積 PV
    risk_flagged = (SPI 不為 None 且 SPI < 0.9) 或 (CPI 不為 None 且 CPI < 0.9)
"""

from __future__ import annotations

from typing import Any, Mapping

from app.schemas.evm import EvmResult, EvmTaskBreakdown, PvCurvePoint

# SPI / CPI 低於此門檻即視為進度落後 / 成本超支，觸發風險旗標。
RISK_THRESHOLD = 0.9


def _planned_fraction(es: int, duration: int, data_date: int) -> float:
    """計算單一任務在 data_date 的計畫完成比例 pf（0..1）。

    零（或負）工期任務（里程碑）：未到 es 為 0，到達 es 後視為 1。
    一般任務：以線性消耗模型 (data_date - es) / duration 並夾擠至 [0, 1]。
    """
    if duration <= 0:
        return 0.0 if data_date < es else 1.0
    fraction = (data_date - es) / duration
    if fraction < 0.0:
        return 0.0
    if fraction > 1.0:
        return 1.0
    return fraction


def _clamp01(value: float) -> float:
    """將數值夾擠至 [0, 1] 區間。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def compute_evm(
    baseline_tasks: list[dict[str, Any]],
    progress: Mapping[str, Mapping[str, Any]],
    data_date: int,
) -> EvmResult:
    """依 PMI 標準計算實獲值管理（EVM）結果。純函式，無副作用。

    參數：
        baseline_tasks  基準線任務清單，每筆需含 task_id / es / duration / budget。
        progress        以 task_id 為 key 的進度對照表，值含 percent_complete /
                        actual_cost；缺漏任務以 0% / AC 0 計。
        data_date       資料截止日（status date）。

    回傳：
        EvmResult —— 含 BAC/PV/EV/AC、SV/CV、SPI/CPI、EAC/ETC/VAC/TCPI、
        風險旗標、PV 累積 S 曲線與各任務拆解。
    """
    progress = progress or {}

    bac = 0.0
    pv = 0.0
    ev = 0.0
    ac = 0.0
    per_task: list[EvmTaskBreakdown] = []

    # 專案總工期（= 各任務 ef = es + duration 的最大值），供 PV 曲線範圍使用。
    project_duration = 0

    for task in baseline_tasks:
        task_id = task["task_id"]
        es = int(task.get("es", 0) or 0)
        duration = int(task.get("duration", 0) or 0)
        budget = float(task.get("budget", 0.0) or 0.0)

        ef = es + (duration if duration > 0 else 0)
        if ef > project_duration:
            project_duration = ef

        entry = progress.get(task_id) or {}
        pct_raw = entry.get("percent_complete", 0)
        percent_complete = int(pct_raw) if pct_raw is not None else 0
        actual_cost = float(entry.get("actual_cost", 0.0) or 0.0)

        pf = _planned_fraction(es, duration, data_date)
        task_pv = budget * pf
        task_ev = budget * percent_complete / 100.0

        bac += budget
        pv += task_pv
        ev += task_ev
        ac += actual_cost

        per_task.append(
            EvmTaskBreakdown(
                task_id=task_id,
                budget=budget,
                planned_pct=round(pf * 100),
                percent_complete=percent_complete,
                pv=task_pv,
                ev=task_ev,
                ac=actual_cost,
            )
        )

    # ---- 差異與績效指標（PMI 標準；安全處理除以零 / None）----
    sv = ev - pv
    cv = ev - ac

    spi = ev / pv if pv > 0 else None
    cpi = ev / ac if ac > 0 else None

    eac = bac / cpi if (cpi is not None and cpi > 0) else None
    etc = (eac - ac) if eac is not None else None
    vac = (bac - eac) if eac is not None else None

    denom = bac - ac
    tcpi = (bac - ev) / denom if denom != 0 else None

    risk_flagged = (spi is not None and spi < RISK_THRESHOLD) or (
        cpi is not None and cpi < RISK_THRESHOLD
    )

    # ---- PV 累積 S 曲線：day 0..project_duration（含端點）----
    pv_curve: list[PvCurvePoint] = []
    for day in range(0, project_duration + 1):
        cumulative = 0.0
        for task in baseline_tasks:
            es = int(task.get("es", 0) or 0)
            duration = int(task.get("duration", 0) or 0)
            budget = float(task.get("budget", 0.0) or 0.0)
            if duration <= 0:
                fraction = 0.0 if day < es else 1.0
            else:
                fraction = _clamp01((day - es) / duration)
            cumulative += budget * fraction
        pv_curve.append(PvCurvePoint(day=day, pv=cumulative))

    return EvmResult(
        data_date=data_date,
        bac=bac,
        pv=pv,
        ev=ev,
        ac=ac,
        sv=sv,
        cv=cv,
        spi=spi,
        cpi=cpi,
        eac=eac,
        etc=etc,
        vac=vac,
        tcpi=tcpi,
        risk_flagged=risk_flagged,
        pv_curve=pv_curve,
        per_task=per_task,
    )
