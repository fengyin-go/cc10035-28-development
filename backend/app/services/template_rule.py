"""模板规则领域服务：管理站点级字段抽取模板。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def split_list(value: Any) -> list[str]:
    """把逗号分隔（中英文逗号均可）的字段串拆成去空白列表。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace("，", ",").split(",") if part.strip()]


class TemplateRuleService(BaseModuleService):
    MODULE = "template_rule"
    ENTITY = "规则模板"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["模板名称", "适用站点", "提取字段", "字段选择器", "采样页面", "校验规则", "更新策略"]
    REQUIRED_FIELDS = ["模板名称", "适用站点", "提取字段"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已失效"]
    ACTION_RULES = {"验证模板": "草稿", "发布模板": "已验证", "标记失效": "已发布"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["模板版本"] = 1
        return entry, missing

    # ------------------------------------------------------------------
    # 选择器范围与委托校验
    # ------------------------------------------------------------------
    def selector_scope(self, template: dict[str, Any]) -> dict[str, str]:
        """解析已发布模板的选择器范围：{提取字段: 选择器}。

        「字段选择器」支持两种写法：`字段:选择器` 显式绑定，或裸选择器按
        顺序对齐「提取字段」。委托授权只允许落在这个范围内。
        """
        fields = split_list(template.get("提取字段"))
        entries = split_list(template.get("字段选择器"))
        scope: dict[str, str] = {}
        positional = 0
        for entry in entries:
            if ":" in entry or "=" in entry:
                name, selector = entry.replace("=", ":", 1).split(":", 1)
                name, selector = name.strip(), selector.strip()
                if name:
                    scope[name] = selector
            elif positional < len(fields):
                scope[fields[positional]] = entry
            positional += 1
        for field in fields:
            scope.setdefault(field, "")
        return scope

    def validate_grant(self, template_id: int, fields: list[str]) -> tuple[bool, str, dict[str, Any]]:
        """校验一次委托授予：模板必须已发布，字段必须落在选择器范围内。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return False, f"模板 {template_id} 不存在", {}
        if template.get("status") != "已发布":
            return False, f"模板「{template.get('模板名称')}」未发布（当前{template.get('status')}），不能授予运行委托", {}
        if not fields:
            return False, "授权字段不能为空", {}
        scope = self.selector_scope(template)
        unknown = [field for field in fields if field not in scope]
        if unknown:
            return False, f"字段{'、'.join(unknown)}不在模板提取字段内", {}
        no_selector = [field for field in fields if not scope.get(field)]
        if no_selector:
            return False, f"字段{'、'.join(no_selector)}缺少字段选择器，超出可选择器范围", {}
        return True, "", {
            "template": template,
            "scope": {field: scope[field] for field in fields},
            "version": int(template.get("模板版本", 1)),
        }

    # ------------------------------------------------------------------
    # 模板维护入口：编辑（越权拒绝）与联动关闭
    # ------------------------------------------------------------------
    def edit_template(self, template_id: int, values: dict[str, Any], actor: str) -> tuple[dict[str, Any] | None, str]:
        """维护者编辑模板并递增版本；仅持有运行委托的协作者越权编辑会被拒绝。

        并发更新模板与委托时，既有委托仍按授予时钉住的字段与有效版本执行，
        编辑不回写委托，保证原有许可与审计回执一致。
        """
        template = store.find(self.MODULE, template_id)
        if template is None:
            return None, f"模板 {template_id} 不存在"
        delegations = [
            row for row in store.rows("probe_delegation")
            if str(row.get("模板ID")) == str(template_id)
        ]
        is_collaborator = any(row.get("协作者") == actor for row in delegations)
        is_maintainer = any(row.get("维护者") == actor for row in delegations)
        if is_collaborator and not is_maintainer:
            return None, f"{actor} 仅持有运行委托，运行权不包含修改模板，越权编辑已拒绝"
        changed = False
        for field in self.LIST_FIELDS:
            if field in values and values[field] is not None:
                template[field] = values[field]
                changed = True
        if changed:
            template["模板版本"] = int(template.get("模板版本", 1)) + 1
            _, template["版本更新时间"] = store.next_event()
        return template, f"模板已更新到版本 {template.get('模板版本', 1)}"

    def close_revoked_grants(self, template_id: int | None = None) -> list[str]:
        """模板维护入口的联动关闭：把已撤销的委托置为已关闭，并作废其冻结回执。

        返回被关闭的委托编号列表，可重复调用（幂等）。
        """
        closed: list[str] = []
        for row in store.rows("probe_delegation"):
            if template_id is not None and str(row.get("模板ID")) != str(template_id):
                continue
            if row.get("status") != "已撤销":
                continue
            seq, ts = store.next_event()
            row["status"] = "已关闭"
            row["pending"] = False
            row["关闭时间"] = ts
            row["关闭序号"] = seq
            closed.append(str(row.get("委托编号")))
            for audit in store.rows("delegation_audit"):
                if str(audit.get("委托编号")) == str(row.get("委托编号")) and audit.get("status") == "已冻结":
                    audit["status"] = "已关闭"
                    audit["pending"] = False
                    audit["结果可用性"] = "已作废"
                    audit["时序结论"] = "派发先于撤权，回执随授权关闭作废"
        return closed

    def run_action(self, entry_id: int, action: str, actor: str | None = None) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, actor=actor)
        if entry is not None and action == "标记失效":
            # 联动：模板失效时关闭已经撤销的授权，避免悬空的运行许可。
            closed = self.close_revoked_grants(template_id=entry_id)
            if closed:
                message = f"{message}，联动关闭已撤销授权：{'、'.join(closed)}"
        return entry, message

    # ------------------------------------------------------------------
    # 既有跨模块消费接口
    # ------------------------------------------------------------------
    def suggest_probes(self, template_id: int) -> dict[str, Any]:
        """根据模板规则推导建议的探针配置，供探针选择模块消费。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        site = template.get("适用站点", "")
        selectors_str = template.get("字段选择器", "")
        sample_page = template.get("采样页面", "")
        selectors = [s.strip() for s in selectors_str.split(",") if s.strip()]

        probes = []
        for i, sel in enumerate(selectors):
            page = sample_page if sample_page else site
            probes.append({
                "探针名称": f"{template.get('模板名称')}-probe-{i+1}",
                "目标页面": page,
                "选择器列表": sel,
                "验证关键字": "",
                "采样频率": "daily",
                "超时秒数": "30",
                "重试次数": "3",
            })
        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "suggested_probe_count": len(probes),
            "probes": probes,
        }

    def compute_template_coverage(self) -> dict[str, Any]:
        """计算模板覆盖范围，供抽取指标消费。"""
        rows = store.rows(self.MODULE)
        sites: dict[str, int] = {}
        total_selectors = 0
        total_fields = 0
        for row in rows:
            site = row.get("适用站点", "")
            sites[site] = sites.get(site, 0) + 1
            total_selectors += len(row.get("字段选择器", "").split(",")) if row.get("字段选择器", "") else 0
            total_fields += len(row.get("提取字段", "").split(",")) if row.get("提取字段", "") else 0
        return {
            "template_count": len(rows),
            "site_count": len(sites),
            "total_selectors": total_selectors,
            "total_target_fields": total_fields,
            "published": sum(1 for r in rows if r.get("status") == "已发布"),
        }

    def get_published_templates(self) -> list[dict[str, Any]]:
        """取出所有已发布模板，供质量检查跨模块比对。"""
        return [
            {
                "id": r.get("id"),
                "name": r.get("模板名称"),
                "site": r.get("适用站点"),
                "fields": r.get("提取字段"),
                "selectors": r.get("字段选择器"),
                "sample_page": r.get("采样页面"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") == "已发布"
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["template_coverage"] = self.compute_template_coverage()
        return base
