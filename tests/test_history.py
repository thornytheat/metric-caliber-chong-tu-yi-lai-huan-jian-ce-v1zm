"""历史对比：新增 / 持续 / 消失，以及新增问题带出的变化指标、版本与下游。"""

from __future__ import annotations

from app import conflict, history, registry, report


def _payload(metrics, baseline=None):
    return report.build_check(metrics, check_id="chk_test", baseline=baseline)


def _by_kind(items, kind):
    return [item for item in items if item["kind"] == kind]


def test_first_check_lists_everything_as_new(metrics):
    current = _payload(metrics)
    comparison = current["comparison"]
    assert comparison["has_baseline"] is False
    assert comparison["first_check"] is True
    assert comparison["persistent_count"] == 0
    assert comparison["resolved_count"] == 0
    # 3 组冲突 + 2 条环 + 2 个发布阻断 = 7 个问题
    assert comparison["new_count"] == 7
    assert {item["kind"] for item in comparison["new"]} == {
        "conflict",
        "cycle",
        "publish_blocked",
    }
    for item in comparison["new"]:
        assert item["status"] == "new"
        assert item["versions"]
    assert {item["name"] for item in _by_kind(comparison["new"], "conflict")} == {
        "conversion_rate",
        "gmv",
        "uv",
    }


def test_each_new_issue_carries_downstream(metrics):
    comparison = _payload(metrics)["comparison"]
    by_key = {item["key"]: item for item in comparison["new"]}
    gmv = by_key["conflict:gmv"]
    # gmv@3 被 pay_gmv_share@1 引用，属于该冲突的外部下游
    assert gmv["downstream_count"] == 1
    assert [item["key"] for item in gmv["downstream"]] == ["pay_gmv_share@1"]
    assert all(item["depth"] >= 1 for item in gmv["downstream"])
    # 下游清单不重复列出问题自身涉及的版本
    assert not ({item["key"] for item in gmv["downstream"]} & set(gmv["versions"]))
    # 环内节点互引，没有外部下游
    cycle = next(
        item
        for item in _by_kind(comparison["new"], "cycle")
        if "cpa@1" in item["versions"]
    )
    assert cycle["downstream_count"] == 0


def test_persistent_and_resolved_after_registry_change(metrics):
    baseline = _payload(metrics)
    baseline = {**baseline, "status": "confirmed"}

    changed = [dict(metric) for metric in metrics]
    for metric in changed:
        # 修掉 uv 异口径：uv@2 改成与 uv@1 同口径
        if metric["name"] == "uv" and metric["version"] == 2:
            metric["expression"] = "count(dim:user_id)"
        # 修掉 conversion_rate 百分比口径
        if metric["name"] == "conversion_rate" and metric["version"] == 2:
            metric["expression"] = "metric:order_count / metric:visit_count"
        # 新增一个同名异口径：basket 两个口径
    changed.append(
        registry.normalize_metric(
            {
                "name": "basket",
                "version": 1,
                "expression": "metric:order_amount",
                "aggregation": "sum",
                "dimensions": ["dt"],
                "published": True,
            }
        )
    )
    changed.append(
        registry.normalize_metric(
            {
                "name": "basket",
                "version": 2,
                "expression": "metric:pay_amount",
                "aggregation": "sum",
                "dimensions": ["dt"],
                "published": False,
            }
        )
    )

    comparison = _payload(changed, baseline)["comparison"]
    assert comparison["has_baseline"] is True
    new_keys = {item["key"] for item in comparison["new"]}
    resolved_keys = {item["key"] for item in comparison["resolved"]}
    persistent_keys = {item["key"] for item in comparison["persistent"]}

    assert "conflict:basket" in new_keys
    assert {"conflict:uv", "conflict:conversion_rate"} <= resolved_keys
    # gmv 三口径没动，仍然持续
    assert "conflict:gmv" in persistent_keys
    # 两条环都没动，仍然持续
    baseline_cycle_keys = {item["key"] for item in _by_kind(baseline["comparison"]["new"], "cycle")}
    assert {item["key"] for item in _by_kind(comparison["persistent"], "cycle")} == baseline_cycle_keys
    assert len(baseline_cycle_keys) == 2


