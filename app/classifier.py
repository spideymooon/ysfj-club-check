"""
签到响应判定（纯函数，无 I/O，便于离线单测）。

实测依据（2026-11-23，未登录态）：
  GET checkinV2.json -> 200 application/json
  {"code":1000000002,"msg":"userId must be greater than or equal to 1 "}
  缺 checkinId -> {"code":12345,"msg":"缺少必要的参数 - checkinId"}
  get_activity_by_yzuid_v2.json -> {"code":0,"data":{"isCheckin":false,...}}
页面未登录时才 302 到 passport.youzan.com/login（手机号验证码登录）。

用户确认的成功响应：
  code == 0 且 data.success == true 且 data.list[0].isSuccess == true
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    SUCCESS = "success"
    ALREADY = "already"
    LOGIN_EXPIRED = "login_expired"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED_DONE = "skipped_done"
    SKIPPED_PAUSED = "skipped_paused"


@dataclass
class Verdict:
    outcome: Outcome
    code: Any = None
    message: str = ""
    detail: dict = field(default_factory=dict)
    raw_excerpt: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (
            Outcome.SUCCESS, Outcome.ALREADY,
            Outcome.SKIPPED_DONE, Outcome.SKIPPED_PAUSED,
        )

    @property
    def needs_relogin(self) -> bool:
        return self.outcome is Outcome.LOGIN_EXPIRED

    @property
    def retryable(self) -> bool:
        """仅网络/解析类错误允许重试；业务失败、重复签到、登录失效都不重试。"""
        return self.outcome is Outcome.ERROR


@dataclass
class ActivityState:
    """前置查询接口解析结果。"""
    is_checkin: bool | None = None
    login_expired: bool = False
    continues_day: int | None = None
    message: str = ""


# 明确的登录/鉴权失败业务码（1000000002 为实测）
AUTH_CODES = {
    1000000001, 1000000002, 1000000003,
    401, 403, 10000, 20001, 100001,
}
AUTH_KEYWORDS = (
    "userid must be greater", "未登录", "请先登录", "重新登录", "登录已失效",
    "登录失效", "用户未登录", "请先授权", "not login", "unauthorized",
    "login required", "invalid session", "session expired", "please login",
)
ALREADY_KEYWORDS = (
    "已签到", "今日已签", "已经签到", "重复签到", "已打卡", "今日已打卡",
    "明天再来", "明日再来", "请勿重复", "已领取", "重复提交",
    "already", "repeat", "duplicate", "has signed", "checked in today",
    "signed today",
)
# 常见的「今日已签」业务码，可用环境变量 EXTRA_ALREADY_CODES 扩展
DEFAULT_ALREADY_CODES: set[int] = set()


def _as_int(code: Any) -> int | None:
    if code is None or isinstance(code, bool):
        return None
    try:
        return int(str(code).strip())
    except (ValueError, TypeError):
        return None


def _truthy(value: Any) -> bool | None:
    """返回 True / False / 无法判断(None)。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y", "success"):
            return True
        if low in ("false", "0", "no", "n", "fail", "failed"):
            return False
    return None


