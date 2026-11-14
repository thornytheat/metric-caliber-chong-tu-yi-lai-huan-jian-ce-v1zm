"""FastAPI 路由（薄）：页面渲染 + JSON API，规则全部在 app/expr|graph|conflict|gate。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import (
    conflict,
    expr,
    gate,
    graph as graph_mod,
    registry,
    report,
    seed,
    settings,
    store,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
        # 上次进程异常退出时可能留下 running 记录：标记失败，旧结论仍可读
        store.reclaim_stale_running(conn, stale_running_before(), report.now_iso())
    yield


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def load_metrics() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        metrics = store.all_metrics(conn)
    return [registry.with_normalized(metric) for metric in metrics]


def stale_running_before() -> str:
    """超过 10 分钟仍 running 的记录视为进程已中断。"""
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc).astimezone() - timedelta(minutes=10)
    ).strftime("%Y-%m-%d %H:%M:%S")


def base_context(request: Request, **extra: Any) -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "error": None,
        "notice": None,
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------- 请求模型


class ParseRequest(BaseModel):
    expression: str = Field(default="")
    strict: bool = False


class ImpactRequest(BaseModel):
    name: str
    version: int | None = None
    expression: str
    aggregation: str | None = None
    dimensions: list[str] | None = None


class PublishRequest(BaseModel):
    name: str
    version: int


class RegistryRequest(BaseModel):
    name: str
    expression: str
    version: int | None = None
    aggregation: str = "sum"
    dimensions: list[str] = Field(default_factory=list)
    unit: str = ""
    owner: str = ""
    description: str = ""
    published: bool = False
    allow_missing_reference: bool = False


class CheckRequest(BaseModel):
    """attempt_key 用于重试幂等：同一次点击重试复用同一条记录。"""

    attempt_key: str | None = Field(default=None, max_length=80)


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.count_metrics(conn)
    return {"status": "ok", "app": settings.APP_SHORT, "metrics": rows}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    overview = report.overview(metrics)
    with store.session() as conn:
        store.init_schema(conn)
        recent_checks = store.list_checks(conn, limit=6)
        baseline = store.baseline_check(conn)
        last_attempt = store.latest_any_check(conn)
    top = sorted(
        overview["conflicts"], key=lambda item: item["variant_count"], reverse=True
    )[:3]
    cycles = overview["cycles"]
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "top_conflicts": top,
        "cycles": cycles,
        "seed_result": getattr(app.state, "seed_result", {"seeded": False, "reason": "启动中"}),
        "recent_checks": recent_checks,
        "baseline": baseline,
        "last_attempt": last_attempt,
        "error": None,
        "notice": None,
        "page": "index",
        "active": "index",
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    names = registry.names(metrics)
    versions = {
        name: [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "unit": metric["unit"],
                "published": metric["published"],
                "owner": metric["owner"],
            }
            for metric in registry.versions_of(metrics, name)
        ]
        for name in names
    }
    focus = registry.versions_of(metrics, "gmv_daily")
    focus_metric = focus[0] if focus else None
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "names": names,
        "versions": versions,
        "focus": focus_metric,
        "focus_key": graph_mod.node_key(focus_metric["name"], focus_metric["version"]) if focus_metric else "",
        "focus_rows": graph_mod.dependency_rows(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "focus_downstream": graph_mod.downstream(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "error": None,
        "notice": None,
        "page": "workbench",
        "active": "workbench",
    }
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{check_id}", response_class=HTMLResponse)
def result_page(request: Request, check_id: str, confirm: str | None = None, confirmed: str | None = None) -> HTMLResponse:
    with store.session() as conn:
        store.init_schema(conn)
        payload = store.load_check(conn, check_id)
        baseline = store.baseline_check(conn)
    metrics = load_metrics()
    is_baseline = bool(payload and baseline and baseline["check_id"] == check_id)
    error = None
    notice = None
    if payload is None:
        error = f"体检记录 {check_id} 不存在，请回到首页重新体检"
    elif payload.get("status") == store.CHECK_FAILED:
        error = f"体检 {check_id} 执行失败：{payload.get('error') or '未知错误'}。旧基线结论仍可查看。"
    elif payload.get("status") == store.CHECK_RUNNING:
        notice = "这次体检尚未完成（正在执行或进程中断），暂不能确认为基线。"
    if confirm == "failed":
        error = "确认失败：只有完整成功的候选结论才能确认为基线。"
    if confirmed == "1":
        notice = "已确认为新基线，后续体检将以这份结论为对比基准。"
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "check": payload,
        "check_id": check_id,
        "baseline": baseline,
        "is_baseline": is_baseline,
        "missing": payload is None,
        "error": error,
        "notice": notice if payload is not None else None,
        "page": "result",
        "active": "result",
    }
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    with store.session() as conn:
        store.init_schema(conn)
        latest = store.latest_check(conn)
        last_attempt = store.latest_any_check(conn)
        baseline = store.baseline_check(conn)
        impact = store.latest_impact(conn)
        events = store.list_events(conn, limit=25)
        checks = store.list_checks(conn, limit=12, include_failed=True)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "latest": latest,
        "last_attempt": last_attempt,
        "baseline": baseline,
        "impact": impact,
        "events": events,
        "checks": checks,
        "error": None,
        "notice": None,
        "page": "review",
        "active": "review",
    }
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 注册表


@app.get("/api/registry")
def api_registry() -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    return {
        "app": settings.APP_SHORT,
        "stats": {
            "metric_entries": overview["metric_entries"],
            "metric_names": overview["metric_names"],
            "published_entries": overview["published_entries"],
            "unpublished_entries": overview["unpublished_entries"],
            "conflict_count": overview["conflict_count"],
            "cycle_count": overview["cycle_count"],
            "publish_blocked_count": overview["publish_blocked_count"],
            "unresolved_count": overview["unresolved_count"],
            "cross_version_count": overview["cross_version_count"],
        },
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unit": metric["unit"],
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "owner": metric["owner"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }


@app.post("/api/registry", status_code=201)
def api_register(payload: RegistryRequest) -> JSONResponse:
    metrics = load_metrics()
    version = payload.version if payload.version else registry.next_version(metrics, payload.name)
    if store_metric_exists(metrics, payload.name, version):
        raise HTTPException(
            status_code=409,
            detail=f"{payload.name}@{version} 已存在，请递增 version 后再登记",
        )
    try:
        metric = registry.validate_metric(
            {
                "name": payload.name,
                "version": version,
                "expression": payload.expression,
                "aggregation": payload.aggregation,
                "dimensions": payload.dimensions,
                "unit": payload.unit,
                "owner": payload.owner,
                "description": payload.description,
                "published": payload.published,
            }
        )
    except registry.RegistryError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.message, "field": exc.field})

    analysis = expr.analyze_expression(payload.expression)
    unknown_dims = _unknown_dimensions(analysis, metrics)
    missing_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version")
    ]
    unpublished_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["published"] is False
    ]
    if missing_refs and not payload.allow_missing_reference:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "表达式引用了不存在或没有已发布版本的指标",
                "missing_references": missing_refs,
                "field": "expression",
            },
        )
    if unpublished_refs and payload.published:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "引用了未发布版本，不能直接登记为已发布",
                "unpublished_references": unpublished_refs,
                "field": "expression",
            },
        )

    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.insert_metric(conn, metric, now)
        store.add_event(
            conn,
            "register",
            f"登记指标 {metric['name']}@{metric['version']}（{metric['normalized']}）",
            {
                "key": f"{metric['name']}@{metric['version']}",
                "normalized": metric["normalized"],
                "published": metric["published"],
            },
            now,
        )
    conflicts = conflict.caliber_groups(metrics + [metric], metric["name"])
    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "key": f"{metric['name']}@{metric['version']}",
            "name": metric["name"],
            "version": metric["version"],
            "expression": metric["expression"],
            "normalized": metric["normalized"],
            "aggregation": metric["aggregation"],
            "dimensions": metric["dimensions"],
            "published": metric["published"],
            "unknown_dims": unknown_dims,
            "missing_references": missing_refs,
            "unpublished_references": unpublished_refs,
            "caliber_group_count": len(conflicts),
            "caliber_conflict": len(conflicts) > 1,
        },
    )


def store_metric_exists(metrics: list[dict], name: str, version: int) -> bool:
    return any(
        metric["name"] == name and int(metric["version"]) == int(version) for metric in metrics
    )


def _unknown_dimensions(analysis: dict, metrics: list[dict]) -> list[str]:
    known = set()
    for metric in metrics:
        known.update(metric.get("dimensions") or [])
    return [ref["name"] for ref in analysis["dims"] if ref["name"] not in known]


# --------------------------------------------------------------------------- 解析 / 体检 / 影响面 / 发布


@app.post("/api/parse")
def api_parse(payload: ParseRequest = Body(default=ParseRequest())) -> JSONResponse:
    analysis = expr.analyze_expression(payload.expression)
    if not analysis["ok"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": analysis["error"],
                "position": analysis["position"],
                "normalized": None,
            },
        )
    metrics = load_metrics()
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    unresolved = []
    for ref in analysis["metrics"]:
        resolved = gate._resolve(ref, metrics, index, published)
        if resolved["reason"] or resolved["published"] is False:
            unresolved.append(
                {
                    "target": resolved["target"],
                    "reason": resolved["reason"] or "unpublished",
                    "published": resolved["published"],
                }
            )
    if unresolved and payload.strict:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "表达式引用了不可解析的指标", "unresolved": unresolved},
        )
    return JSONResponse(
        content={
            "ok": True,
            "raw": analysis["raw"],
            "normalized": analysis["normalized"],
            "changed": analysis["raw"] != analysis["normalized"],
            "references": analysis["references"],
            "metrics": [ref["name"] for ref in analysis["metrics"]],
            "dims": [ref["name"] for ref in analysis["dims"]],
            "functions": analysis["functions"],
            "unresolved": unresolved,
        }
    )


def _run_and_store_check(attempt_key: str | None = None) -> dict:
    """执行一次体检并落库。

    幂等：同一 attempt_key 的重试复用同一条记录（失败记录也会被复活，
    而不是新开一条），因此重试既不会重复记录，也不会丢掉上次的失败记录。
    """
    key = (attempt_key or "").strip() or report.new_id("run")
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        record = store.find_check_by_attempt(conn, key)
        if record is not None and record["status"] in (
            store.CHECK_CANDIDATE,
            store.CHECK_CONFIRMED,
        ):
            return record  # 已成功的同一次尝试：直接返回原结论，不重复写入
        baseline = store.baseline_check(conn)
        if record is not None:
            check_id = record["check_id"]
            store.restart_check(conn, check_id, now)
        else:
            check_id = report.new_id("chk")
            try:
                store.start_check(conn, check_id, key, now)
            except sqlite3.IntegrityError:
                # 并发下同 attempt_key 已被另一请求写入：复用那条记录，避免重复
                conn.rollback()
                record = store.find_check_by_attempt(conn, key)
                if record is None:
                    raise
                check_id = record["check_id"]
                store.restart_check(conn, check_id, now)
        conn.commit()

    try:
        metrics = load_metrics()
        payload = report.build_check(metrics, check_id=check_id, baseline=baseline, created_at=now)
    except Exception as exc:  # noqa: BLE001 - 体检失败必须落库且不破坏旧结论
        message = f"{type(exc).__name__}: {exc}"
        with store.session() as conn:
            store.init_schema(conn)
            store.fail_check(conn, check_id, message, now)
            store.add_event(
                conn,
                "check_failed",
                f"体检 {check_id} 失败，已保留记录与旧结论：{message}",
                {"check_id": check_id, "attempt_key": key},
                now,
            )
        raise

    with store.session() as conn:
        store.init_schema(conn)
        payload["status"] = store.CHECK_CANDIDATE
        store.finish_check(conn, check_id, payload)
        store.add_event(
            conn,
            "check",
            _check_event_summary(payload),
            {
                "check_id": payload["check_id"],
                "verdict": payload["verdict"],
                "attempt_key": key,
                "baseline_check_id": payload["baseline"]["check_id"],
                "delta": payload["delta_stats"],
            },
            now,
        )
    return payload


def _check_event_summary(payload: dict) -> str:
    base = (
        f"执行注册表体检：冲突 {payload['stats']['conflict_count']} 组 / "
        f"环 {payload['stats']['cycle_count']} 条 / "
        f"发布阻断 {payload['stats']['publish_blocked_count']} 个"
    )
    delta = payload.get("delta_stats") or {}
    if payload.get("baseline", {}).get("check_id"):
        base += (
            f"；相对基线新增 {delta.get('new_count', 0)} / "
            f"持续 {delta.get('persistent_count', 0)} / "
            f"消失 {delta.get('resolved_count', 0)}"
        )
    else:
        base += "；首次体检（暂无已确认基线）"
    return base


@app.post("/api/check")
def api_check(payload: CheckRequest = Body(default=CheckRequest())) -> dict:
    return _run_and_store_check(payload.attempt_key)


@app.post("/check")
def check_form(attempt_key: str = Form("")) -> RedirectResponse:
    payload = _run_and_store_check(attempt_key)
    return RedirectResponse(url=f"/result/{payload['check_id']}", status_code=303)


@app.post("/api/check/{check_id}/confirm")
def api_confirm_check(check_id: str) -> dict:
    """确认候选体检为基线；未确认前不会自动切换基线。"""
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        existing = store.load_check(conn, check_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"体检记录 {check_id} 不存在")
        try:
            confirmed = store.confirm_check(conn, check_id, now)
        except store.CheckStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        previous = existing.get("baseline") or {}
        if previous.get("check_id") and previous["check_id"] != check_id:
            store.add_event(
                conn,
                "baseline",
                f"基线切换：{previous['check_id']} → {check_id}（{confirmed.get('verdict')}）",
                {"from": previous["check_id"], "to": check_id},
                now,
            )
        else:
            store.add_event(
                conn,
                "baseline",
                f"确认基线：{check_id}（后续体检以此为对比基准）",
                {"to": check_id},
                now,
            )
    return confirmed


@app.post("/check/{check_id}/confirm")
def confirm_check_form(check_id: str) -> RedirectResponse:
    try:
        api_confirm_check(check_id)
    except HTTPException:
        return RedirectResponse(url=f"/result/{check_id}?confirm=failed", status_code=303)
    return RedirectResponse(url=f"/result/{check_id}?confirmed=1", status_code=303)


@app.get("/api/baseline")
def api_baseline() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        baseline = store.baseline_check(conn)
    if baseline is None:
        return {"has_baseline": False, "baseline": None}
    return {
        "has_baseline": True,
        "baseline": {
            "check_id": baseline["check_id"],
            "created_at": baseline["created_at"],
            "confirmed_at": baseline["confirmed_at"],
            "verdict": baseline.get("verdict"),
            "fingerprint": baseline.get("fingerprint"),
            "stats": baseline.get("stats", {}),
        },
    }


@app.get("/api/check/{check_id}")
def api_check_detail(check_id: str) -> dict:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"体检记录 {check_id} 不存在")
    return payload


@app.post("/api/impact")
def api_impact(payload: ImpactRequest) -> dict:
    metrics = load_metrics()
    if not any(metric["name"] == payload.name for metric in metrics):
        raise HTTPException(status_code=404, detail=f"指标 {payload.name} 未注册")
    version = payload.version
    if not version:
        latest = registry.latest_version(metrics, payload.name)
        version = latest["version"] if latest else 1
    try:
        impact = report.build_impact(
            metrics,
            name=payload.name,
            version=int(version),
            expression=payload.expression,
            aggregation=payload.aggregation,
            dimensions=payload.dimensions,
        )
    except gate.ExpressionInvalid as exc:
        raise HTTPException(
            status_code=400, detail={"error": exc.message, "position": exc.position}
        )
    except gate.TargetNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_impact(conn, impact)
        store.add_event(
            conn,
            "impact",
            f"评估 {impact['target']['key']} 变更：下游 {impact['downstream_count']} 个，breaking {impact['breaking_count']} 个",
            {"impact_id": impact["impact_id"], "key": impact["target"]["key"]},
            now,
        )
    return impact


@app.post("/api/publish")
def api_publish(payload: PublishRequest) -> JSONResponse:
    metrics = load_metrics()
    try:
        result = gate.publish_check(metrics, payload.name, int(payload.version))
    except gate.GateError as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    if not result["ok"]:
        return JSONResponse(status_code=409, content=result)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.set_published(conn, payload.name, int(payload.version), True, now)
        store.add_event(
            conn,
            "publish",
            f"发布指标 {result['key']}（{result['normalized']}）",
            {"key": result["key"], "normalized": result["normalized"]},
            now,
        )
    result["published"] = True
    return JSONResponse(status_code=200, content=result)


@app.get("/api/metrics")
def api_metrics() -> dict:
    metrics = load_metrics()
    return {
        "count": len(metrics),
        "names": registry.names(metrics),
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }
