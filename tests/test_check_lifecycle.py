"""体检记录生命周期：基线确认、失败恢复、重试幂等（不重复记录、不丢记录）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import report, settings, store
from app.main import app


@pytest.fixture()
def isolated_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "checks.sqlite3")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _check_count(tmp_path) -> int:
    with store.session() as conn:
        store.init_schema(conn)
        return len(store.list_checks(conn, limit=1000, include_failed=True))


def test_check_is_candidate_until_confirmed(isolated_client):
    first = isolated_client.post("/api/check").json()
    assert first["status"] == "candidate"
    assert first["comparison"]["has_baseline"] is False
    assert first["comparison"]["new_count"] == 7

    baseline = isolated_client.get("/api/baseline").json()
    assert baseline["has_baseline"] is False  # 未确认，不切换基线

    confirmed = isolated_client.post(f"/api/check/{first['check_id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    baseline = isolated_client.get("/api/baseline").json()
    assert baseline["has_baseline"] is True
    assert baseline["baseline"]["check_id"] == first["check_id"]

    # 新体检以已确认基线为参照，且仍是候选，不自动切换基线
    second = isolated_client.post("/api/check").json()
    assert second["status"] == "candidate"
    assert second["comparison"]["has_baseline"] is True
    assert second["comparison"]["baseline_check_id"] == first["check_id"]
    assert second["comparison"]["new_count"] == 0
    assert second["comparison"]["persistent_count"] == 7
    assert isolated_client.get("/api/baseline").json()["baseline"]["check_id"] == first["check_id"]


def test_confirm_unknown_or_failed_check_is_rejected(isolated_client):
    response = isolated_client.post("/api/check/chk_nope/confirm")
    assert response.status_code == 404

    with store.session() as conn:
        store.init_schema(conn)
        store.start_check(conn, "chk_failed", "attempt-x", report.now_iso())
        store.fail_check(conn, "chk_failed", "boom")
    response = isolated_client.post("/api/check/chk_failed/confirm")
    assert response.status_code == 409
    assert "失败" not in response.text or "只有" in response.text
    assert isolated_client.get("/api/baseline").json()["has_baseline"] is False


def test_retry_same_attempt_key_does_not_duplicate(isolated_client):
    attempt = "attempt-fixed-1"
    first = isolated_client.post("/api/check", json={"attempt_key": attempt}).json()
    second = isolated_client.post("/api/check", json={"attempt_key": attempt}).json()
    assert first["check_id"] == second["check_id"]
    with store.session() as conn:
        store.init_schema(conn)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM checks WHERE attempt_key = ?", (attempt,)
        ).fetchone()
    assert rows["c"] == 1
    # 独立 attempt_key 才会新开记录
    third = isolated_client.post("/api/check", json={"attempt_key": "attempt-fixed-2"}).json()
    assert third["check_id"] != first["check_id"]


def test_failure_keeps_old_baseline_and_retry_reuses_failed_row(isolated_client, monkeypatch):
    original_build_check = report.build_check
    first = isolated_client.post("/api/check").json()
    isolated_client.post(f"/api/check/{first['check_id']}/confirm")

    def boom(metrics, **kwargs):
        raise RuntimeError("模拟注册表读取失败")

    monkeypatch.setattr(report, "build_check", boom)
    response = isolated_client.post("/api/check", json={"attempt_key": "attempt-boom"})
    assert response.status_code == 500

    with store.session() as conn:
        store.init_schema(conn)
        failed = store.find_check_by_attempt(conn, "attempt-boom")
    assert failed is not None
    assert failed["status"] == "failed"
    assert "模拟注册表读取失败" in failed["error"]

    # 旧基线仍可读取，最新可用结论仍是旧基线
    baseline = isolated_client.get("/api/baseline").json()
    assert baseline["baseline"]["check_id"] == first["check_id"]
    page = isolated_client.get(f"/result/{first['check_id']}")
    assert str(first["check_id"]) in page.text
    failed_page = isolated_client.get(f"/result/{failed['check_id']}")
    assert failed_page.status_code == 200
    assert "失败" in failed_page.text

    # 恢复后重试同一 attempt_key：复用失败记录，不新增行，且失败轨迹保留在记录里
    monkeypatch.setattr(report, "build_check", original_build_check)
    retried = isolated_client.post("/api/check", json={"attempt_key": "attempt-boom"}).json()
    assert retried["check_id"] == failed["check_id"]
    assert retried["status"] == "candidate"
    with store.session() as conn:
        store.init_schema(conn)
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM checks WHERE attempt_key = ?", ("attempt-boom",)
        ).fetchone()["c"]
        stored = store.load_check(conn, failed["check_id"])
    assert count == 1
    attempts = stored["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert "模拟注册表读取失败" in attempts[0]["error"]
    # 成功后的结论本身是完整的，且不再显示为失败
    assert stored["status"] == "candidate"
    assert stored["error"] == ""
    assert stored["stats"]["conflict_count"] == 3


def test_failed_check_does_not_hide_records(isolated_client, monkeypatch):
    original_build_check = report.build_check
    ok = isolated_client.post("/api/check", json={"attempt_key": "ok-1"}).json()

    def boom(metrics, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(report, "build_check", boom)
    isolated_client.post("/api/check", json={"attempt_key": "bad-1"})
    monkeypatch.setattr(report, "build_check", original_build_check)

    with store.session() as conn:
        store.init_schema(conn)
        all_items = store.list_checks(conn, limit=50, include_failed=True)
        usable = store.list_checks(conn, limit=50)
        failed = store.find_check_by_attempt(conn, "bad-1")
    assert {item["check_id"] for item in all_items} == {ok["check_id"], failed["check_id"]}
    assert any(item["status"] == "failed" for item in all_items)
    assert {item["check_id"] for item in usable} == {ok["check_id"]}


def test_form_retry_posts_attempt_key(isolated_client, monkeypatch):
    original_build_check = report.build_check

    def boom(metrics, **kwargs):
        raise RuntimeError("表单失败")

    monkeypatch.setattr(report, "build_check", boom)
    response = isolated_client.post(
        "/check", data={"attempt_key": "form-attempt"}, follow_redirects=False
    )
    assert response.status_code == 500

    monkeypatch.setattr(report, "build_check", original_build_check)
    retry = isolated_client.post(
        "/check", data={"attempt_key": "form-attempt"}, follow_redirects=False
    )
    assert retry.status_code == 303
    with store.session() as conn:
        store.init_schema(conn)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM checks WHERE attempt_key = ?", ("form-attempt",)
        ).fetchone()
    assert rows["c"] == 1


def test_stale_running_is_reclaimed_and_old_conclusion_survives(isolated_client):
    with store.session() as conn:
        store.init_schema(conn)
        store.start_check(conn, "chk_stale", "attempt-stale", "2020-01-01 00:00:00")
    reclaimed = 0
    with store.session() as conn:
        reclaimed = store.reclaim_stale_running(conn, report.now_iso())
    assert reclaimed == 1
    with store.session() as conn:
        record = store.load_check(conn, "chk_stale")
    assert record["status"] == "failed"
    assert "中断" in record["error"]


def test_unique_attempt_key_blocks_duplicate_rows(tmp_path):
    import sqlite3

    db = tmp_path / "unique.sqlite3"
    with store.session(db) as conn:
        store.init_schema(conn)
        store.start_check(conn, "chk_a", "same-key", report.now_iso())
    try:
        with store.session(db) as conn:
            store.start_check(conn, "chk_b", "same-key", report.now_iso())
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - 唯一索引失效时明确失败
        raise AssertionError("同一 attempt_key 不应写入两条非失败记录")
    with store.session(db) as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM checks").fetchone()
    assert rows["c"] == 1


def test_legacy_check_row_is_migrated_and_readable(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE checks (check_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO checks VALUES (?, ?, ?)",
        (
            "chk_old",
            "2026-01-01 00:00:00",
            '{"verdict": "blocked", "stats": {"conflict_count": 3, "cycle_count": 2, '
            '"publish_blocked_count": 2}, "conflicts": [], "cycles": [], '
            '"publish_blocked": [], "unresolved": [], "metrics": []}',
        ),
    )
    conn.commit()
    conn.close()

    with store.session(db) as conn:
        store.init_schema(conn)
        record = store.load_check(conn, "chk_old")
        baseline = store.baseline_check(conn)
    assert record["status"] == "candidate"
    assert record["comparison"]["has_baseline"] is False
    assert record["comparison"]["new_count"] == 0
    assert baseline is None
