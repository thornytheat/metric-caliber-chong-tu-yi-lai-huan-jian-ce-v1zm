"""SQLite 访问层：指标注册表、体检记录、影响面记录、事件流水。

命名卷 metricring-data 挂到 /app/data，重建数据只需删卷。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import history, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    name        TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    expression  TEXT    NOT NULL,
    unit        TEXT    NOT NULL DEFAULT '',
    aggregation TEXT    NOT NULL DEFAULT 'sum',
    dimensions  TEXT    NOT NULL DEFAULT '[]',
    owner       TEXT    NOT NULL DEFAULT '',
    published   INTEGER NOT NULL DEFAULT 0,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS checks (
    check_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'candidate',
    attempt_key TEXT,
    error       TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS impacts (
    impact_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_checks(conn)
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS checks_definitive_attempt
            ON checks(attempt_key)
            WHERE attempt_key IS NOT NULL AND attempt_key != '' AND status != 'failed';
        CREATE INDEX IF NOT EXISTS checks_status_created
            ON checks(status, created_at);
        """
    )
    conn.commit()


def _migrate_checks(conn: sqlite3.Connection) -> None:
    """旧库补列：体检记录增加状态/幂等键/错误信息，历史记录视为候选。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(checks)").fetchall()}
    additions = {
        "status": "TEXT NOT NULL DEFAULT 'candidate'",
        "attempt_key": "TEXT",
        "error": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE checks ADD COLUMN {column} {definition}")
    conn.commit()


def _row_to_metric(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "version": int(row["version"]),
        "expression": row["expression"],
        "unit": row["unit"],
        "aggregation": row["aggregation"],
        "dimensions": json.loads(row["dimensions"] or "[]"),
        "owner": row["owner"],
        "published": bool(row["published"]),
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def count_metrics(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()
    return int(row["c"])


def all_metrics(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM metrics ORDER BY name ASC, version ASC"
    ).fetchall()
    return [_row_to_metric(row) for row in rows]


def get_metric(conn: sqlite3.Connection, name: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return _row_to_metric(row) if row else None


def upsert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, version) DO UPDATE SET
            expression = excluded.expression,
            unit = excluded.unit,
            aggregation = excluded.aggregation,
            dimensions = excluded.dimensions,
            owner = excluded.owner,
            published = excluded.published,
            description = excluded.description,
            updated_at = excluded.updated_at
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            now,
            now,
        ),
    )


def set_published(conn: sqlite3.Connection, name: str, version: int, published: bool, now: str) -> None:
    conn.execute(
        "UPDATE metrics SET published = ?, updated_at = ? WHERE name = ? AND version = ?",
        (1 if published else 0, now, name, int(version)),
    )


def insert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            now,
            now,
        ),
    )


def metric_exists(conn: sqlite3.Connection, name: str, version: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return row is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------- 体检记录
#
# 一次体检 = 一条 checks 记录，生命周期：
#   running → candidate（成功、待确认）→ confirmed（已确认为基线）
#           ↘ failed（失败，保留错误信息，可用同一 attempt_key 重试并复用该行）
#
# attempt_key 全局唯一（空值除外）：同一 attempt_key 重试只会得到同一条记录，
# 不会重复写入，也不会因为重试把失败记录覆盖丢失（失败原因随后续成功记录一起保留）。

CHECK_RUNNING = "running"
CHECK_FAILED = "failed"
CHECK_CANDIDATE = "candidate"
CHECK_CONFIRMED = "confirmed"

USABLE_STATUSES = (CHECK_CANDIDATE, CHECK_CONFIRMED)
BASELINE_META_KEY = "baseline_check_id"


class CheckStateError(ValueError):
    """体检记录状态不允许该操作。"""


def _row_payload(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload"] or "{}")
    payload.setdefault("check_id", row["check_id"])
    payload["status"] = row["status"]
    payload["error"] = row["error"] or ""
    payload["attempt_key"] = row["attempt_key"] or ""
    payload["confirmed_at"] = row["confirmed_at"] or ""
    if payload.get("stats") and "comparison" not in payload:
        # 旧版体检记录没有历史对比字段：按「无基线」补一份，保证页面可读
        payload.setdefault("fingerprint", "")
        payload.setdefault("baseline", {"check_id": None, "created_at": None, "fingerprint": None})
        payload["comparison"] = history.compare(payload, None)
        payload["delta_stats"] = {
            "new_count": payload["comparison"]["new_count"],
            "persistent_count": 0,
            "resolved_count": 0,
        }
    return payload


def find_check_by_attempt(conn: sqlite3.Connection, attempt_key: str) -> dict | None:
    if not attempt_key:
        return None
    # 优先返回可用结论；同一 attempt_key 理论上最多一条非失败记录
    row = conn.execute(
        "SELECT * FROM checks WHERE attempt_key = ? "
        "ORDER BY (status = 'failed') ASC, created_at DESC, rowid DESC LIMIT 1",
        (attempt_key,),
    ).fetchone()
    return _row_payload(row) if row else None


def start_check(conn: sqlite3.Connection, check_id: str, attempt_key: str, now: str) -> None:
    """写入 running 状态占位；同一 attempt_key 已有记录时由调用方先复用。"""
    conn.execute(
        "INSERT INTO checks (check_id, created_at, payload, status, attempt_key, error, confirmed_at) "
        "VALUES (?, ?, '{}', ?, ?, '', '')",
        (check_id, now, CHECK_RUNNING, attempt_key or None),
    )


def _payload_attempts(conn: sqlite3.Connection, check_id: str) -> list[dict]:
    row = conn.execute("SELECT payload FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    if row is None:
        return []
    payload = json.loads(row["payload"] or "{}")
    return list(payload.get("attempts") or [])


def _write_attempts(conn: sqlite3.Connection, check_id: str, attempts: list[dict]) -> None:
    row = conn.execute("SELECT payload FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    payload = json.loads(row["payload"] or "{}") if row else {}
    payload["attempts"] = attempts[-20:]  # 保留最近 20 次尝试轨迹，避免无限增长
    conn.execute(
        "UPDATE checks SET payload = ? WHERE check_id = ?",
        (json.dumps(payload, ensure_ascii=False), check_id),
    )


def restart_check(conn: sqlite3.Connection, check_id: str, now: str) -> None:
    """复用失败/中断的记录重试：重置为 running，保留 check_id、attempt_key 与历史尝试轨迹。"""
    attempts = _payload_attempts(conn, check_id)
    conn.execute(
        "UPDATE checks SET status = ?, error = '', created_at = ?, payload = ? WHERE check_id = ?",
        (CHECK_RUNNING, now, json.dumps({"attempts": attempts}, ensure_ascii=False), check_id),
    )


def finish_check(conn: sqlite3.Connection, check_id: str, payload: dict) -> None:
    attempts = _payload_attempts(conn, check_id)
    stored = dict(payload)
    stored["attempts"] = attempts
    conn.execute(
        "UPDATE checks SET payload = ?, status = ?, error = '' WHERE check_id = ?",
        (json.dumps(stored, ensure_ascii=False), CHECK_CANDIDATE, check_id),
    )


def fail_check(conn: sqlite3.Connection, check_id: str, error: str, at: str = "") -> None:
    attempts = _payload_attempts(conn, check_id)
    attempts.append({"at": at, "status": CHECK_FAILED, "error": str(error)[:2000]})
    conn.execute(
        "UPDATE checks SET status = ?, error = ? WHERE check_id = ?",
        (CHECK_FAILED, str(error)[:2000], check_id),
    )
    _write_attempts(conn, check_id, attempts)


def reclaim_stale_running(conn: sqlite3.Connection, stale_before: str, at: str = "") -> int:
    """进程中断遗留的 running 记录标记为失败，保证旧结论仍可读取。"""
    cursor = conn.execute(
        "UPDATE checks SET status = ?, error = ? WHERE status = ? AND created_at < ?",
        (CHECK_FAILED, "体检中断：进程退出或超时，旧结论不受影响", CHECK_RUNNING, stale_before),
    )
    count = cursor.rowcount or 0
    if count:
        for row in conn.execute(
            "SELECT check_id FROM checks WHERE status = ? AND error = ?",
            (CHECK_FAILED, "体检中断：进程退出或超时，旧结论不受影响"),
        ).fetchall():
            attempts = _payload_attempts(conn, row["check_id"])
            if not attempts:
                _write_attempts(
                    conn,
                    row["check_id"],
                    [{"at": at or stale_before, "status": CHECK_FAILED, "error": "体检中断"}],
                )
    return count


def load_check(conn: sqlite3.Connection, check_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    return _row_payload(row) if row else None


def latest_check(conn: sqlite3.Connection) -> dict | None:
    """最近一条可用结论（候选或已确认），失败/中断记录不参与。"""
    row = conn.execute(
        "SELECT * FROM checks WHERE status IN (?, ?) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        USABLE_STATUSES,
    ).fetchone()
    return _row_payload(row) if row else None


def latest_any_check(conn: sqlite3.Connection) -> dict | None:
    """最近一次体检尝试（含失败），用于提示上次失败原因。"""
    row = conn.execute(
        "SELECT * FROM checks ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _row_payload(row) if row else None


def baseline_check(conn: sqlite3.Connection) -> dict | None:
    """已确认基线；指针失效或指向非 confirmed 记录时视为没有基线。"""
    check_id = get_meta(conn, BASELINE_META_KEY)
    if not check_id:
        return None
    row = conn.execute(
        "SELECT * FROM checks WHERE check_id = ? AND status = ?",
        (check_id, CHECK_CONFIRMED),
    ).fetchone()
    return _row_payload(row) if row else None


def confirm_check(conn: sqlite3.Connection, check_id: str, now: str) -> dict:
    """把候选结论确认为新基线；只有 candidate 状态可以确认。"""
    row = conn.execute("SELECT * FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    if row is None:
        raise KeyError(check_id)
    if row["status"] == CHECK_CONFIRMED:
        return _row_payload(row)
    if row["status"] != CHECK_CANDIDATE:
        raise CheckStateError(
            f"体检记录 {check_id} 当前状态为 {row['status']}，只有完整成功的候选结论才能确认为基线"
        )
    cursor = conn.execute(
        "UPDATE checks SET status = ?, confirmed_at = ? WHERE check_id = ? AND status = ?",
        (CHECK_CONFIRMED, now, check_id, CHECK_CANDIDATE),
    )
    if not cursor.rowcount:
        raise CheckStateError(f"体检记录 {check_id} 已被并发确认，请刷新")
    set_meta(conn, BASELINE_META_KEY, check_id)
    return _row_payload(
        conn.execute("SELECT * FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    )


def list_checks(conn: sqlite3.Connection, limit: int = 10, include_failed: bool = False) -> list[dict]:
    where = "" if include_failed else "WHERE status != ?"
    params: list = [] if include_failed else [CHECK_FAILED]
    rows = conn.execute(
        "SELECT check_id, created_at, status, error, confirmed_at, payload FROM checks "
        f"{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        comparison = payload.get("comparison") or {
            "has_baseline": False,
            "baseline_check_id": None,
            "new_count": 0,
            "persistent_count": 0,
            "resolved_count": 0,
        }
        items.append(
            {
                "check_id": row["check_id"],
                "created_at": row["created_at"],
                "status": row["status"],
                "error": row["error"] or "",
                "confirmed_at": row["confirmed_at"] or "",
                "verdict": payload.get("verdict"),
                "stats": payload.get("stats", {}),
                "comparison": comparison,
            }
        )
    return items


def save_impact(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO impacts (impact_id, created_at, name, version, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            payload["impact_id"],
            payload["created_at"],
            payload["target"]["name"],
            payload["target"]["version"],
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def load_impact(conn: sqlite3.Connection, impact_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM impacts WHERE impact_id = ?", (impact_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_impact(conn: sqlite3.Connection, name: str | None = None) -> dict | None:
    if name:
        row = conn.execute(
            "SELECT payload FROM impacts WHERE name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM impacts ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]
