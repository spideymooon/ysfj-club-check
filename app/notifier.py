"""
PushPlus 微信通知。

Token 只从环境变量 / .env 读取；请求体、响应体、异常信息一律不进入日志，
避免凭证回显。通知失败不影响签到主流程。
"""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config, now_str

log = logging.getLogger("youzan.notify")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def send(cfg: Config, title: str, content: str, *, force: bool = False) -> bool:
    if not cfg.pushplus_enabled and not force:
        return False
    if not cfg.pushplus_token:
        log.warning("PushPlus 未配置 PUSHPLUS_TOKEN，跳过通知")
        return False

    payload = {
        "token": cfg.pushplus_token,
        "title": title,
        "content": content,
        "template": cfg.pushplus_template,
        "channel": cfg.pushplus_channel,
    }
    if cfg.pushplus_topic:
        payload["topic"] = cfg.pushplus_topic

    req = Request(
        PUSHPLUS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
        # 只解析 code，不打印响应体（可能回显 token）
        try:
            result = json.loads(raw)
            ok = str(result.get("code")) == "200"
        except Exception:
            ok = False
        log.info("PushPlus 推送%s", "成功" if ok else "返回非 200，请检查 token / 实名认证")
        return ok
    except (HTTPError, URLError, OSError) as exc:
        log.warning("PushPlus 推送失败：%s", type(exc).__name__)
        return False
    except Exception as exc:
        log.warning("PushPlus 推送异常：%s", type(exc).__name__)
        return False


def _html(lines: list[str]) -> str:
    return "<br>".join(lines)


def notify_result(cfg: Config, outcome: str, message: str, biz_date: str) -> None:
    titles = {
        "success": "有赞签到成功",
        "already": "有赞签到：今日已签到",
        "failed": "有赞签到失败",
        "error": "有赞签到异常",
    }
    send(
        cfg,
        f"【{titles.get(outcome, '有赞签到')}】{biz_date}",
        _html([
            f"<b>日期</b>：{biz_date}（北京时间）",
            f"<b>结果</b>：{outcome}",
            f"<b>说明</b>：{message}",
            f"<b>时间</b>：{now_str(cfg)}",
        ]),
    )


def notify_login_expired(cfg: Config, reason: str, newly_paused: bool) -> None:
    title = "【需要重新登录】有赞签到已暂停" if newly_paused else "【提醒】有赞签到仍处于暂停"
    send(
        cfg,
        title,
        _html([
            "<b>登录态已失效，自动签到已暂停，不会重复重试。</b>",
            f"<b>原因</b>：{reason}",
            f"<b>时间</b>：{now_str(cfg)}",
            "<b>处理方式</b>：",
            cfg.login_hint,
            "重新登录成功后会自动恢复每日 08:00 签到。",
        ]),
    )


def notify_paused_reminder(cfg: Config) -> None:
    """仍处于暂停状态时每天最多提醒一次，避免告警疲劳。"""
    from . import db
    from .config import today_str

    today = today_str(cfg)
    if db.get_state(cfg, "last_paused_reminder_date") == today:
        return
    send(
        cfg,
        "【提醒】有赞签到仍处暂停，等待重新登录",
        _html([
            f"<b>日期</b>：{today}",
            "登录态尚未恢复，今日 08:00 签到已跳过。",
            cfg.login_hint,
        ]),
    )
    db.set_state(cfg, "last_paused_reminder_date", today)
