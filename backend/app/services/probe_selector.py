"""探针选择领域服务：管理页面元素的自动化探测探针。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.services.template_rule import split_list
from app.store import store


class ProbeSelectorService(BaseModuleService):
    MODULE = "probe_selector"
    ENTITY = "探针配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["探针名称", "目标页面", "选择器列表", "验证关键字", "采样频率", "超时秒数", "重试次数"]
    REQUIRED_FIELDS = ["探针名称", "目标页面", "选择器列表"]
    STATUS_ORDER = ["草稿", "探测中", "已验证", "已废弃"]
    ACTION_RULES = {"启动探测": "草稿", "验证探针": "探测中", "废弃探针": "已验证"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 观测派发：消费受控探针配置数据集里的许可、字段与有效版本
    # ------------------------------------------------------------------
    def dispatch_observation(
        self,
        *,
        probe_id: int,
        actor: str,
        fields: list[str],
        delegation_id: int | None = None,
        template_id: int | None = None,
    ) -> dict[str, Any]:
        """按许可派发一次观测，生成快照请求并回写委托审计清单。

        - 协作者必须持生效中的委托，且字段不超出授予时钉住的范围；
          停权（已撤销/已关闭）后不得发起新观测。
        - 维护者直采时字段必须落在已发布模板的选择器范围内。
        """
        from app.services import service_for  # 延迟导入，避免模块间循环依赖

        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        if not fields:
            return {"ok": False, "reason": "观测字段不能为空"}

        template_service = service_for("template_rule")
        delegation_service = service_for("probe_delegation")
        audit_service = service_for("delegation_audit")

        delegation = None
        delegation_code = "维护者直采"
        if delegation_id is not None:
            delegation = store.find("probe_delegation", delegation_id)
            if delegation is None:
                return {"ok": False, "reason": f"运行委托 {delegation_id} 不存在"}
            if delegation.get("status") != "生效中":
                return {"ok": False, "reason": f"委托 {delegation.get('委托编号')} 当前{delegation.get('status')}，停权后不得发起新观测"}
            if actor != delegation.get("协作者"):
                return {"ok": False, "reason": f"委托 {delegation.get('委托编号')} 不属于 {actor}"}
            granted = delegation_service.granted_fields(delegation)
            overreach = [field for field in fields if field not in granted]
            if overreach:
                return {"ok": False, "reason": f"字段{'、'.join(overreach)}超出已授予范围，只允许先前授予的字段"}
            delegation_code = str(delegation.get("委托编号"))
            template_id = int(str(delegation.get("模板ID")))
            template_version = str(delegation.get("有效版本"))
        else:
            if template_id is None:
                return {"ok": False, "reason": "维护者直采需要指定模板"}
            template = store.find("template_rule", template_id)
            if template is None:
                return {"ok": False, "reason": f"模板 {template_id} 不存在"}
            if template.get("status") != "已发布":
                return {"ok": False, "reason": f"模板「{template.get('模板名称')}」未发布，不能发起观测"}
            scope = template_service.selector_scope(template)
            out_of_scope = [field for field in fields if field not in scope or not scope.get(field)]
            if out_of_scope:
                return {"ok": False, "reason": f"字段{'、'.join(out_of_scope)}超出模板选择器范围"}
            template_version = str(template.get("模板版本", 1))

        snapshot_request = self._build_snapshot_request(probe, fields)
        audit = audit_service.record_dispatch(
            delegation_code=delegation_code,
            delegation_id=delegation_id,
            actor=actor,
            fields=fields,
            template_version=template_version,
            probe_id=probe_id,
            snapshot_request=snapshot_request,
        )
        return {
            "ok": True,
            "probe_id": probe_id,
            "probe_name": probe.get("探针名称"),
            "delegation_code": delegation_code,
            "template_version": template_version,
            "fields_used": fields,
            "audit_id": audit.get("id"),
            "snapshot_request": snapshot_request,
        }

    def _build_snapshot_request(self, probe: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        target_page = probe.get("目标页面", "")
        selectors = probe.get("选择器列表", "")
        page_hash = hashlib.md5(target_page.encode()).hexdigest()[:8]
        field_tag = hashlib.md5(",".join(fields).encode()).hexdigest()[:6]
        dom_hash = hashlib.sha256(
            f"{target_page}|{selectors}|{field_tag}|{hashlib.md5(b'dummy_dom').hexdigest()[:8]}".encode()
        ).hexdigest()[:16]
        text_hash = hashlib.sha256(
            f"{target_page}|text_content|{field_tag}".encode()
        ).hexdigest()[:16]
        return {
            "快照ID": f"snap-{page_hash}-{field_tag}",
            "页面URI": target_page,
            "DOM哈希": dom_hash,
            "文本哈希": text_hash,
            "抓取时间": "2026-11-20T10:00:00Z",
            "页面大小": "45.2KB",
        }

    # ------------------------------------------------------------------
    # 既有跨模块消费接口
    # ------------------------------------------------------------------
    def trigger_snapshot(self, probe_id: int) -> dict[str, Any]:
        """触发页面快照：探针→快照的跨模块联动。被页面快照消费。"""
        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        target_page = probe.get("目标页面", "")

        return {
            "ok": True,
            "probe_id": probe_id,
            "probe_name": probe.get("探针名称"),
            "target_page": target_page,
            "snapshot_request": self._build_snapshot_request(probe, split_list(probe.get("选择器列表"))),
        }

    def compute_probe_health(self) -> dict[str, Any]:
        """探针健康度计算，供聚合视图和抽取指标消费。"""
        rows = store.rows(self.MODULE)
        valid = sum(1 for r in rows if r.get("status") == "已验证")
        active = sum(1 for r in rows if r.get("status") == "探测中")
        dead = sum(1 for r in rows if r.get("status") == "已废弃")
        total = len(rows)
        return {
            "total_probes": total,
            "valid_probes": valid,
            "active_probes": active,
            "dead_probes": dead,
            "health_ratio": round(valid / total, 2) if total > 0 else 0.0,
        }

    def get_probes_for_page(self, page_uri: str) -> list[dict[str, Any]]:
        """按目标页面过滤探针。"""
        return [
            {"id": r.get("id"), "name": r.get("探针名称"),
             "selectors": r.get("选择器列表"), "status": r.get("status")}
            for r in store.rows(self.MODULE)
            if r.get("目标页面") == page_uri
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["probe_health"] = self.compute_probe_health()
        return base