def test_new_issue_explains_what_changed(metrics):
    baseline = _payload(metrics)
    baseline = {**baseline, "status": "confirmed"}

    changed = [dict(metric) for metric in metrics]
    for metric in changed:
        if metric["name"] == "gmv" and metric["version"] == 1:
            metric["expression"] = "metric:order_amount + metric:refund_amount"
    for metric in changed:
        if metric["name"] == "gmv_daily" and metric["version"] == 1:
            metric["dimensions"] = ["dt"]

    comparison = _payload(changed, baseline)["comparison"]
    by_key = {item["key"]: item for item in comparison["new"] + comparison["persistent"]}
    # gmv 的组数没变（3 组），但口径内容变了：应能在持续项里看到版本变化
    gmv = by_key["conflict:gmv"]
    assert gmv["status"] == "persistent"

    metric_changes = comparison["metric_changes"]
    changed_keys = {item["key"] for item in metric_changes["changed"]}
    assert "gmv@1" in changed_keys
    assert "gmv_daily@1" in changed_keys
    gmv_change = next(item for item in metric_changes["changed"] if item["key"] == "gmv@1")
    assert "expression" in gmv_change["fields"]
    assert gmv_change["fields"]["expression"]["before"] == "metric:order_amount"
    assert gmv_change["fields"]["expression"]["after"] == (
        "metric:order_amount + metric:refund_amount"
    )
    # 归一化口径也一并给出，便于判断是否只是写法变化
    assert gmv_change["fields"]["normalized"]["after"] == (
        "metric:order_amount+metric:refund_amount"
    )


def test_new_publish_blocker_appears_when_a_dependency_is_unpublished(metrics):
    baseline = _payload(metrics)
    baseline = {**baseline, "status": "confirmed"}

    changed = [dict(metric) for metric in metrics]
    for metric in changed:
        if metric["name"] == "gmv_daily" and metric["version"] == 1:
            metric["expression"] = "metric:gmv@3 - metric:refund_amount"

    comparison = _payload(changed, baseline)["comparison"]
    new = {item["key"]: item for item in comparison["new"]}
    assert "publish_blocked:gmv_daily@1" in new
    item = new["publish_blocked:gmv_daily@1"]
    assert item["detail"]["unpublished_refs"] == ["gmv@3"]
    assert item["changes"][0]["key"] == "gmv_daily@1"
    assert item["changes"][0]["fields"]["expression"]["after"] == (
        "metric:gmv@3 - metric:refund_amount"
    )
    # 下游：gmv_7d / gmv_index / arpu / revenue / pay_gmv_share / uv_per_gmv
    assert item["downstream_count"] == 6


def test_bypassing_normalization_does_not_create_false_new_issue(metrics):
    """只改原始写法（加空格/括号）但归一化口径不变：不应新增问题。"""
    baseline = _payload(metrics)
    baseline = {**baseline, "status": "confirmed"}
    changed = [dict(metric) for metric in metrics]
    for metric in changed:
        if metric["name"] == "gmv" and metric["version"] == 1:
            metric["expression"] = "( metric:order_amount ) * 1"

    comparison = _payload(changed, baseline)["comparison"]
    assert comparison["new_count"] == 0
    assert comparison["resolved_count"] == 0
    assert comparison["persistent_count"] == 7
    # 版本字段确实变了（原始表达式），但口径不变
    changes = {item["key"]: item for item in comparison["metric_changes"]["changed"]}
    assert "gmv@1" in changes


def test_issue_map_is_stable_for_same_snapshot(metrics):
    first = history.issue_map(_payload(metrics))
    second = history.issue_map(_payload(metrics))
    assert set(first) == set(second)
    assert all(first[key]["kind"] == second[key]["kind"] for key in first)


def test_compare_without_baseline_marks_everything_new(metrics):
    current = _payload(metrics)
    comparison = history.compare(current, None)
    assert comparison["has_baseline"] is False
    assert comparison["new_count"] == len(comparison["new"]) == 7


def test_conflict_issue_key_by_name_not_by_grouping(metrics):
    conflicts = conflict.detect_conflicts(metrics)
    payload = {"conflicts": conflicts, "cycles": [], "publish_blocked": [], "unresolved": []}
    items = history.issue_map(payload)
    assert sorted(items) == ["conflict:conversion_rate", "conflict:gmv", "conflict:uv"]