def _get_list0(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    lst = data.get("list")
    if isinstance(lst, list) and lst and isinstance(lst[0], dict):
        return lst[0]
    return {}


def _reason_text(item: dict) -> str:
    keys = ("msg", "message", "reason", "failReason", "errorMsg", "desc",
            "remark", "tip", "errorMsg", "subMsg")
    return " ".join(str(item[k]) for k in keys if k in item and item[k] is not None)


def classify(
    http_status: int,
    content_type: str,
    body: str | bytes,
    final_url: str = "",
    already_codes: set[int] | None = None,
) -> Verdict:
    """把一次签到 HTTP 响应归类为 SUCCESS / ALREADY / LOGIN_EXPIRED / FAILED / ERROR。"""
    codes = set(DEFAULT_ALREADY_CODES) | set(already_codes or ())
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    excerpt = text[:600]
    ctype = (content_type or "").lower()
    low_url = (final_url or "").lower()

    # --- 1) URL / 状态码层面的登录跳转 ---
    if http_status in (401, 403) or "passport.youzan.com/login" in low_url:
        return Verdict(
            Outcome.LOGIN_EXPIRED, message="请求被重定向到登录页或返回未授权",
            detail={"http_status": http_status}, raw_excerpt=excerpt,
        )

    # --- 2) 非 JSON 响应：多半是被登录页 / 网关 HTML 拦截 ---
    if "json" not in ctype:
        if any(k in text.lower() for k in ("passport", "验证码登录", "请登录", "<title>登录", "login")):
            return Verdict(
                Outcome.LOGIN_EXPIRED, message="响应为登录页 HTML，登录态已失效",
                detail={"http_status": http_status, "content_type": content_type},
                raw_excerpt=excerpt,
            )
        return Verdict(
            Outcome.ERROR, message=f"响应不是 JSON (content-type={content_type})",
            detail={"http_status": http_status}, raw_excerpt=excerpt,
        )

    # --- 3) JSON 解析 ---
    try:
        obj = json.loads(text)
    except Exception:
        return Verdict(
            Outcome.ERROR, message="响应 JSON 解析失败",
            detail={"http_status": http_status}, raw_excerpt=excerpt,
        )
    if not isinstance(obj, dict):
        return Verdict(
            Outcome.ERROR, message="响应 JSON 顶层不是对象",
            detail={"http_status": http_status}, raw_excerpt=excerpt,
        )

    code = obj.get("code")
    code_int = _as_int(code)
    msg = str(obj.get("msg") or obj.get("message") or "")
    data = obj.get("data")
    flat_low = json.dumps(obj, ensure_ascii=False, default=str).lower()

    # --- 4) 登录失效（最高优先级，实测 code=1000000002）---
    auth_hit = code_int in AUTH_CODES or any(k in flat_low for k in AUTH_KEYWORDS)
    if auth_hit:
        return Verdict(
            Outcome.LOGIN_EXPIRED, code=code, message=msg or "登录态失效",
            detail={"auth_signal": True, "http_status": http_status},
            raw_excerpt=excerpt,
        )
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str) and ("passport" in value.lower() or "loginurl" in value.lower()):
                return Verdict(
                    Outcome.LOGIN_EXPIRED, code=code, message=msg or "响应要求跳转登录",
                    detail={"login_url_in_data": True}, raw_excerpt=excerpt,
                )

    # --- 5) 提取业务标志 ---
    success_flag: Any = None
    is_checkin: Any = None
    if isinstance(data, dict):
        success_flag = data.get("success")
        is_checkin = data.get("isCheckin")
    item0 = _get_list0(data)
    item_success = item0.get("isSuccess")

    # --- 6) 明确成功：用户确认的判定式 ---
    if code_int == 0:
        if _truthy(item_success) is True:
            return Verdict(
                Outcome.SUCCESS, code=code, message=msg or "签到成功",
                detail={"isSuccess": True, "http_status": http_status},
                raw_excerpt=excerpt,
            )
        if _truthy(success_flag) is True and _truthy(item_success) is not False:
            return Verdict(
                Outcome.SUCCESS, code=code, message=msg or "签到成功",
                detail={"data.success": True, "http_status": http_status},
                raw_excerpt=excerpt,
            )

    # --- 7) 重复签到：归入「已完成」，绝不重试 ---
    dup_reason = _reason_text(item0)
    dup_hit = (
        code_int in codes
        or _truthy(is_checkin) is True
        or any(k in flat_low for k in ALREADY_KEYWORDS)
        or any(k in dup_reason.lower() for k in ALREADY_KEYWORDS)
        or (_truthy(item_success) is False and code_int == 0 and dup_reason)
    )
    if dup_hit:
        return Verdict(
            Outcome.ALREADY, code=code,
            message=msg or dup_reason or "今日已签到，无需重复签到",
            detail={"already_signal": True, "http_status": http_status},
            raw_excerpt=excerpt,
        )

    # --- 8) code=0 但 list[0].isSuccess 明确为 false ---
    if code_int == 0 and _truthy(item_success) is False:
        return Verdict(
            Outcome.FAILED, code=code,
            message=msg or dup_reason or "签到未成功（isSuccess=false）",
            detail={"isSuccess": False, "http_status": http_status},
            raw_excerpt=excerpt,
        )

    # --- 9) code=0 且无错误信号：兜底视为成功 ---
    if code_int == 0:
        return Verdict(
            Outcome.SUCCESS, code=code, message=msg or "签到完成（code=0）",
            detail={"fallback_code0": True, "http_status": http_status},
            raw_excerpt=excerpt,
        )

    # --- 10) 其它业务失败 ---
    return Verdict(
        Outcome.FAILED, code=code, message=msg or f"签到失败 (code={code})",
        detail={"http_status": http_status}, raw_excerpt=excerpt,
    )


def parse_activity(
    http_status: int,
    content_type: str,
    body: str | bytes,
    final_url: str = "",
) -> ActivityState:
    """解析前置查询接口，返回今日是否已签到。"""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    low_url = (final_url or "").lower()
    if http_status in (401, 403) or "passport.youzan.com/login" in low_url:
        return ActivityState(login_expired=True, message="查询接口被重定向到登录页")
    if "json" not in (content_type or "").lower():
        return ActivityState(message="查询接口返回非 JSON")
    try:
        obj = json.loads(text)
    except Exception:
        return ActivityState(message="查询接口 JSON 解析失败")
    if not isinstance(obj, dict):
        return ActivityState(message="查询接口 JSON 结构异常")

    code_int = _as_int(obj.get("code"))
    flat_low = json.dumps(obj, ensure_ascii=False, default=str).lower()
    if code_int in AUTH_CODES or any(k in flat_low for k in AUTH_KEYWORDS):
        return ActivityState(login_expired=True, message="查询接口判定登录失效")

    data = obj.get("data")
    if not isinstance(data, dict):
        return ActivityState(message=str(obj.get("msg") or "查询接口无 data"))
    is_checkin = _truthy(data.get("isCheckin"))
    day = data.get("continuesDay")
    try:
        day = int(day) if day is not None else None
    except (ValueError, TypeError):
        day = None
    return ActivityState(
        is_checkin=is_checkin,
        continues_day=day,
        message=str(obj.get("msg") or ""),
    )
