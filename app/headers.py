"""
请求构造：基于实测补充必要 Header，并支持从真实页面请求中捕获合并额外参数。

要点：
- Cookie 永远不由我们手工设置；context.request 与浏览器上下文共享 Cookie Jar，
  自动携带持久化 Profile 中的登录态。
- 抓包得到的 Cookie / Authorization / token 等敏感项一律剔除后才落盘。
- 用户已确认的参数只是基线，真实请求里出现的额外参数会被自动合并。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .config import Config
from .logging_setup import scrub

log = logging.getLogger("youzan.headers")

# 这些参数/请求头绝不允许从抓包结果里固化下来（凭证类）
SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "refresh_token", "session", "sid", "signature",
    "sign", "code", "secret", "password", "openid", "kdt_session_id",
}
SENSITIVE_HEADER_KEYS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "content-length", "host", "connection", "x-yz-trace", "x-rontgen",
}

# 实测：PC 网页版签到是 XHR，需要 Referer / X-Requested-With / Accept。
# User-Agent 从浏览器 navigator.userAgent 实时读取，保证与登录态 Profile 一致。
BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def default_headers(cfg: Config, user_agent: str = "") -> dict:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = cfg.result_page
    headers["Origin"] = cfg.base_url
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def base_params(cfg: Config) -> dict:
    """用户已确认的两个参数。实测 checkinId 为必需，kdt_id 保留。"""
    return {"checkinId": str(cfg.checkin_id), "kdt_id": str(cfg.kdt_id)}


def safe_capture_from_request(method: str, url: str, headers: dict) -> dict:
    """把一次真实请求转成可持久化模板（脱敏、剔除凭证）。"""
    parts = urlsplit(url)
    params = {
        k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in SENSITIVE_QUERY_KEYS
    }
    safe_headers = {
        k: v for k, v in (headers or {}).items()
        if k.lower() not in SENSITIVE_HEADER_KEYS
    }
    return {
        "method": method,
        "path": parts.path,
        "params": params,
        "headers": safe_headers,
    }


def save_capture(cfg: Config, capture: dict) -> Path:
    cfg.capture_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "method": capture.get("method", "GET"),
        "path": capture.get("path", ""),
        "params": {
            str(k): str(v) for k, v in (capture.get("params") or {}).items()
            if str(k).lower() not in SENSITIVE_QUERY_KEYS
        },
        "headers": {
            str(k): str(v) for k, v in (capture.get("headers") or {}).items()
            if str(k).lower() not in SENSITIVE_HEADER_KEYS
        },
        "captured_at": cfg.capture_path.stat().st_mtime if cfg.capture_path.exists() else None,
    }
    # 二次脱敏，防止抓包里混进完整 Cookie 串
    text = scrub(json.dumps(payload, ensure_ascii=False, indent=2), cfg)
    cfg.capture_path.write_text(text, encoding="utf-8")
    return cfg.capture_path


def load_capture(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.capture_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build(
    cfg: Config,
    user_agent: str = "",
    capture: dict | None = None,
) -> tuple[str, dict, dict]:
    """
    构造 (url, params, headers)。

    合并优先级：用户确认基线 < 真实抓包补充 < EXTRA_QUERY / EXTRA_HEADERS 显式配置。
    任何凭证类键值都不会进入结果。
    """
    cap = capture if capture is not None else load_capture(cfg)

    params = base_params(cfg)
    for key, value in (cap.get("params") or {}).items():
        if str(key).lower() not in SENSITIVE_QUERY_KEYS and key not in params:
            params[str(key)] = str(value)
    for key, value in cfg.extra_query.items():
        if str(key).lower() not in SENSITIVE_QUERY_KEYS:
            params[str(key)] = str(value)

    headers = default_headers(cfg, user_agent)
    for key, value in (cap.get("headers") or {}).items():
        if str(key).lower() not in SENSITIVE_HEADER_KEYS:
            headers[str(key)] = str(value)
    for key, value in cfg.extra_headers.items():
        if str(key).lower() not in SENSITIVE_HEADER_KEYS:
            headers[str(key)] = str(value)

    # 兜底：确保关键头存在且不被抓包里的空值覆盖
    for key, value in BASE_HEADERS.items():
        if not headers.get(key):
            headers[key] = value
    headers.setdefault("Referer", cfg.result_page)
    headers.setdefault("Origin", cfg.base_url)
    # 最后一道保险：绝不手工带 Cookie
    for key in list(headers):
        if key.lower() in SENSITIVE_HEADER_KEYS:
            headers.pop(key, None)

    return cfg.checkin_api, params, headers


def activity_params(cfg: Config, captured_params: dict | None = None) -> dict:
    params = {"checkinId": str(cfg.checkin_id), "kdt_id": str(cfg.kdt_id)}
    for key, value in (captured_params or {}).items():
        if str(key).lower() not in SENSITIVE_QUERY_KEYS and key not in params:
            params[str(key)] = str(value)
    for key, value in cfg.extra_query.items():
        if str(key).lower() not in SENSITIVE_QUERY_KEYS:
            params[str(key)] = str(value)
    return params


def safe_url_for_log(url: str, params: dict) -> str:
    """生成可安全写入日志的 URL（不含任何凭证）。"""
    pairs = []
    for key, value in params.items():
        shown = "***" if str(key).lower() in SENSITIVE_QUERY_KEYS else str(value)
        pairs.append(f"{key}={shown}")
    return f"{url}?{'&'.join(pairs)}" if pairs else url
