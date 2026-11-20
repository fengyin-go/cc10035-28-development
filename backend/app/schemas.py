"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None
    # 操作者标识：运行委托、委托审计等模块需要按操作者做权限校验，
    # 其余模块忽略该字段，保持既有行为不变。
    actor: str | None = None


class GrantPayload(BaseModel):
    """维护者授予协作者按字段运行探针的委托。"""
    maintainer: str
    collaborator: str
    fields: list[str] = Field(default_factory=list)
    expires: str | None = None


class TemplateEditPayload(BaseModel):
    """模板维护入口的编辑请求：只有维护者可改，协作者越权编辑会被拒绝。"""
    actor: str
    values: dict[str, Any] = Field(default_factory=dict)


class ObservePayload(BaseModel):
    """发起观测：维护者直采传 template_id；受托协作者传 delegation_id。"""
    actor: str
    fields: list[str] = Field(default_factory=list)
    delegation_id: int | None = None
    template_id: int | None = None
