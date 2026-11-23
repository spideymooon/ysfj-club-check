"""
Playwright 持久化浏览器上下文 + 人工登录流程。

- 使用 launch_persistent_context，登录 Cookie 保存在 profile_dir（不进 Git）。
- 首次运行 login 打开有头浏览器，由用户手动完成手机号验证码登录。
- 登录检测采用「被动监听 URL」：不刷新页面、不干扰用户输入验证码。
- Docker 内通过 Xvfb + noVNC 提供同样的有头登录体验。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import db
from .config import Config, now_str
from .headers import safe_capture_from_request, save_capture

log = logging.getLogger("youzan.browser")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]


class BrowserSession:
    def __init__(self, cfg: Config, headless: bool | None = None):
        self.cfg = cfg
        self.headless = cfg.headless if headless is None else headless
        self._pw = None
        self.context: BrowserContext | None = None

    def start(self) -> BrowserContext:
        self.cfg.ensure_dirs()
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.cfg.profile_dir),
            headless=self.headless,
            args=LAUNCH_ARGS,
            locale="zh-CN",
            timezone_id=self.cfg.timezone,
            viewport={"width": 1280, "height": 900},
        )
        if self.cfg.browser_channel:
            kwargs["channel"] = self.cfg.browser_channel
        self.context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.context.set_default_timeout(self.cfg.request_timeout * 1000)
        return self.context

    def first_page(self) -> Page:
        assert self.context is not None
        return self.context.pages[0] if self.context.pages else self.context.new_page()

    def new_page(self) -> Page:
        assert self.context is not None
        return self.context.new_page()

    def user_agent(self) -> str:
        try:
            page = self.new_page()
            ua = page.evaluate("navigator.userAgent")
            page.close()
            return str(ua or "")
        except Exception:
            return ""

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self.context = None
            self._pw = None


def is_login_page(url: str) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if "passport" in host and "login" in path:
        return True
    if "login" in path and "ysjf.com" not in host:
        return True
    return False


# 未登录（游客）态实测只有这两个 Cookie
GUEST_COOKIE_NAMES = {"kdtsessionid", "nobody_sign"}


def _has_login_cookie(context: BrowserContext) -> bool:
    """Cookie Jar 中出现游客基线之外的站点 Cookie，视为已登录。"""
    try:
        for cookie in context.cookies():
            name = (cookie.get("name") or "").lower()
            domain = (cookie.get("domain") or "").lower()
            if ("ysjf.com" in domain or "youzan.com" in domain) \
                    and name not in GUEST_COOKIE_NAMES:
                return True
    except Exception:
        pass
    return False


def looks_logged_in(context: BrowserContext) -> bool:
    """
    登录完成判据（两路，任一命中即可）：
    1) 页面跳回目标站点且不再是登录页（覆盖正常重定向）；
    2) Cookie Jar 出现游客基线之外的站点 Cookie —— 覆盖登录成功后停在
       passport 中间页、跳到 youzan.com 其它域名、或用户另开标签页等情况。
    """
    for page in context.pages:
        try:
            url = page.url or ""
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if not host.endswith("ysjf.com"):
                continue
            if "login" in parts.path.lower():
                continue
            title = ""
            try:
                title = page.title() or ""
            except Exception:
                pass
            if "登录" in title:
                continue
            return True
        except Exception:
            continue
    return _has_login_cookie(context)


def do_login(cfg: Config, headless: bool = False) -> bool:
    """有头浏览器人工登录；成功后保存 Profile 并解除暂停状态。"""
    db.init_db(cfg)
    session = BrowserSession(cfg, headless=headless)
    captured: dict = {}
    try:
        context = session.start()
        page = session.first_page()

        def on_request(req):
            if "checkinV2.json" in req.url:
                captured.clear()
                captured.update(safe_capture_from_request(req.method, req.url, req.headers))

        page.on("request", on_request)

        print("=" * 68)
        print("  请在打开的浏览器中完成登录（手机号 + 短信验证码）")
        print("  登录成功后页面会跳回 club.ysjf.com，程序会自动检测并保存登录态。")
        print(f"  最长等待 {cfg.login_wait_seconds} 秒，期间请勿关闭浏览器窗口。")
        print("=" * 68)

        try:
            page.goto(cfg.result_page, wait_until="domcontentloaded",
                      timeout=cfg.request_timeout * 1000)
        except Exception as exc:
            log.warning("结果页加载异常（可继续手动登录）：%s", type(exc).__name__)

        if is_login_page(page.url):
            log.info("当前处于登录页，等待人工登录…")
        else:
            log.info("当前 URL 不在登录页，可能已存在有效登录态")

        deadline = time.time() + cfg.login_wait_seconds
        ok = False
        while time.time() < deadline:
            if looks_logged_in(context):
                # 再给页面一点时间完成跳转，避免登录过程中的中间态
                time.sleep(2)
                if looks_logged_in(context):
                    ok = True
                    break
            time.sleep(2)

        if not ok:
            log.error("等待登录超时，未检测到登录成功，Profile 可能仍为未登录状态")
            db.audit(cfg, "error", "login_timeout", "人工登录等待超时")
            return False

        if cfg.capture_enabled and captured:
            save_capture(cfg, captured)
            log.info("已保存真实签到请求模板：参数键=%s", list(captured.get("params", {})))

        db.set_state(cfg, "last_login_at", now_str(cfg))
        db.resume(cfg)
        db.audit(cfg, "info", "login_success", "人工登录完成，登录态已保存到持久化 Profile")
        print("[OK] 登录成功，登录态已保存。现在可以执行 python -m app.main once 进行单次签到测试。")
        return True
    except Exception as exc:
        log.exception("登录流程异常：%s", type(exc).__name__)
        db.audit(cfg, "error", "login_error", f"登录流程异常：{type(exc).__name__}")
        return False
    finally:
        session.close()


def do_probe(cfg: Config, headless: bool | None = None) -> dict:
    """
    抓取真实签到请求：打开已登录的结果页，观察页面实际发出的 checkinV2.json 请求。
    只观察、不主动发起签到请求；抓到的模板会被保存供后续复用。
    """
    db.init_db(cfg)
    session = BrowserSession(cfg, headless=headless)
    captured: dict = {}
    observed: list[str] = []
    try:
        context = session.start()
        page = session.first_page()

        def on_request(req):
            if "checkinV2.json" in req.url:
                captured.clear()
                captured.update(safe_capture_from_request(req.method, req.url, req.headers))
                observed.append(req.url.split("?")[0])

        page.on("request", on_request)
        try:
            page.goto(cfg.result_page, wait_until="domcontentloaded",
                      timeout=cfg.request_timeout * 1000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            log.warning("结果页加载异常：%s", type(exc).__name__)

        if is_login_page(page.url):
            db.audit(cfg, "warning", "probe_login_expired", "探测时发现登录态失效")
            print("[!] 当前被重定向到登录页，登录态已失效，请先执行 login")
            return {}

        if captured:
            path = save_capture(cfg, captured)
            print(f"[OK] 已捕获真实请求并保存到 {path}")
            print("     参数:", json_pretty(captured.get("params", {})))
            print("     请求头:", json_pretty(captured.get("headers", {})))
        else:
            print("[i] 结果页未自动发出 checkinV2.json 请求。")
            print("    这很正常：签到按钮可能在活动页触发。程序仍会以显式 XHR 方式调用，")
            print("    并使用实测补齐的 Header。可用 EXTRA_QUERY / EXTRA_HEADERS 手动补充参数。")
        db.audit(cfg, "info", "probe", f"探测完成，捕获请求数={len(observed)}")
        return captured
    finally:
        session.close()


def json_pretty(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)
