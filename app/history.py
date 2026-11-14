"""体检历史对比：以「已确认基线」为参照，把问题分成新增 / 持续 / 消失。

纯函数，输入是两份体检 payload（`app/report.build_check` 的产出），
不读取数据库、不依赖 Web 层。对比口径固定：

- 同名异口径：按指标名识别（`conflict:<name>`），组数/版本变化算同一问题的变化；
- 依赖环：按环内节点集合识别（`cycle:<node>|<node>`），方向变化不产生新问题；
- 发布阻断：按被阻断的指标版本识别（`publish_blocked:<name@version>`）；
- 无法解析引用：按「引用方 → 目标」识别（`unresolved:<from>-><target>`）。

新增问题额外带上：实际发生变化的指标版本（含新增/变更/移除字段）、
直接上游里的变化（解释问题为何出现）、以及当前依赖图上的全部传递下游。
"""

from __future__ import annotations

from . import graph as graph_mod

KIND_ORDER = {"conflict": 0, "cycle": 1, "publish_blocked": 2, "unresolved": 3}

KIND_LABELS = {
    "conflict": "同名异口径",
    "cycle": "依赖环",
    "publish_blocked": "发布阻断",
    "unresolved": "无法解析引用",
}

METRIC_FIELDS = ("expression", "normalized", "aggregation", "dimensions", "published")


# --------------------------------------------------------------------------- 基础工具


def _key_order(key: str) -> tuple[str, int]:
    name, _, version = str(key).partition("@")
    try:
        number = int(version)
    except (TypeError, ValueError):
        number = 0
    return name, number


def _sorted_keys(keys) -> list[str]:
    return sorted(set(keys), key=_key_order)


def _slim(metric: dict) -> dict:
    return {
        "key": metric.get("key")
        or graph_mod.node_key(metric.get("name", ""), metric.get("version", 1)),
        "name": metric.get("name", ""),
        "version": metric.get("version"),
        "expression": metric.get("expression", ""),
        "normalized": metric.get("normalized", ""),
        "aggregation": metric.get("aggregation", ""),
        "dimensions": list(metric.get("dimensions") or []),
        "published": bool(metric.get("published")),
        "owner": metric.get("owner", ""),
        "unit": metric.get("unit", ""),
    }


def _field_value(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, bool):
        return value
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- 问题索引


def issue_map(payload: dict) -> dict[str, dict]:
    """把一份体检结果里的四类问题拍平成 `issue_key -> issue`。"""
    items: dict[str, dict] = {}

    for item in payload.get("conflicts") or []:
        versions = []
        groups = []
        for group in item.get("groups") or []:
            version_keys = list(group.get("version_keys") or [])
            versions.extend(version_keys)
            groups.append(
                {
                    "normalized": group.get("normalized", ""),
                    "version_keys": version_keys,
                    "published": bool(group.get("published")),
                }
            )
        key = f"conflict:{item.get('name', '')}"
        items[key] = {
            "key": key,
            "kind": "conflict",
            "kind_label": KIND_LABELS["conflict"],
            "name": item.get("name", ""),
            "title": item.get("summary") or f"{item.get('name', '')} 同名异口径",
            "versions": _sorted_keys(versions),
            "detail": {
                "variant_count": item.get("variant_count"),
                "version_count": item.get("version_count"),
                "groups": groups,
                "detail": "",
            },
        }

    for cycle in payload.get("cycles") or []:
        path = [str(key) for key in (cycle.get("path") or [])]
        node_keys = path[:-1] if path else []
        key = "cycle:" + "|".join(sorted(node_keys))
        items[key] = {
            "key": key,
            "kind": "cycle",
            "kind_label": KIND_LABELS["cycle"],
            "name": "",
            "title": " → ".join(path),
            "versions": _sorted_keys(node_keys),
            "detail": {
                "path": path,
                "length": cycle.get("length"),
                "variant_count": None,
                "version_count": None,
                "groups": [],
                "detail": "",
            },
        }

    for item in payload.get("publish_blocked") or []:
        key = f"publish_blocked:{item.get('key', '')}"
        items[key] = {
            "key": key,
            "kind": "publish_blocked",
            "kind_label": KIND_LABELS["publish_blocked"],
            "name": item.get("name", ""),
            "title": item.get("key", ""),
            "versions": [item.get("key", "")],
            "detail": {
                "unpublished_refs": list(item.get("unpublished_refs") or []),
                "missing_refs": list(item.get("missing_refs") or []),
                "variant_count": None,
                "version_count": None,
                "groups": [],
                "detail": item.get("detail", ""),
            },
        }

    for item in payload.get("unresolved") or []:
        key = f"unresolved:{item.get('from')}->{item.get('target')}"
        items[key] = {
            "key": key,
            "kind": "unresolved",
            "kind_label": KIND_LABELS["unresolved"],
            "name": "",
            "title": f"{item.get('from')} → {item.get('target')}",
            "versions": [item.get("from", "")],
            "detail": {
                "reason": item.get("reason"),
                "variant_count": None,
                "version_count": None,
                "groups": [],
                "detail": item.get("detail", ""),
            },
        }

    return items


