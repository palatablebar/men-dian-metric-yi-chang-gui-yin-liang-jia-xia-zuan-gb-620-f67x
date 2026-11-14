"""对账纯函数：量价三项、渠道明细、门店总量三向对平。

三条恒等式（README §10 口径）：
1. 量价恒等式：门店日 `revenue = adr × occupancy × rooms_available`
   （已售房晚 sold = occupancy × rooms_available，允许取整残差）；
2. 渠道明细 → 门店总量：`Σ_channel revenue == store.revenue`、
   `Σ_channel rooms_sold == store.rooms_sold`；
3. LMDI 量价分解闭合：`part_adr + part_occ + part_rooms + residual == Δrev`。

设计：
- 全部为纯函数、无数据库依赖、确定性输出，同输入同结果；
- 金额容差默认 1.0 元（浮点 + 渠道行分/元取整），售量容差 0（整数加和必须严格相等）；
- 任一检查不平 → status="mismatch" 并逐条列出差异，由调用方决定批次 partial / 告警，
  对账结果不静默修正数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MONEY_TOLERANCE = 1.0
DEFAULT_COUNT_TOLERANCE = 0


@dataclass
class CheckResult:
    code: str
    ok: bool
    detail: dict
    message: str = ""


def check_identity(row: dict, money_tolerance: float = DEFAULT_MONEY_TOLERANCE) -> CheckResult:
    """检查门店/渠道行 revenue = adr × occ × rooms（即 adr × sold 与 revenue 对平）。"""
    adr = float(row.get("adr") or 0.0)
    occ = float(row.get("occupancy") or 0.0)
    rooms = float(row.get("rooms_available") or 0.0)
    sold = float(row.get("rooms_sold") or 0.0)
    revenue = float(row.get("revenue") or 0.0)
    derived = adr * sold
    identity_gap = round(revenue - derived, 4)
    sold_gap = round(sold - occ * rooms, 4)
    ok = abs(identity_gap) <= money_tolerance and abs(sold_gap) <= 1.0
    return CheckResult(
        code="identity",
        ok=ok,
        detail={
            "revenue": round(revenue, 2),
            "adr_times_sold": round(derived, 2),
            "gap": identity_gap,
            "sold": int(sold),
            "occ_times_rooms": round(occ * rooms, 4),
            "sold_gap": sold_gap,
            "tolerance": money_tolerance,
        },
        message="" if ok else f"量价恒等式不平：营收 {revenue:.2f} ≠ ADR×售量 {derived:.2f}（差 {identity_gap:.2f}）",
    )


def check_channel_sum(
    store_row: dict, channel_rows: list[dict], money_tolerance: float = DEFAULT_MONEY_TOLERANCE
) -> CheckResult:
    """检查渠道明细营收/售量之和与门店总量对平。"""
    rev_sum = round(sum(float(row.get("revenue") or 0.0) for row in channel_rows), 4)
    sold_sum = int(sum(int(row.get("rooms_sold") or 0) for row in channel_rows))
    store_rev = float(store_row.get("revenue") or 0.0)
    store_sold = int(store_row.get("rooms_sold") or 0)
    rev_gap = round(store_rev - rev_sum, 4)
    sold_gap = store_sold - sold_sum
    ok = abs(rev_gap) <= money_tolerance and abs(sold_gap) <= DEFAULT_COUNT_TOLERANCE
    per_channel = {
        str(row.get("channel")): {
            "revenue": round(float(row.get("revenue") or 0.0), 2),
            "rooms_sold": int(row.get("rooms_sold") or 0),
        }
        for row in channel_rows
    }
    return CheckResult(
        code="channel_sum",
        ok=ok,
        detail={
            "store_revenue": round(store_rev, 2),
            "channel_revenue_sum": round(rev_sum, 2),
            "revenue_gap": rev_gap,
            "store_sold": store_sold,
            "channel_sold_sum": sold_sum,
            "sold_gap": sold_gap,
            "channels": per_channel,
            "tolerance": money_tolerance,
        },
        message=""
        if ok
        else f"渠道明细与门店总量不平：营收差 {rev_gap:.2f}、售量差 {sold_gap}",
    )


def check_decompose_closed(parts: dict, residual: float, total_delta: float,
                           money_tolerance: float = DEFAULT_MONEY_TOLERANCE) -> CheckResult:
    """检查 LMDI 三项 + 残差 == Δ营收。parts: {"adr":..,"occ":..,"rooms":..}。"""
    part_sum = round(sum(float(parts.get(key) or 0.0) for key in ("adr", "occ", "rooms")), 4)
    residual = float(residual or 0.0)
    total_delta = float(total_delta or 0.0)
    gap = round(total_delta - part_sum - residual, 4)
    ok = abs(gap) <= money_tolerance
    return CheckResult(
        code="decompose_closed",
        ok=ok,
        detail={
            "total_delta": round(total_delta, 2),
            "parts_sum": part_sum,
            "residual": round(residual, 4),
            "gap": gap,
            "tolerance": money_tolerance,
        },
        message="" if ok else f"量价分解不闭合：Δ营收 {total_delta:.2f} ≠ 三项 {part_sum:.2f} + 残差 {residual:.2f}",
    )


@dataclass
class ReconciliationReport:
    status: str = "balanced"
    checked_days: int = 0
    checks: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "checked_days": self.checked_days,
            "check_count": len(self.checks),
            "failure_count": len(self.failures),
            "checks": self.checks,
            "failures": self.failures,
        }


def reconcile_store_day(store_row: dict, channel_rows: list[dict],
                        money_tolerance: float = DEFAULT_MONEY_TOLERANCE) -> list[CheckResult]:
    """单门店单日：量价恒等式 + 渠道汇总两项检查。"""
    results = [check_identity(store_row, money_tolerance)]
    results.append(check_channel_sum(store_row, channel_rows, money_tolerance))
    for row in channel_rows:
        results.append(check_identity(row, money_tolerance))
    return results


def build_report(results: list[CheckResult]) -> ReconciliationReport:
    """汇总多日/多店检查结果，任一不平即 mismatch（不丢任何一条失败明细）。"""
    report = ReconciliationReport(checked_days=0)
    for result in results:
        report.checks.append({"code": result.code, "ok": result.ok, "detail": result.detail})
        if not result.ok:
            report.failures.append({"code": result.code, "message": result.message, "detail": result.detail})
    report.status = "balanced" if not report.failures else "mismatch"
    return report
