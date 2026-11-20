"""运行委托领域路由：授予、模板维护编辑、观测派发与回执完成。

通用资源路由（/api/{module}/...）继续可用；这里承载需要操作者与
跨模块校验的领域动作，权限判断全部落在 services 层。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import GrantPayload, ObservePayload, TemplateEditPayload
from app.services import service_for

router = APIRouter(prefix="/api", tags=["运行委托"])


@router.post("/template_rule/{template_id}/grant", response_model=dict)
def grant_delegation(template_id: int, payload: GrantPayload) -> dict[str, Any]:
    """维护者授予协作者按字段运行探针的临时许可。"""
    service = service_for("probe_delegation")
    entry, message = service.grant(
        template_id=template_id,
        maintainer=payload.maintainer.strip(),
        collaborator=payload.collaborator.strip(),
        fields=payload.fields,
        expires=payload.expires,
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/template_rule/{template_id}/edit", response_model=dict)
def edit_template(template_id: int, payload: TemplateEditPayload) -> dict[str, Any]:
    """模板维护入口：维护者编辑模板；仅持运行委托的协作者越权编辑被拒绝。"""
    service = service_for("template_rule")
    entry, message = service.edit_template(template_id, payload.values, payload.actor.strip())
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/probe_selector/{probe_id}/observe", response_model=dict)
def dispatch_observation(probe_id: int, payload: ObservePayload) -> dict[str, Any]:
    """发起观测：消费许可、字段与有效版本，生成快照请求并回写审计。"""
    service = service_for("probe_selector")
    result = service.dispatch_observation(
        probe_id=probe_id,
        actor=payload.actor.strip(),
        fields=payload.fields,
        delegation_id=payload.delegation_id,
        template_id=payload.template_id,
    )
    return result


@router.post("/delegation_audit/{audit_id}/receipt", response_model=dict)
def complete_receipt(audit_id: int) -> dict[str, Any]:
    """回执完成：按撤权是否先到达决定结果即时可用还是冻结待确认。"""
    service = service_for("delegation_audit")
    entry, message = service.complete_receipt(audit_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "entry": entry}
