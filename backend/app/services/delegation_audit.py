"""委托审计领域服务：记录探针派发的实际字段、派发结果与时序。

探针选择每次消费许可生成快照请求时回写一条审计记录；撤销与在途回执
冲突时，按派发/撤权/回执的事件序号还原实际先后，并记录结果何时可用：
- 回执先于撤权：结果即时可用；
- 派发先于撤权但回执晚于撤权：结果冻结，由维护者确认后可用或作废。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DelegationAuditService(BaseModuleService):
    MODULE = "delegation_audit"
    ENTITY = "审计记录"
    OPERATION_TERM = "观测"
    LIST_FIELDS = [
        "委托编号", "操作者", "实际字段", "模板版本", "派发时间",
        "撤权时间", "回执时间", "时序结论", "派发结果", "结果可用性",
    ]
    REQUIRED_FIELDS = ["委托编号", "操作者", "实际字段"]
    STATUS_ORDER = ["已派发", "已回执", "已冻结", "已关闭"]
    ACTION_RULES = {"确认回执": "已冻结", "作废回执": "已冻结"}
    NEGATIVE_ACTIONS = ["作废回执"]

    # ------------------------------------------------------------------
    # 回写：探针派发时登记审计记录
    # ------------------------------------------------------------------
    def record_dispatch(
        self,
        *,
        delegation_code: str,
        delegation_id: int | None,
        actor: str,
        fields: list[str],
        template_version: str,
        probe_id: int,
        snapshot_request: dict[str, Any],
    ) -> dict[str, Any]:
        """探针选择消费许可后回写：实际使用字段、派发结果与派发序号。"""
        seq, ts = store.next_event()
        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({
            "委托编号": delegation_code,
            "操作者": actor,
            "实际字段": ",".join(fields),
            "模板版本": template_version,
            "派发时间": ts,
            "撤权时间": "",
            "回执时间": "",
            "时序结论": "派发已登记，回执未达",
            "派发结果": "已派发",
            "结果可用性": "待回执",
            "status": "已派发",
            "pending": True,
            "abnormal": False,
            "派发序号": seq,
            "委托ID": delegation_id,
            "探针ID": probe_id,
            "快照请求": snapshot_request,
        })
        rows.append(entry)
        return entry

    # ------------------------------------------------------------------
    # 回执：按撤权是否已到达决定结果可用性
    # ------------------------------------------------------------------
    def complete_receipt(self, audit_id: int) -> tuple[dict[str, Any] | None, str]:
        from app.services import service_for

        audit = store.find(self.MODULE, audit_id)
        if audit is None:
            return None, f"审计记录 {audit_id} 不存在"
        if audit.get("status") != "已派发":
            return None, f"审计记录 {audit_id} 当前为{audit.get('status')}，不能重复回执"

        seq, ts = store.next_event()
        audit["回执时间"] = ts
        audit["回执序号"] = seq

        delegation_id = audit.get("委托ID")
        delegation = store.find("probe_delegation", int(delegation_id)) if delegation_id else None
        # 回执发生在“现在”，委托若已撤销/已关闭，则撤权必然先于本次回执到达。
        revoked_before_receipt = (
            delegation is not None
            and delegation.get("status") in ("已撤销", "已关闭")
        )

        if revoked_before_receipt:
            # 撤权先于回执到达：结果冻结，不改变维护者已收到的数据，
            # 等待维护者确认可用或作废；审计里保留实际先后。
            audit["撤权时间"] = delegation.get("撤权时间", audit.get("撤权时间", ""))
            audit["撤权序号"] = delegation.get("撤权序号", audit.get("撤权序号"))
            audit["status"] = "已冻结"
            audit["时序结论"] = "派发先于撤权，回执晚于撤权"
            audit["派发结果"] = "已派发-撤权在途"
            audit["结果可用性"] = "冻结待确认"
            return audit, f"回执到达时委托 {audit.get('委托编号')} 已撤销，结果冻结待维护者确认"

        self._publish_snapshot(audit)
        audit["status"] = "已回执"
        audit["pending"] = False
        audit["时序结论"] = "回执先于撤权" if delegation is not None else "维护者直采，回执生效"
        audit["派发结果"] = "已派发-回执完成"
        audit["结果可用性"] = "即时可用"
        audit["结果可用时间"] = ts
        return audit, f"回执完成，快照已发布，结果{audit['结果可用性']}"

    def _publish_snapshot(self, audit: dict[str, Any]) -> dict[str, Any] | None:
        from app.services import service_for

        snapshot_service = service_for("page_snapshot")
        request = audit.get("快照请求") or {}
        entry, missing = snapshot_service.create_entry({
            "快照ID": request.get("快照ID", ""),
            "页面URI": request.get("页面URI", ""),
            "DOM哈希": request.get("DOM哈希", ""),
            "文本哈希": request.get("文本哈希", ""),
            "抓取时间": request.get("抓取时间", ""),
            "页面大小": request.get("页面大小", ""),
            "是否变更": "否",
        })
        if entry is not None:
            entry["来源委托"] = audit.get("委托编号")
            entry["来源审计"] = audit.get("id")
            audit["快照记录ID"] = entry.get("id")
        return entry

    # ------------------------------------------------------------------
    # 冻结回执的维护者处置：确认可用 / 作废
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str, actor: str | None = None) -> tuple[dict[str, Any] | None, str]:
        if action in ("确认回执", "作废回执"):
            return self._settle_frozen(entry_id, action, actor)
        return super().run_action(entry_id, action, actor=actor)

    def _settle_frozen(self, entry_id: int, action: str, actor: str | None) -> tuple[dict[str, Any] | None, str]:
        audit = store.find(self.MODULE, entry_id)
        if audit is None:
            return None, f"审计记录 {entry_id} 不存在"
        if audit.get("status") != "已冻结":
            return None, f"审计记录 {entry_id} 当前为{audit.get('status')}，只有冻结回执需要处置"
        delegation_id = audit.get("委托ID")
        delegation = store.find("probe_delegation", int(delegation_id)) if delegation_id else None
        if delegation is not None and actor != delegation.get("维护者"):
            return None, "只有授予该委托的维护者可以处置冻结回执"

        seq, ts = store.next_event()
        if action == "确认回执":
            self._publish_snapshot(audit)
            audit["status"] = "已回执"
            audit["pending"] = False
            audit["abnormal"] = False
            audit["结果可用性"] = "撤权后确认可用"
            audit["结果可用时间"] = ts
            audit["处置序号"] = seq
            return audit, f"冻结回执已确认，快照发布，结果{audit['结果可用性']}"

        audit["status"] = "已关闭"
        audit["pending"] = False
        audit["abnormal"] = True
        audit["结果可用性"] = "已作废"
        audit["处置序号"] = seq
        return audit, "冻结回执已作废，快照不发布"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["receipt_state"] = {
            "in_flight": sum(1 for row in rows if row.get("status") == "已派发"),
            "frozen": sum(1 for row in rows if row.get("status") == "已冻结"),
            "settled": sum(1 for row in rows if row.get("status") == "已回执"),
        }
        return base
