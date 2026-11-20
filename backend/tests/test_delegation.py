"""运行委托全链路验收测试。

覆盖需求约定：
- 维护者授予协作者按字段运行探针的临时许可（选择器范围与委托记录校验）；
- 协作者只读受控探针配置数据集，运行权不包含修改模板（越权编辑拒绝）；
- 探针选择消费许可、字段与有效版本生成快照请求，实际字段与派发结果回写审计；
- 回执完成前撤销委托：记录派发与撤权的实际先后及结果可用时间，在途回执冻结；
- 停权后不得发起新观测；并发更新模板与委托时只允许先前授予的字段；
- 模板维护入口（标记失效）联动关闭已经撤销的授权。
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)

TEMPLATE_ID = 3          # 已发布的电商商品页模板
PROBE_ID = 3             # 商品页探针
DELEGATION_ID = 1        # dlg-001：owner-01 授予 collab-01「标题,价格」
MAINTAINER = "owner-01"
COLLABORATOR = "collab-01"


@pytest.fixture(autouse=True)
def restore_store():
    """每个用例前后还原内存仓库，保证用例互不影响。"""
    tables = copy.deepcopy(store._tables)
    seq = store._seq
    yield
    store._tables = tables
    store._seq = seq


# ----------------------------------------------------------------------
# 授予校验：发布模板的选择器范围与委托记录
# ----------------------------------------------------------------------
def test_grant_on_unpublished_template_rejected():
    resp = client.post(
        "/api/template_rule/1/grant",
        json={"maintainer": MAINTAINER, "collaborator": "collab-09", "fields": ["标题"]},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "未发布" in body["message"]


def test_grant_outside_selector_scope_rejected():
    resp = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/grant",
        json={"maintainer": MAINTAINER, "collaborator": "collab-09", "fields": ["评论数"]},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "不在模板提取字段内" in body["message"]


def test_grant_within_scope_recorded_with_version():
    resp = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/grant",
        json={"maintainer": MAINTAINER, "collaborator": "collab-09",
              "fields": ["标题", "库存"], "expires": "2026-12-01"},
    )
    body = resp.json()
    assert body["ok"] is True
    entry = body["entry"]
    assert entry["status"] == "生效中"
    assert entry["授权字段"] == "标题,库存"
    assert entry["有效版本"] == "1"          # 授予时钉住模板有效版本
    assert entry["委托编号"].startswith("dlg-")


def test_grant_field_without_selector_rejected():
    # 模板加一个没有选择器的字段后再授予，应被选择器范围校验拦下
    edit = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/edit",
        json={"actor": MAINTAINER, "values": {"提取字段": "标题,价格,库存,评分"}},
    )
    assert edit.json()["ok"] is True
    resp = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/grant",
        json={"maintainer": MAINTAINER, "collaborator": "collab-09", "fields": ["评分"]},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "缺少字段选择器" in body["message"]


# ----------------------------------------------------------------------
# 维护者与受托协作者分别发起观察
# ----------------------------------------------------------------------
def test_maintainer_direct_observation_publishes_snapshot():
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": MAINTAINER, "template_id": TEMPLATE_ID, "fields": ["标题", "价格", "库存"]},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["delegation_code"] == "维护者直采"
    request = body["snapshot_request"]
    assert request["页面URI"] == "https://shop.example.com/item/1"

    audit_id = body["audit_id"]
    receipt = client.post(f"/api/delegation_audit/{audit_id}/receipt")
    assert receipt.json()["ok"] is True

    audit = receipt.json()["entry"]
    assert audit["status"] == "已回执"
    assert audit["结果可用性"] == "即时可用"
    assert audit["结果可用时间"]

    snaps = client.get("/api/page_snapshot", params={"页面URI": "item/1"}).json()
    assert any(item["快照ID"] == request["快照ID"] for item in snaps["items"])


def test_collaborator_observation_within_grant():
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题"]},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["delegation_code"] == "dlg-001"
    assert body["template_version"] == "1"

    receipt = client.post(f"/api/delegation_audit/{body['audit_id']}/receipt")
    audit = receipt.json()["entry"]
    # 实际使用字段与派发结果回写到委托审计清单
    assert audit["实际字段"] == "标题"
    assert audit["操作者"] == COLLABORATOR
    assert audit["派发结果"] == "已派发-回执完成"
    assert audit["模板版本"] == "1"


def test_collaborator_observation_outside_grant_rejected():
    # 「库存」在模板内但未授予 dlg-001
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["库存"]},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "超出已授予范围" in body["reason"]

    # 完全不存在的字段同样拒绝
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["评论数"]},
    )
    assert resp.json()["ok"] is False


def test_collaborator_cannot_use_others_delegation():
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": "collab-02", "delegation_id": DELEGATION_ID, "fields": ["标题"]},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "不属于" in body["reason"]


# ----------------------------------------------------------------------
# 越权编辑与并发更新
# ----------------------------------------------------------------------
def test_collaborator_edit_template_rejected_maintainer_allowed():
    resp = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/edit",
        json={"actor": COLLABORATOR, "values": {"提取字段": "标题"}},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "运行权不包含修改模板" in body["message"]

    resp = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/edit",
        json={"actor": MAINTAINER, "values": {"提取字段": "标题,价格,库存,评分",
                                            "字段选择器": "标题:h1.title,价格:.price,库存:.stock,评分:.score"}},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["entry"]["模板版本"] == 2     # 维护者编辑递增版本


def test_concurrent_update_keeps_previously_granted_fields():
    # 维护者并发更新模板（加字段、升版本），委托仍钉住授予时的字段与版本
    edit = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/edit",
        json={"actor": MAINTAINER, "values": {"提取字段": "标题,价格,库存,评分",
                                            "字段选择器": "标题:h1.title,价格:.price,库存:.stock,评分:.score"}},
    )
    assert edit.json()["entry"]["模板版本"] == 2

    # 新字段「评分」不在既有委托内 → 拒绝（只允许先前授予的字段）
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["评分"]},
    )
    assert resp.json()["ok"] is False

    # 既有授权字段仍可派发，审计记录的模板版本与授予时一致（原有许可与审计回执一致）
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["价格"]},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["template_version"] == "1"
    receipt = client.post(f"/api/delegation_audit/{body['audit_id']}/receipt")
    audit = receipt.json()["entry"]
    assert audit["模板版本"] == "1"
    assert audit["实际字段"] == "价格"


# ----------------------------------------------------------------------
# 回执完成前撤销委托：时序记录、冻结与处置
# ----------------------------------------------------------------------
def test_revoke_before_receipt_freezes_result_and_records_order():
    # 1. 协作者按许可派发一组探针（在途）
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题", "价格"]},
    )
    dispatch = resp.json()
    assert dispatch["ok"] is True
    audit_id = dispatch["audit_id"]

    # 2. 非维护者不能撤销
    denied = client.post(
        f"/api/probe_delegation/{DELEGATION_ID}/actions",
        json={"action": "撤销委托", "actor": COLLABORATOR},
    )
    assert denied.json()["ok"] is False

    # 3. 维护者在回执完成前撤销委托
    revoke = client.post(
        f"/api/probe_delegation/{DELEGATION_ID}/actions",
        json={"action": "撤销委托", "actor": MAINTAINER},
    )
    assert revoke.json()["ok"] is True
    delegation = revoke.json()["entry"]
    assert delegation["status"] == "已撤销"
    assert delegation["撤权时间"]

    # 4. 停权后不得发起新观测
    blocked = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题"]},
    )
    assert blocked.json()["ok"] is False
    assert "停权后不得发起新观测" in blocked.json()["reason"]

    # 5. 在途回执到达 → 冻结，记录派发与撤权的实际先后
    snap_count_before = client.get("/api/page_snapshot").json()["total"]
    receipt = client.post(f"/api/delegation_audit/{audit_id}/receipt")
    audit = receipt.json()["entry"]
    assert audit["status"] == "已冻结"
    assert audit["时序结论"] == "派发先于撤权，回执晚于撤权"
    assert audit["结果可用性"] == "冻结待确认"
    assert audit["派发序号"] < audit["撤权序号"] < audit["回执序号"]
    assert audit["派发时间"] <= audit["撤权时间"] <= audit["回执时间"]

    # 6. 冻结期间快照不发布（不擅自改变维护者收到的数据）
    assert client.get("/api/page_snapshot").json()["total"] == snap_count_before

    # 7. 维护者确认后快照发布，记录结果可用时间
    settle = client.post(
        f"/api/delegation_audit/{audit_id}/actions",
        json={"action": "确认回执", "actor": MAINTAINER},
    )
    settled = settle.json()["entry"]
    assert settled["status"] == "已回执"
    assert settled["结果可用性"] == "撤权后确认可用"
    assert settled["结果可用时间"] >= settled["回执时间"]
    snaps = client.get("/api/page_snapshot").json()["items"]
    assert any(item["快照ID"] == dispatch["snapshot_request"]["快照ID"] for item in snaps)


def test_frozen_receipt_can_be_discarded():
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["价格"]},
    )
    audit_id = resp.json()["audit_id"]
    client.post(f"/api/probe_delegation/{DELEGATION_ID}/actions",
                json={"action": "撤销委托", "actor": MAINTAINER})
    client.post(f"/api/delegation_audit/{audit_id}/receipt")

    snap_count_before = client.get("/api/page_snapshot").json()["total"]
    settle = client.post(
        f"/api/delegation_audit/{audit_id}/actions",
        json={"action": "作废回执", "actor": MAINTAINER},
    )
    audit = settle.json()["entry"]
    assert audit["status"] == "已关闭"
    assert audit["结果可用性"] == "已作废"
    assert client.get("/api/page_snapshot").json()["total"] == snap_count_before


def test_receipt_before_revoke_stays_effective():
    # 回执先于撤权完成：结果即时可用，撤销不回溯
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题"]},
    )
    audit_id = resp.json()["audit_id"]
    client.post(f"/api/delegation_audit/{audit_id}/receipt")
    client.post(f"/api/probe_delegation/{DELEGATION_ID}/actions",
                json={"action": "撤销委托", "actor": MAINTAINER})

    audit = client.get(f"/api/delegation_audit/{audit_id}").json()
    assert audit["status"] == "已回执"
    assert audit["时序结论"] == "回执先于撤权"
    assert audit["结果可用性"] == "即时可用"
    assert audit["撤权时间"]          # 撤权时间仍被回写，便于核对先后
    assert audit["回执序号"] < audit["撤权序号"]


# ----------------------------------------------------------------------
# 模板维护入口联动关闭已经撤销的授权
# ----------------------------------------------------------------------
def test_template_maintenance_closes_revoked_grants():
    # 在途派发 → 撤销 → 回执冻结
    resp = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题"]},
    )
    audit_id = resp.json()["audit_id"]
    client.post(f"/api/probe_delegation/{DELEGATION_ID}/actions",
                json={"action": "撤销委托", "actor": MAINTAINER})
    client.post(f"/api/delegation_audit/{audit_id}/receipt")

    # 另一个模板维护入口：标记失效，联动关闭已撤销授权
    action = client.post(
        f"/api/template_rule/{TEMPLATE_ID}/actions",
        json={"action": "标记失效", "actor": MAINTAINER},
    )
    assert "联动关闭已撤销授权" in action.json()["message"]

    delegation = client.get(f"/api/probe_delegation/{DELEGATION_ID}").json()
    assert delegation["status"] == "已关闭"
    assert delegation["关闭时间"]

    audit = client.get(f"/api/delegation_audit/{audit_id}").json()
    assert audit["status"] == "已关闭"
    assert audit["结果可用性"] == "已作废"


def test_close_grant_action_on_delegation_row():
    # 行级「关闭授权」只处理已撤销的委托
    active = client.post(
        f"/api/probe_delegation/{DELEGATION_ID}/actions",
        json={"action": "关闭授权", "actor": MAINTAINER},
    )
    assert active.json()["ok"] is False            # 生效中的委托不能关闭

    client.post(f"/api/probe_delegation/{DELEGATION_ID}/actions",
                json={"action": "撤销委托", "actor": MAINTAINER})
    closed = client.post(
        f"/api/probe_delegation/{DELEGATION_ID}/actions",
        json={"action": "关闭授权", "actor": MAINTAINER},
    )
    assert closed.json()["ok"] is True
    assert closed.json()["entry"]["status"] == "已关闭"


# ----------------------------------------------------------------------
# 核对请求、已发布快照和授权审计数据
# ----------------------------------------------------------------------
def test_full_flow_audit_consistency():
    # 维护者直采一次
    direct = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": MAINTAINER, "template_id": TEMPLATE_ID, "fields": ["库存"]},
    ).json()
    client.post(f"/api/delegation_audit/{direct['audit_id']}/receipt")

    # 协作者委托派发一次并完成回执
    delegated = client.post(
        f"/api/probe_selector/{PROBE_ID}/observe",
        json={"actor": COLLABORATOR, "delegation_id": DELEGATION_ID, "fields": ["标题", "价格"]},
    ).json()
    client.post(f"/api/delegation_audit/{delegated['audit_id']}/receipt")

    # 核对快照请求：字段进入快照ID，页面URI与探针一致
    assert delegated["snapshot_request"]["页面URI"] == "https://shop.example.com/item/1"
    assert direct["snapshot_request"]["快照ID"] != delegated["snapshot_request"]["快照ID"]

    # 核对已发布快照：两次观测各发布一条
    snaps = client.get("/api/page_snapshot", params={"size": 100}).json()["items"]
    snap_ids = {item["快照ID"] for item in snaps}
    assert direct["snapshot_request"]["快照ID"] in snap_ids
    assert delegated["snapshot_request"]["快照ID"] in snap_ids

    # 核对授权审计数据：委托编号、实际字段、版本、可用性
    audits = client.get("/api/delegation_audit", params={"size": 100}).json()["items"]
    by_id = {item["id"]: item for item in audits}
    assert by_id[direct["audit_id"]]["委托编号"] == "维护者直采"
    delegated_audit = by_id[delegated["audit_id"]]
    assert delegated_audit["委托编号"] == "dlg-001"
    assert delegated_audit["实际字段"] == "标题,价格"
    assert delegated_audit["模板版本"] == "1"
    assert delegated_audit["结果可用性"] == "即时可用"

    # 委托记录本身保持授予时的字段与版本
    delegation = client.get(f"/api/probe_delegation/{DELEGATION_ID}").json()
    assert delegation["授权字段"] == "标题,价格"
    assert delegation["有效版本"] == "1"
    assert delegation["status"] == "生效中"
