"""
单次签到编排。

流程：
  幂等检查 -> 启动持久化浏览器 -> 打开结果页(捕获真实请求) -> 前置查 isCheckin
  -> context.request 调用签到 API(自动携带 Profile Cookie) -> 响应判定
  -> 重试(仅网络类错误) -> 落库 -> 状态/通知

关键约束：
- Cookie 完全由浏览器上下文提供，代码里不出现任何 Cookie 字面量。
- 重复签到 / 登录失效 / 业务失败都是终态，绝不无限重试。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from . import db, notifier
from .browser import BrowserSession, is_login_page
from .classifier import Outcome, Verdict, classify, parse_activity
from .config import Config, now_str, today_str
from .headers import (
    activity_params, build, safe_capture_from_request, safe_url_for_log,
    save_capture,
)
from .logging_setup import scrub

log = logging.getLogger("youzan.checkin")


def _activity_precheck(cfg: Config, context: Any, params: dict,
                       headers: dict) -> bool | None:
    """返回 True=今日已签 / False=未签 / None=无法判定。"""
    try:
        q = activity_params(cfg, params)
        resp = context.request.get(
            cfg.activity_api, params=q, headers=headers,
            timeout=cfg.request_timeout * 1000,
        )
        state = parse_activity(
            resp.status, (resp.headers or {}).get("content-type", ""),
            resp.body(), resp.url,
        )
        if state.login_expired:
            log.info("前置查询提示登录失效，交由签到接口做权威判定")
            return None
        if state.is_checkin is None:
            log.info("前置查询未能判定：%s", state.message)
            return None
        log.info("前置查询：isCheckin=%s，连续签到天数=%s",
                 state.is_checkin, state.continues_day)
        return state.is_checkin
    except Exception as exc:
        log.warning("前置查询异常（忽略，继续签到）：%s", type(exc).__name__)
        return None


def _post_process(cfg: Config, verdict: Verdict, biz_date: str) -> None:
    if verdict.needs_relogin:
        # 暂停签到；只有「新进入暂停」或跨天才推送，避免重复轰炸
        newly = db.pause(cfg, verdict.message)
        reason = db.get_state(cfg, "pause_reason", "") or verdict.message
        if newly or db.get_state(cfg, "last_login_alert_date") != biz_date:
            notifier.notify_login_expired(cfg, reason, newly)
            db.set_state(cfg, "last_login_alert_date", biz_date)
        db.audit(cfg, "warning", "paused", "登录态失效，已暂停自动签到",
                 biz_date=biz_date, reason=reason)
        return

    if verdict.outcome in (Outcome.SUCCESS, Outcome.ALREADY):
        db.set_state(cfg, "last_success_date", biz_date)
        if verdict.outcome is Outcome.SUCCESS and cfg.notify_on_success:
            notifier.notify_result(cfg, "success", verdict.message, biz_date)
        if verdict.outcome is Outcome.ALREADY and cfg.notify_on_already:
            notifier.notify_result(cfg, "already", verdict.message, biz_date)
        return

    if verdict.outcome in (Outcome.FAILED, Outcome.ERROR) and cfg.notify_on_failure:
        notifier.notify_result(cfg, verdict.outcome.value, verdict.message, biz_date)


def run_once(
    cfg: Config,
    *,
    force: bool = False,
    session_factory: Callable[[Config], Any] | None = None,
) -> Verdict:
    """执行一轮签到。session_factory 供离线测试注入假浏览器。"""
    db.init_db(cfg)
    factory = session_factory or (lambda c: BrowserSession(c))
    biz_date = today_str(cfg)
    started = now_str(cfg)

    # --- 幂等 & 暂停保护 ---
    if db.is_paused(cfg) and not force:
        msg = "登录态失效，自动签到已暂停；请重新登录后自动恢复"
        db.audit(cfg, "warning", "skipped_paused", msg, biz_date=biz_date)
        return Verdict(Outcome.SKIPPED_PAUSED, message=msg)
    if not force and db.today_has_terminal(cfg, biz_date):
        msg = "今日已完成签到（幂等保护），跳过本次请求"
        db.audit(cfg, "info", "skipped_done", msg, biz_date=biz_date)
        return Verdict(Outcome.SKIPPED_DONE, message=msg)

    session = factory(cfg)
    captured: dict = {}
    verdict: Verdict | None = None
    http_status: int | None = None
    req_url_log = ""
    retry_count = 0

    try:
        context = session.start()
        page = session.new_page()

        # 监听页面真实发出的签到请求（用于补充参数 / Header）
        if cfg.capture_enabled:
            def on_request(req):
                if "checkinV2.json" in req.url:
                    captured.clear()
                    captured.update(
                        safe_capture_from_request(req.method, req.url, req.headers)
                    )
            page.on("request", on_request)

        # 打开结果页：建立同源来源，浏览器自动附带完整登录 Cookie
        try:
            page.goto(cfg.result_page, wait_until="domcontentloaded",
                      timeout=cfg.request_timeout * 1000)
            page.wait_for_timeout(1500)
            if is_login_page(page.url):
                log.warning("结果页被重定向到登录页，登录态可能已失效（以 API 返回为准）")
        except Exception as exc:
            log.warning("结果页加载失败（继续尝试 API）：%s", type(exc).__name__)

        if cfg.capture_enabled and captured:
            save_capture(cfg, captured)
            db.set_state(cfg, "captured_at", now_str(cfg))
            log.info("已合并真实请求模板：参数=%s 请求头=%s",
                     list(captured.get("params", {})),
                     list(captured.get("headers", {})))

        ua = session.user_agent()
        api_url, params, headers = build(cfg, user_agent=ua, capture=captured or None)
        req_url_log = safe_url_for_log(api_url, params)
        log.info("签到请求：%s", req_url_log)
        log.info("请求头键：%s（Cookie 由浏览器上下文自动携带）", list(headers))

        # --- 前置检查：今日已签则直接跳过，不发签到请求 ---
        if cfg.precheck_enabled and not force:
            pre = _activity_precheck(cfg, context, params, headers)
            if pre is True:
                verdict = Verdict(
                    Outcome.ALREADY,
                    message="前置检查 data.isCheckin=true，今日已签到",
                    detail={"precheck": True},
                )
                log.info("前置检查命中：今日已签到，跳过签到请求")

        # --- 调用签到 API（仅网络/解析类错误重试）---
        if verdict is None:
            for attempt in range(1, max(1, cfg.max_retry) + 1):
                retry_count = attempt - 1
                try:
                    resp = context.request.get(
                        api_url, params=params, headers=headers,
                        timeout=cfg.request_timeout * 1000,
                    )
                    http_status = resp.status
                    ctype = (resp.headers or {}).get("content-type", "")
                    body = resp.body()
                    verdict = classify(http_status, ctype, body, final_url=resp.url)
                    log.info("响应：HTTP %s | code=%s | 判定=%s | msg=%s",
                             http_status, verdict.code, verdict.outcome.value,
                             scrub(verdict.message, cfg))
                    log.debug("响应体（已脱敏）：%s", scrub(verdict.raw_excerpt, cfg))
                    if not verdict.retryable:
                        break
                except Exception as exc:
                    verdict = Verdict(
                        Outcome.ERROR,
                        message=f"签到请求异常：{type(exc).__name__}",
                        detail={"attempt": attempt},
                    )
                    log.warning("第 %s/%s 次请求异常：%s",
                                attempt, cfg.max_retry, type(exc).__name__)
                if attempt < cfg.max_retry:
                    time.sleep(cfg.retry_backoff)
    except Exception as exc:
        verdict = Verdict(
            Outcome.ERROR,
            message=f"签到流程异常：{type(exc).__name__}",
        )
        log.exception("签到流程异常")
        db.audit(cfg, "error", "exception", verdict.message, biz_date=biz_date)
    finally:
        session.close()

    if verdict is None:
        verdict = Verdict(Outcome.ERROR, message="未获得有效签到结果")

    # --- 落库 ---
    db.add_record(
        cfg,
        biz_date=biz_date,
        status=verdict.outcome.value,
        http_status=http_status,
        resp_code=str(verdict.code) if verdict.code is not None else None,
        message=verdict.message,
        request_url=req_url_log,
        raw_excerpt=verdict.raw_excerpt,
        retry_count=retry_count,
        started_at=started,
        finished_at=now_str(cfg),
    )
    db.audit(cfg, "info", "checkin_result",
             f"签到结果={verdict.outcome.value}：{verdict.message}",
             biz_date=biz_date, code=verdict.code)

    _post_process(cfg, verdict, biz_date)
    return verdict
