"""
SQLite：签到记录 / 运行日志 / 应用状态。

单进程内共享一个连接（check_same_thread=False + 可重入锁），兼容 APScheduler
线程任务。所有写入 DB 的文本都先经 scrub() 脱敏。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from .config import Config, now_str
from .logging_setup import scrub

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_CONN_PATH: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date      TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    http_status   INTEGER,
    resp_code     TEXT,
    message       TEXT,
    request_url   TEXT,
    raw_excerpt   TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_records_date ON checkin_records(biz_date);

CREATE TABLE IF NOT EXISTS run_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    level      TEXT,
    event      TEXT,
    message    TEXT,
    context    TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON run_logs(ts);

CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

# 这些状态代表「今天已经处理完，无需再发签到请求」
TERMINAL_STATUSES = ("success", "already", "skipped_done")


def init_db(cfg: Config) -> sqlite3.Connection:
    global _CONN, _CONN_PATH
    with _LOCK:
        target = str(cfg.db_path)
        if _CONN is not None and _CONN_PATH == target:
            return _CONN
        # 配置切换（例如测试用临时库）时关闭旧连接再重建
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
        cfg.ensure_dirs()
        conn = sqlite3.connect(target, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _CONN = conn
        _CONN_PATH = target
        return conn


def _conn(cfg: Config) -> sqlite3.Connection:
    return init_db(cfg)


def close_db() -> None:
    """显式关闭当前连接（测试收尾用；运行期一般无需调用）。"""
    global _CONN, _CONN_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
        _CONN_PATH = None


def add_record(cfg: Config, **kwargs: Any) -> int:
    columns = (
        "biz_date", "status", "http_status", "resp_code", "message",
        "request_url", "raw_excerpt", "retry_count", "started_at", "finished_at",
    )
    values = []
    for col in columns:
        val = kwargs.get(col)
        if col in ("message", "request_url", "raw_excerpt") and val is not None:
            val = scrub(str(val), cfg)
        values.append(val)
    with _LOCK:
        cur = _conn(cfg).execute(
            f"INSERT INTO checkin_records ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            values,
        )
        _conn(cfg).commit()
        return int(cur.lastrowid)


def today_has_terminal(cfg: Config, biz_date: str) -> bool:
    """幂等保护：今天是否已经有成功/已签/跳过记录。"""
    marks = ",".join("?" for _ in TERMINAL_STATUSES)
    with _LOCK:
        row = _conn(cfg).execute(
            f"SELECT 1 FROM checkin_records WHERE biz_date=? AND status IN ({marks}) LIMIT 1",
            (biz_date, *TERMINAL_STATUSES),
        ).fetchone()
    return row is not None


def recent_records(cfg: Config, limit: int = 10) -> list[dict]:
    with _LOCK:
        rows = _conn(cfg).execute(
            "SELECT * FROM checkin_records ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def insert_run_log(cfg: Config, level: str, event: str, message: str,
                   context: dict | None = None) -> None:
    try:
        ctx = json.dumps(context or {}, ensure_ascii=False, default=str)
    except Exception:
        ctx = "{}"
    with _LOCK:
        _conn(cfg).execute(
            "INSERT INTO run_logs (ts, level, event, message, context) VALUES (?,?,?,?,?)",
            (now_str(cfg), level.upper(), event, scrub(message, cfg), scrub(ctx, cfg)),
        )
        _conn(cfg).commit()


def audit(cfg: Config, level: str, event: str, message: str, **context: Any) -> None:
    """同时写结构化运行日志与文本日志。"""
    lvl = {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level.lower(), 20)
    logging.getLogger("youzan").log(lvl, "[%s] %s", event, message)
    insert_run_log(cfg, level, event, message, context)


def get_state(cfg: Config, key: str, default: str | None = None) -> str | None:
    with _LOCK:
        row = _conn(cfg).execute(
            "SELECT value FROM app_state WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_state(cfg: Config, key: str, value: str) -> None:
    with _LOCK:
        _conn(cfg).execute(
            "INSERT INTO app_state (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_str(cfg)),
        )
        _conn(cfg).commit()


def is_paused(cfg: Config) -> bool:
    return (get_state(cfg, "paused", "0") or "0") == "1"


def pause(cfg: Config, reason: str) -> bool:
    """返回是否为「本次新进入暂停」（用于避免重复告警）。"""
    newly = not is_paused(cfg)
    set_state(cfg, "paused", "1")
    set_state(cfg, "pause_reason", scrub(reason, cfg))
    set_state(cfg, "paused_at", now_str(cfg))
    return newly


def resume(cfg: Config) -> None:
    set_state(cfg, "paused", "0")
    set_state(cfg, "pause_reason", "")
    set_state(cfg, "last_login_alert_date", "")


def health(cfg: Config) -> bool:
    try:
        with _LOCK:
            _conn(cfg).execute("SELECT 1 FROM app_state LIMIT 1").fetchone()
        return True
    except Exception:
        return False