def _issue_order(issue: dict) -> tuple[int, str]:
    return KIND_ORDER.get(issue["kind"], 99), issue["key"]


# --------------------------------------------------------------------------- 指标版本变化


def metric_change_index(current: dict, baseline: dict) -> dict[str, dict]:
    """当前快照相对基线的逐版本变化；未变化的条目 status=unchanged。"""
    before = {item["key"]: item for item in baseline.get("metrics") or []}
    after = {item["key"]: item for item in current.get("metrics") or []}
    index: dict[str, dict] = {}
    for key in _sorted_keys(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            index[key] = {
                "key": key,
                "status": "added",
                "before": None,
                "after": _slim(new or {}),
                "fields": {},
            }
        elif new is None:
            index[key] = {
                "key": key,
                "status": "removed",
                "before": _slim(old),
                "after": None,
                "fields": {},
            }
        else:
            fields = {}
            for field in METRIC_FIELDS:
                old_value = _field_value(old.get(field))
                new_value = _field_value(new.get(field))
                if old_value != new_value:
                    fields[field] = {"before": old_value, "after": new_value}
            index[key] = {
                "key": key,
                "status": "changed" if fields else "unchanged",
                "before": _slim(old),
                "after": _slim(new),
                "fields": fields,
            }
    return index


def _changes_for(keys, change_index: dict[str, dict]) -> list[dict]:
    items = []
    for key in _sorted_keys(keys):
        change = change_index.get(key)
        if change is None or change["status"] == "unchanged":
            continue
        items.append(change)
    return items


def _changed_metric_lists(change_index: dict[str, dict]) -> dict:
    groups = {"added": [], "changed": [], "removed": []}
    for change in change_index.values():
        if change["status"] in groups:
            groups[change["status"]].append(change)
    for name in groups:
        groups[name].sort(key=lambda item: _key_order(item["key"]))
    return {
        **groups,
        "counts": {name: len(items) for name, items in groups.items()},
    }


# --------------------------------------------------------------------------- 下游与关联变化


def _graph(payload: dict) -> graph_mod.Graph:
    return graph_mod.build_graph(payload.get("metrics") or [])


def _downstream_for(versions: list[str], graph: graph_mod.Graph) -> list[dict]:
    """问题涉及版本在当前依赖图上的传递下游。

    问题自身的版本会单独列在「涉及版本」里，这里排除它们，只保留真正被波及的外部下游。
    """
    involved = set(versions)
    seen: dict[str, dict] = {}
    for key in versions:
        if key not in graph.nodes:
            continue
        for item in graph_mod.downstream(graph, key):
            target = item["key"]
            if target in involved:
                continue
            depth = int(item.get("depth") or 1)
            existing = seen.get(target)
            if existing is not None and existing["depth"] <= depth:
                continue
            seen[target] = {
                "key": target,
                "name": item.get("name", target.partition("@")[0]),
                "version": item.get("version"),
                "depth": depth,
                "owner": item.get("owner", ""),
                "published": item.get("published"),
                "dimensions": list(item.get("dimensions") or []),
            }
    return sorted(seen.values(), key=lambda item: (item["depth"], _key_order(item["key"])))


def _related_change_keys(
    versions: list[str], graph: graph_mod.Graph, change_index: dict[str, dict]
) -> list[str]:
    """涉及版本的直接上游里，本次实际发生变化的版本（用于解释问题来源）。"""
    related = set()
    for key in versions:
        for dependency in graph.deps.get(key, []):
            change = change_index.get(dependency)
            if change is not None and change["status"] != "unchanged":
                related.add(dependency)
    related.difference_update(set(versions))
    return _sorted_keys(related)


# --------------------------------------------------------------------------- 问题富化


def _enrich(
    issue: dict,
    change_index: dict[str, dict],
    graph: graph_mod.Graph | None,
    status: str,
    baseline_present: bool = True,
) -> dict:
    item = dict(issue)
    item["status"] = status
    versions = list(issue.get("versions") or [])
    item["involved"] = [
        {
            "key": key,
            "status": (
                (change_index.get(key) or {}).get("status", "unchanged")
                if baseline_present
                else "initial"
            ),
        }
        for key in versions
    ]
    item["changes"] = _changes_for(versions, change_index)
    if graph is not None:
        related = _related_change_keys(versions, graph, change_index)
        item["related_changes"] = _changes_for(related, change_index)
        downstream = _downstream_for(versions, graph)
    else:
        item["related_changes"] = []
        downstream = []
    item["downstream"] = downstream[:20]
    item["downstream_count"] = len(downstream)
    return item


def _delta(current_issue: dict, baseline_issue: dict | None) -> dict:
    if not baseline_issue:
        return {"added_versions": [], "removed_versions": []}
    before = set(baseline_issue.get("versions") or [])
    after = set(current_issue.get("versions") or [])
    delta = {
        "added_versions": _sorted_keys(after - before),
        "removed_versions": _sorted_keys(before - after),
    }
    if current_issue.get("kind") == "conflict":
        delta["variant_count_before"] = baseline_issue.get("detail", {}).get("variant_count")
        delta["variant_count_after"] = current_issue.get("detail", {}).get("variant_count")
    return delta


# --------------------------------------------------------------------------- 主入口


def compare(current: dict, baseline: dict | None) -> dict:
    """生成当前体检相对已确认基线的差异结论。"""
    current_map = issue_map(current)

    if baseline is None:
        graph = _graph(current)
        issues = sorted(current_map.values(), key=_issue_order)
        new = [
            _enrich(issue, {}, graph, "new", baseline_present=False) for issue in issues
        ]
        return {
            "has_baseline": False,
            "baseline_check_id": None,
            "baseline_created_at": None,
            "new": new,
            "persistent": [],
            "resolved": [],
            "new_count": len(new),
            "persistent_count": 0,
            "resolved_count": 0,
            "metric_changes": _changed_metric_lists({}),
            "first_check": True,
        }

    baseline_map = issue_map(baseline)
    change_index = metric_change_index(current, baseline)
    graph = _graph(current)

    new = [
        _enrich(current_map[key], change_index, graph, "new")
        for key in sorted(set(current_map) - set(baseline_map))
    ]
    persistent = []
    for key in sorted(set(current_map) & set(baseline_map)):
        item = _enrich(current_map[key], change_index, graph, "persistent")
        item["delta"] = _delta(current_map[key], baseline_map[key])
        persistent.append(item)
    resolved = []
    for key in sorted(set(baseline_map) - set(current_map)):
        item = _enrich(baseline_map[key], change_index, None, "resolved")
        item["delta"] = _delta(baseline_map[key], current_map.get(key))
        resolved.append(item)

    new.sort(key=_issue_order)
    persistent.sort(key=_issue_order)
    resolved.sort(key=_issue_order)

    return {
        "has_baseline": True,
        "baseline_check_id": baseline.get("check_id"),
        "baseline_created_at": baseline.get("created_at"),
        "new": new,
        "persistent": persistent,
        "resolved": resolved,
        "new_count": len(new),
        "persistent_count": len(persistent),
        "resolved_count": len(resolved),
        "metric_changes": _changed_metric_lists(change_index),
        "first_check": False,
    }
