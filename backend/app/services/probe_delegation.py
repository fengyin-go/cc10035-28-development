"""运行委托领域服务：维护者授予协作者按字段运行探针的临时许可。

委托记录构成受控探针配置数据集的许可层：协作者只读该数据集获得运行权，
运行权不包含修改模板；委托在授予时钉住授权字段与模板有效版本，后续模板
并发更新不影响既有许可，保证原有许可与审计回执一致。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.template_rule import split_list
from app.store import store


class ProbeDelegationService(BaseModuleService):
    MODULE = "probe_delegation"
    ENTITY = "运行委托"
    OPERATION_TERM = "观测"
    LIST_FIELDS = ["委托编号", "模板ID", "维护者", "协作者", "授权字段", "有效版本", "到期时间"]
    REQUIRED_FIELDS = ["模板ID", "维护者", "协作者", "授权字段"]
    STATUS_ORDER = ["生效中", "已撤销", "已关闭"]
    ACTION_RULES = {"撤销委托": "生效中", "关闭授权": "已撤销"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 授予：校验发布模板的选择器范围后登记委托
    # ------------------------------------------------------------------
    def grant(
        self,
        *,
        template_id: int,
        maintainer: str,
        collaborator: str,
        fields: list[str],
        expires: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        from app.services import service_for  # 延迟导入，避免模块间循环依赖

        if not maintainer.strip() or not collaborator.strip():
            return None, "维护者与协作者不能为空"
        if maintainer == collaborator:
            return None, "维护者与协作者不能是同一人"
        template_service = service_for("template_rule")
        ok, reason, detail = template_service.validate_grant(template_id, fields)
        if not ok:
            return None, reason

        entry, _ = self.create_entry({
            "模板ID": str(template_id),
            "维护者": maintainer,
            "协作者": collaborator,
            "授权字段": ",".join(fields),
            "有效版本": str(detail["version"]),
            "到期时间": expires or "",
        })
        entry["委托编号"] = f"dlg-{int(entry['id']):03d}"
        return entry, f"运行委托 {entry['委托编号']} 已授予 {collaborator}"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """通用登记入口同样走授予校验，保证委托记录合法。"""
        from app.services import service_for

        missing = [field for field in self.REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        template_service = service_for("template_rule")
        try:
            template_id = int(str(values.get("模板ID")).strip())
        except ValueError:
            return None, ["模板ID必须是数字"]
        fields = split_list(values.get("授权字段"))
        ok, reason, detail = template_service.validate_grant(template_id, fields)
        if not ok:
            return None, [reason]
        normalized = dict(values)
        normalized["模板ID"] = str(template_id)
        normalized["授权字段"] = ",".join(fields)
        normalized["有效版本"] = str(detail["version"])
        entry, _ = super().create_entry(normalized)
        entry["委托编号"] = f"dlg-{int(entry['id']):03d}"
        return entry, []

    # ------------------------------------------------------------------
    # 撤销：记录撤权时间，并把在途回执标记为与撤权冲突
    # ------------------------------------------------------------------
    def revoke(self, delegation_id: int, actor: str | None) -> tuple[dict[str, Any] | None, str]:
        delegation = store.find(self.MODULE, delegation_id)
        if delegation is None:
            return None, f"运行委托 {delegation_id} 不存在"
        if delegation.get("status") != "生效中":
            return None, f"运行委托 {delegation.get('委托编号')} 当前为{delegation.get('status')}，不能重复撤销"
        if not actor or actor != delegation.get("维护者"):
            return None, "只有授予该委托的维护者可以撤销"

        seq, ts = store.next_event()
        delegation["status"] = "已撤销"
        delegation["pending"] = False
        delegation["撤权时间"] = ts
        delegation["撤权序号"] = seq

        # 在途回执与撤权到达时机冲突：给该委托名下所有审计记录补记撤权时间，
        # 已回执的标记「回执先于撤权」，未回执的等回执到达时按序号判定先后。
        for audit in store.rows("delegation_audit"):
            if str(audit.get("委托编号")) != str(delegation.get("委托编号")):
                continue
            audit["撤权时间"] = ts
            audit["撤权序号"] = seq
            if audit.get("status") == "已回执":
                audit["时序结论"] = "回执先于撤权"
            elif audit.get("status") == "已派发":
                audit["时序结论"] = "派发先于撤权，回执未达"
        return delegation, f"运行委托 {delegation.get('委托编号')} 已撤销，停权后不得发起新观测"

    def run_action(self, entry_id: int, action: str, actor: str | None = None) -> tuple[dict[str, Any] | None, str]:
        if action == "撤销委托":
            return self.revoke(entry_id, actor)
        if action == "关闭授权":
            from app.services import service_for

            delegation = store.find(self.MODULE, entry_id)
            if delegation is None:
                return None, f"运行委托 {entry_id} 不存在"
            if delegation.get("status") != "已撤销":
                return None, f"运行委托 {delegation.get('委托编号')} 当前为{delegation.get('status')}，只有已撤销授权需要关闭"
            template_service = service_for("template_rule")
            template_id = int(str(delegation.get("模板ID")))
            template_service.close_revoked_grants(template_id=template_id)
            return store.find(self.MODULE, entry_id), f"运行委托 {delegation.get('委托编号')} 已联动关闭"
        return super().run_action(entry_id, action, actor=actor)

    # ------------------------------------------------------------------
    # 查询辅助：供探针选择消费许可
    # ------------------------------------------------------------------
    def granted_fields(self, delegation: dict[str, Any]) -> list[str]:
        return split_list(delegation.get("授权字段"))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["delegation_state"] = {
            "active": sum(1 for row in rows if row.get("status") == "生效中"),
            "revoked": sum(1 for row in rows if row.get("status") == "已撤销"),
            "closed": sum(1 for row in rows if row.get("status") == "已关闭"),
        }
        return base
