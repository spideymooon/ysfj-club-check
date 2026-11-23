"""
本机网页控制台。

功能：
- 状态面板：运行状态 / 下次执行 / 上次登录 / 上次成功 / 最近签到记录
- 配置表单：PushPlus Token（脱敏显示）、签到时间、签到活动参数、通知开关 → 保存进 .env
- 一键操作：立即签到 / 打开登录窗口（有头浏览器人工登录）/ 解除暂停 / 测试推送
- 日志尾部查看（日志本身经过脱敏过滤）

安全设计：
- 默认只绑定 127.0.0.1，仅本机可访问；如需局域网访问改 WEB_HOST 并自担风险
- Token 在 GET /api/config 中只返回脱敏形式，明文只在 POST 保存时短暂出现
- 所有接口不打印请求体、不写敏感值进日志
- 仅使用 Python 标准库，无新增依赖
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, notifier
from .browser import do_login
from .checkin import run_once
from .config import Config, now_str
from .scheduler import daily_job, make_trigger

log = logging.getLogger("youzan.web")

# 允许网页保存的 .env 键白名单（绝不包含 Cookie/密码类）
EDITABLE_KEYS = {
    "PUSHPLUS_TOKEN", "PUSHPLUS_TOPIC", "PUSHPLUS_CHANNEL",
    "NOTIFY_ON_SUCCESS", "NOTIFY_ON_ALREADY", "NOTIFY_ON_FAILURE",
    "SCHEDULE_HOUR", "SCHEDULE_MINUTE",
    "CHECKIN_ID", "KDT_ID", "RUN_ON_STARTUP", "LOGIN_WAIT_SECONDS",
}

# 前端字段名 → .env 键 的固定映射（只接受这些字段）
FIELD_MAP = {
    "pushplus_token": "PUSHPLUS_TOKEN",
    "pushplus_topic": "PUSHPLUS_TOPIC",
    "pushplus_channel": "PUSHPLUS_CHANNEL",
    "schedule_hour": "SCHEDULE_HOUR",
    "schedule_minute": "SCHEDULE_MINUTE",
    "login_wait_seconds": "LOGIN_WAIT_SECONDS",
    "notify_on_success": "NOTIFY_ON_SUCCESS",
    "notify_on_already": "NOTIFY_ON_ALREADY",
    "notify_on_failure": "NOTIFY_ON_FAILURE",
    "run_on_startup": "RUN_ON_STARTUP",
    "checkin_id": "CHECKIN_ID",
    "kdt_id": "KDT_ID",
}

_BOOL_KEYS = {"NOTIFY_ON_SUCCESS", "NOTIFY_ON_ALREADY", "NOTIFY_ON_FAILURE",
              "RUN_ON_STARTUP"}
_INT_KEYS = {"SCHEDULE_HOUR", "SCHEDULE_MINUTE", "LOGIN_WAIT_SECONDS"}

_MASK = "__unchanged__"  # 前端表示「不修改该字段」的哨兵值


# ---------------------------------------------------------------------------
# .env 读写（保留注释与未列出键，只更新白名单键）
# ---------------------------------------------------------------------------

def _env_path(cfg: Config) -> Path:
    return cfg.data_dir.parent / ".env"


def update_env(cfg: Config, updates: dict[str, str]) -> list[str]:
    """把 updates 写入 .env（保留其余行），同时刷新 os.environ。"""
    import os

    path = _env_path(cfg)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    changed: list[str] = []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                if key != "PUSHPLUS_TOKEN":
                    changed.append(key)
                else:
                    changed.append("PUSHPLUS_TOKEN")
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
            changed.append(key)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

    for key, value in updates.items():
        os.environ[key] = value
    return changed


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:3] + "*" * 6 + token[-4:]


# ---------------------------------------------------------------------------
# 后台动作状态（同一时刻只允许一个动作，避免并发操作浏览器 Profile）
# ---------------------------------------------------------------------------

class ActionRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self.state: dict | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, name: str, fn) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self.state = {"name": name, "status": "running",
                      "started_at": now_str(Config.load()), "result": None}

        def _wrap():
            ok, message = False, ""
            try:
                ok, message = fn()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                log.exception("网页操作 %s 异常", name)
            finally:
                self.state = {"name": name, "status": "done",
                              "started_at": self.state["started_at"] if self.state else "",
                              "result": {"ok": bool(ok), "message": str(message)}}
                with self._lock:
                    self._busy = False

        threading.Thread(target=_wrap, name=f"web-action-{name}", daemon=True).start()
        return True


RUNNER = ActionRunner()


def _action_checkin():
    cfg = Config.load()
    verdict = run_once(cfg)
    return (verdict.ok or verdict.outcome.value == "already",
            f"结果：{verdict.outcome.value} | code={verdict.code} | {verdict.message}")


def _action_login():
    cfg = Config.load()
    ok = do_login(cfg, headless=False)
    return (ok, "登录成功，登录态已保存" if ok else "未检测到登录成功（超时或取消），可重试")


def _action_resume():
    cfg = Config.load()
    db.init_db(cfg)
    db.resume(cfg)
    db.audit(cfg, "info", "resume", "网页控制台：手动解除登录失效暂停")
    return True, "已解除暂停，建议点「立即签到」验证登录态"


def _action_notify_test():
    cfg = Config.load()
    ok = notifier.send(
        cfg, "【测试】有赞签到通知",
        "这是一条来自有赞自动签到网页控制台的 PushPlus 测试消息。<br>收到即说明通知配置正确。",
        force=True,
    )
    return ok, "推送成功，请查看微信" if ok else "推送失败：请检查 Token 是否正确 / 已实名认证"


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "YouzanCheckin/1.0"

    # -- 基础工具 ----------------------------------------------------------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s %s", self.command, self.path)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 8192:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- 路由 --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            self._html(PAGE)
        elif path == "/api/status":
            self._json(self._build_status())
        elif path == "/api/config":
            self._json(self._build_config())
        elif path == "/api/logs":
            self._json(self._build_logs())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        data = self._read_json()
        if path == "/api/config":
            self._post_config(data)
        elif path == "/api/action":
            self._post_action(data)
        else:
            self._json({"error": "not found"}, 404)

    # -- GET 构造 ----------------------------------------------------------
    def _build_status(self) -> dict:
        cfg = Config.load()
        db.init_db(cfg)
        next_run = None
        job = getattr(self.server, "aps_job", None)
        scheduler = getattr(self.server, "aps_scheduler", None)
        if scheduler is not None:
            try:
                job = scheduler.get_job("daily_checkin")
                if job is not None and job.next_run_time is not None:
                    next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                next_run = None
        if next_run is None:
            try:
                fire = make_trigger(cfg).get_next_fire_time(None, __import__("datetime").datetime.now(cfg.tz))
                next_run = fire.strftime("%Y-%m-%d %H:%M:%S") if fire else None
            except Exception:
                next_run = None
        records = [
            {"biz_date": r["biz_date"], "status": r["status"],
             "code": r["resp_code"], "finished_at": r["finished_at"],
             "message": r["message"]}
            for r in db.recent_records(cfg, 10)
        ]
        return {
            "now": now_str(cfg),
            "paused": db.is_paused(cfg),
            "pause_reason": db.get_state(cfg, "pause_reason", ""),
            "paused_at": db.get_state(cfg, "paused_at", ""),
            "last_login_at": db.get_state(cfg, "last_login_at", "从未"),
            "last_success_date": db.get_state(cfg, "last_success_date", "从未"),
            "schedule": f"{cfg.schedule_hour:02d}:{cfg.schedule_minute:02d}",
            "next_run": next_run,
            "run_on_startup": cfg.run_on_startup,
            "pushplus_configured": bool(cfg.pushplus_token),
            "records": records,
            "action": RUNNER.state,
            "action_busy": RUNNER.busy,
        }

    def _build_config(self) -> dict:
        cfg = Config.load()
        return {
            "pushplus_token_masked": mask_token(cfg.pushplus_token),
            "pushplus_token_set": bool(cfg.pushplus_token),
            "pushplus_topic": cfg.pushplus_topic,
            "pushplus_channel": cfg.pushplus_channel,
            "schedule_hour": cfg.schedule_hour,
            "schedule_minute": cfg.schedule_minute,
            "checkin_id": cfg.checkin_id,
            "kdt_id": cfg.kdt_id,
            "run_on_startup": cfg.run_on_startup,
            "login_wait_seconds": cfg.login_wait_seconds,
            "notify_on_success": cfg.notify_on_success,
            "notify_on_already": cfg.notify_on_already,
            "notify_on_failure": cfg.notify_on_failure,
            "web_host": cfg.web_host,
            "web_port": cfg.web_port,
        }

    def _build_logs(self) -> dict:
        cfg = Config.load()
        try:
            text = cfg.log_dir.joinpath("app.log").read_text(
                encoding="utf-8", errors="replace")
            lines = text.splitlines()[-120:]
        except Exception:
            lines = ["（暂无日志）"]
        return {"lines": lines}

    # -- POST 处理 ---------------------------------------------------------
    def _post_config(self, data: dict) -> None:
        cfg = Config.load()

        # 只接受白名单字段
        unknown = {k for k in data if k not in FIELD_MAP}
        if unknown:
            return self._json({"ok": False,
                               "message": f"不允许的字段：{sorted(unknown)}"}, 400)

        updates: dict[str, str] = {}
        env_key_of = FIELD_MAP  # 本地别名

        token = str(data.get("pushplus_token", "")).strip()
        if token and token != _MASK and token != mask_token(cfg.pushplus_token):
            updates["PUSHPLUS_TOKEN"] = token

        for field in ("pushplus_topic", "pushplus_channel"):
            if field in data:
                updates[env_key_of[field]] = str(data[field]).strip()

        for field in ("schedule_hour", "schedule_minute", "login_wait_seconds"):
            if field not in data:
                continue
            try:
                value = int(str(data[field]).strip())
            except ValueError:
                return self._json({"ok": False, "message": f"{field} 必须是数字"}, 400)
            if field == "schedule_hour" and not (0 <= value <= 23):
                return self._json({"ok": False, "message": "签到小时需在 0-23"}, 400)
            if field == "schedule_minute" and not (0 <= value <= 59):
                return self._json({"ok": False, "message": "签到分钟需在 0-59"}, 400)
            if field == "login_wait_seconds" and not (60 <= value <= 7200):
                return self._json({"ok": False, "message": "登录等待需在 60-7200 秒"}, 400)
            updates[env_key_of[field]] = str(value)

        for field in ("notify_on_success", "notify_on_already",
                      "notify_on_failure", "run_on_startup"):
            if field in data:
                updates[env_key_of[field]] = "true" if data.get(field) else "false"

        for field in ("checkin_id", "kdt_id"):
            if field in data:
                value = str(data.get(field, "")).strip()
                if not value:
                    return self._json({"ok": False, "message": f"{field} 不能为空"}, 400)
                updates[env_key_of[field]] = value

        if not updates:
            return self._json({"ok": True, "message": "没有需要保存的修改"})

        changed = update_env(cfg, updates)

        # 签到时间变了 → 立即重排定时任务，无需重启
        scheduler = getattr(self.server, "aps_scheduler", None)
        if scheduler is not None and {"SCHEDULE_HOUR", "SCHEDULE_MINUTE"} & set(updates):
            new_cfg = Config.load()
            scheduler.reschedule_job("daily_checkin", trigger=make_trigger(new_cfg))
            db.audit(new_cfg, "info", "web_reschedule",
                     f"网页控制台调整签到时间为 {new_cfg.schedule_hour:02d}:{new_cfg.schedule_minute:02d}")
        db.audit(Config.load(), "info", "web_config_save",
                 "网页控制台保存配置：" + ", ".join(changed))
        self._json({"ok": True, "message": "已保存（"
                    + ", ".join(changed) + "）"})

    def _post_action(self, data: dict) -> None:
        action = str(data.get("action", "")).strip()
        cfg = Config.load()
        db.init_db(cfg)
        actions = {
            "checkin": ("立即签到", _action_checkin),
            "login": ("打开登录窗口", _action_login),
            "resume": ("解除暂停", _action_resume),
            "notify_test": ("测试推送", _action_notify_test),
        }
        if action not in actions:
            return self._json({"ok": False, "message": f"未知操作：{action}"}, 400)
        name, fn = actions[action]
        if RUNNER.busy:
            return self._json({"ok": False, "message": "已有操作进行中，请稍候"}, 409)
        if action in ("checkin", "login") and db.is_paused(cfg) and action != "login":
            pass  # checkin 允许在暂停态尝试（会再次确认登录失效）
        if not RUNNER.start(name, fn):
            return self._json({"ok": False, "message": "操作启动失败（可能并发）"}, 409)
        hint = {
            "checkin": "正在签到，稍候可在状态区查看结果",
            "login": f"已打开浏览器登录窗口，请在 {cfg.login_wait_seconds // 60} 分钟内完成登录",
            "resume": "已解除暂停",
            "notify_test": "正在发送测试推送",
        }[action]
        self._json({"ok": True, "message": hint})


def run_web(cfg: Config) -> None:
    """启动网页控制台（内含每日 08:00 调度与启动补签）。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    db.init_db(cfg)
    db.audit(cfg, "info", "web_start",
             f"网页控制台启动：http://{cfg.web_host}:{cfg.web_port}")

    scheduler = BackgroundScheduler(timezone=cfg.tz)
    scheduler.add_job(
        lambda: daily_job(cfg),
        trigger=make_trigger(cfg),
        id="daily_checkin",
        name="有赞每日自动签到",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.start()

    server = ThreadingHTTPServer((cfg.web_host, cfg.web_port), Handler)
    server.aps_scheduler = scheduler
    server.daemon_threads = True

    print("=" * 68)
    print("  有赞每日自动签到 - 网页控制台已启动")
    print(f"  控制台地址：http://{cfg.web_host}:{cfg.web_port}")
    print(f"  当前时间  ：{now_str(cfg)} ({cfg.timezone})")
    print(f"  每日执行  ：{cfg.schedule_hour:02d}:{cfg.schedule_minute:02d}"
          f"（RUN_ON_STARTUP={'开' if cfg.run_on_startup else '关'}，开机补签）")
    print(f"  数据目录  ：{cfg.data_dir}")
    print("=" * 68)

    if cfg.run_on_startup:
        print("[i] RUN_ON_STARTUP=true，启动时立即执行一次补签")
        threading.Thread(target=daily_job, args=(cfg,), daemon=True).start()

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        server.server_close()
        db.audit(cfg, "info", "web_stop", "网页控制台已停止")


# ---------------------------------------------------------------------------
# 前端单页（原生 HTML/CSS/JS，无外部资源）
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>影视飓风club自动签到</title>
<style>
:root { --line:#e5e7eb; --muted:#6b7280; --bg:#ffffff; --card:#fff;
        --brand:#e63946; --ok:#16a34a; --warn:#d97706; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
       background:var(--bg); color:#1f2937; font-size:14px; }
.wrap { max-width:900px; margin:0 auto; padding:20px 16px 60px;
        position:relative; z-index:1; }
h1 { font-size:20px; margin:8px 0 2px; }
h1 .dot { display:inline-block;width:10px;height:10px;border-radius:50%;
          margin-right:8px; vertical-align:1px; }
.sub { color:var(--muted); font-size:12px; margin-bottom:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px; margin-bottom:14px; }
.card h2 { font-size:15px; margin:0 0 12px; display:flex; align-items:center;
           justify-content:space-between; }
.grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
        gap:10px; }
@media (max-width:760px) { .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.kv { background:#fafafa; border:1px solid var(--line); border-radius:8px;
      padding:10px 12px; min-height:66px; }
.kv .k { color:var(--muted); font-size:12px; margin-bottom:4px; }
.kv .v { font-size:14px; font-weight:600; word-break:break-all; }
.btn { display:inline-block; border:0; border-radius:8px; padding:9px 16px;
       font-size:14px; cursor:pointer; background:#ef4444; color:#fff; }
.btn.ghost { background:#fff; color:#374151; border:1px solid #d1d5db; }
.btn:disabled { opacity:.5; cursor:not-allowed; }
.btn + .btn { margin-left:8px; }
.row { display:flex; flex-wrap:wrap; gap:10px; }
form .field { margin-bottom:12px; }
label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
input[type=text], input[type=password], input[type=number] {
  width:100%; max-width:340px; padding:8px 10px; border:1px solid #d1d5db;
  border-radius:8px; font-size:14px; }
.inline { display:flex; gap:16px; flex-wrap:wrap; }
.inline .field { min-width:110px; }
.switch { display:flex; align-items:center; gap:8px; margin:6px 0; }
.switch input { transform:scale(1.1); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; }
.tag { display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; }
.tag.success { background:#dcfce7; color:var(--ok); }
.tag.already { background:#e0f2fe; color:#0369a1; }
.tag.failed, .tag.error, .tag.login_expired { background:#fee2e2; color:var(--brand); }
pre { background:#111827; color:#d1fae5; font-size:12px; padding:12px;
      border-radius:8px; overflow:auto; max-height:320px; }
.toast { position:fixed; left:50%; top:18px; transform:translateX(-50%);
         background:#111827; color:#fff; padding:10px 18px; border-radius:8px;
         font-size:13px; opacity:0; transition:opacity .25s; z-index:9; }
.toast.show { opacity:.96; }
.toast.ok { background:var(--ok); }
.toast.err { background:var(--brand); }
.hint { font-size:12px; color:var(--muted); margin-top:4px; }
.actionbar { display:flex; gap:10px; flex-wrap:wrap; }
#actionState { font-size:13px; margin-top:10px; color:var(--warn); }
details summary { cursor:pointer; color:var(--muted); font-size:13px; }
/* ===== 背景装饰：左右「无限」「进步」大字水印 ===== */
.bg-deco {
  position: fixed;
  top: 46%;
  transform: translateY(-50%);
  width: min(32vw, calc((100vw - 900px) / 2 - 24px));
  background-repeat: no-repeat;
  background-position: center;
  background-size: 100% 100%;
  opacity: .15;
  pointer-events: none;
  z-index: 0;
}
.bg-deco--left {
  left: 1vw;
  aspect-ratio: 270 / 120;
  background-image: url("data:image/png;base64,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");
  -webkit-mask-image: linear-gradient(to right, #000 0%, #000 76%, transparent 100%);
          mask-image: linear-gradient(to right, #000 0%, #000 76%, transparent 100%);
}
.bg-deco--right {
  right: 5vw;
  width: min(24.5vw, calc((100vw - 900px) / 2 - 24px) * 0.767);
  aspect-ratio: 207 / 120;
  background-image: url("data:image/png;base64,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");
  -webkit-mask-image: linear-gradient(to left, #000 0%, #000 82%, transparent 100%);
          mask-image: linear-gradient(to left, #000 0%, #000 82%, transparent 100%);
}
@media (max-width: 1080px) {
  .bg-deco { width: 22vw; opacity: .07; }
}
</style>
</head>
<body>
<div class="bg-deco bg-deco--left" aria-hidden="true"></div>
<div class="bg-deco bg-deco--right" aria-hidden="true"></div>
<div class="toast" id="toast"></div>
<div class="wrap">
  <h1><span class="dot" id="dot"></span>影视飓风club自动签到</h1>
  <div class="sub">网页控制台 · 数据保存在本机 · 刷新页面自动更新状态</div>

  <div class="card">
    <h2>当前状态 <span style="font-weight:400;font-size:12px;color:var(--muted)" id="now"></span></h2>
    <div class="grid">
      <div class="kv"><div class="k">运行状态</div><div class="v" id="stState">-</div></div>
      <div class="kv"><div class="k">每日签到时间</div><div class="v" id="stSchedule">-</div></div>
      <div class="kv"><div class="k">下次执行</div><div class="v" id="stNext">-</div></div>
      <div class="kv"><div class="k">上次登录</div><div class="v" id="stLogin">-</div></div>
      <div class="kv"><div class="k">上次成功日期</div><div class="v" id="stSuccess">-</div></div>
      <div class="kv"><div class="k">微信通知</div><div class="v" id="stNotify">-</div></div>
    </div>
    <div id="pauseBox" style="display:none;margin-top:10px;color:var(--brand);
         background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px"></div>
  </div>

  <div class="card">
    <h2>常用操作</h2>
    <div class="actionbar">
      <button class="btn" id="btnCheckin">立即签到</button>
      <button class="btn ghost" id="btnLogin">打开登录窗口（首次/失效后）</button>
      <button class="btn ghost" id="btnResume">解除暂停</button>
      <button class="btn ghost" id="btnNotify">发送测试推送</button>
    </div>
    <div id="actionState"></div>
    <div class="hint">「打开登录窗口」会在本机弹出 Chrome，请用手机号+验证码登录，完成后自动保存登录态。</div>
  </div>

  <div class="card">
    <h2>通知与签到设置</h2>
    <form id="cfgForm" onsubmit="return saveConfig(event)">
      <div class="field">
        <label>PushPlus Token（微信通知）</label>
        <input type="password" id="f_token" autocomplete="off" placeholder="">
        <div class="hint" id="tokenHint">未配置。留空表示不修改。</div>
      </div>
      <div class="inline">
        <div class="field"><label>PushPlus 群组编码（可选）</label>
          <input type="text" id="f_topic"></div>
      </div>
      <div class="inline">
        <div class="field"><label>签到时间（时 0-23）</label>
          <input type="number" id="f_hour" min="0" max="23"></div>
        <div class="field"><label>签到时间（分 0-59）</label>
          <input type="number" id="f_minute" min="0" max="59"></div>
        <div class="field"><label>登录等待（分钟）</label>
          <input type="number" id="f_wait" min="1" max="120"></div>
      </div>
      <div class="inline">
        <div class="field"><label>签到活动 checkinId</label>
          <input type="text" id="f_checkinId"></div>
        <div class="field"><label>店铺 kdt_id</label>
          <input type="text" id="f_kdtId"></div>
      </div>
      <div class="field">
        <label>通知与补签开关</label>
        <div class="switch"><input type="checkbox" id="f_nSucc"><span>签到成功时通知</span></div>
        <div class="switch"><input type="checkbox" id="f_nAlready"><span>「今日已签」也通知</span></div>
        <div class="switch"><input type="checkbox" id="f_nFail"><span>失败/登录失效时通知</span></div>
        <div class="switch"><input type="checkbox" id="f_startup"><span>服务启动时补签（漏掉 08:00 时自动补）</span></div>
      </div>
      <button class="btn" type="submit">保存设置</button>
    </form>
  </div>

  <div class="card">
    <h2>最近签到记录</h2>
    <table>
      <thead><tr><th>日期</th><th>结果</th><th>code</th><th>完成时间</th><th>说明</th></tr></thead>
      <tbody id="records"><tr><td colspan="5">加载中…</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <details>
      <summary>运行日志（最近 120 行，已脱敏）</summary>
      <pre id="logs" style="margin-top:10px">加载中…</pre>
      <button class="btn ghost" style="margin-top:8px" onclick="loadLogs()">刷新日志</button>
    </details>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let busy = false;

function toast(msg, ok) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = 'toast', 3200);
}

async function post(url, body) {
  const r = await fetch(url, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  return r.json();
}

function renderAction(a) {
  const box = $('actionState');
  if (!a) { box.textContent = ''; return; }
  if (a.status === 'running') {
    box.textContent = `⏳ ${a.name} 进行中（开始于 ${a.started_at}）…`;
  } else if (a.result) {
    box.style.color = a.result.ok ? 'var(--ok)' : 'var(--brand)';
    box.textContent = `${a.result.ok ? '✔' : '✖'} ${a.name}：${a.result.message}`;
  }
}

async function loadStatus() {
  try {
    const s = await (await fetch('/api/status')).json();
    $('now').textContent = '更新于 ' + s.now;
    const paused = s.paused;
    $('dot').style.background = paused ? 'var(--brand)' : 'var(--ok)';
    $('stState').textContent = paused ? '已暂停（需重新登录）' : '正常运行';
    $('stState').style.color = paused ? 'var(--brand)' : 'var(--ok)';
    $('stSchedule').textContent = '每天 ' + s.schedule +
        (s.run_on_startup ? '（含启动补签）' : '');
    $('stNext').textContent = s.next_run || '-';
    $('stLogin').textContent = s.last_login_at;
    $('stSuccess').textContent = s.last_success_date;
    $('stNotify').textContent = s.pushplus_configured ? '已配置' : '未配置';
    $('stNotify').style.color = s.pushplus_configured ? 'var(--ok)' : 'var(--warn)';
    const pb = $('pauseBox');
    if (paused) {
      pb.style.display = 'block';
      pb.textContent = '登录已失效：' + (s.pause_reason || '需重新登录') +
        '（' + (s.paused_at || '') + '）。点击「打开登录窗口」重新登录即可自动恢复。';
    } else { pb.style.display = 'none'; }
    const tb = $('records');
    if (!s.records.length) {
      tb.innerHTML = '<tr><td colspan="5">暂无记录</td></tr>';
    } else {
      tb.innerHTML = s.records.map(r =>
        `<tr><td>${r.biz_date}</td><td><span class="tag ${r.status}">${statusText(r.status)}</span></td>` +
        `<td>${r.code ?? '-'}</td><td>${r.finished_at || '-'}</td><td>${r.message || ''}</td></tr>`).join('');
    }
    busy = s.action_busy;
    ['btnCheckin','btnLogin','btnNotify'].forEach(b => $(b).disabled = busy);
    renderAction(s.action);
  } catch (e) { /* 网页控制台未运行时静默 */ }
}

function statusText(s) {
  return {success:'签到成功', already:'今日已签', failed:'失败',
          login_expired:'登录失效', error:'异常'}[s] || s;
}

async function loadConfig() {
  const c = await (await fetch('/api/config')).json();
  $('f_token').placeholder = c.pushplus_token_set ?
      ('已保存：' + c.pushplus_token_masked + '（留空不修改）') : '未配置';
  $('tokenHint').textContent = c.pushplus_token_set ?
      '已保存 ' + c.pushplus_token_masked : '未配置（填入 Token 保存即可开启微信通知）';
  $('f_topic').value = c.pushplus_topic || '';
  $('f_hour').value = c.schedule_hour;
  $('f_minute').value = c.schedule_minute;
  $('f_wait').value = Math.round(c.login_wait_seconds / 60);
  $('f_checkinId').value = c.checkin_id;
  $('f_kdtId').value = c.kdt_id;
  $('f_nSucc').checked = c.notify_on_success;
  $('f_nAlready').checked = c.notify_on_already;
  $('f_nFail').checked = c.notify_on_failure;
  $('f_startup').checked = c.run_on_startup;
}

async function saveConfig(ev) {
  ev.preventDefault();
  const body = {
    pushplus_topic: $('f_topic').value.trim(),
    schedule_hour: $('f_hour').value,
    schedule_minute: $('f_minute').value,
    login_wait_seconds: (parseInt($('f_wait').value || '10', 10) || 10) * 60,
    checkin_id: $('f_checkinId').value.trim(),
    kdt_id: $('f_kdtId').value.trim(),
    notify_on_success: $('f_nSucc').checked,
    notify_on_already: $('f_nAlready').checked,
    notify_on_failure: $('f_nFail').checked,
    run_on_startup: $('f_startup').checked,
  };
  const token = $('f_token').value.trim();
  if (token) body.pushplus_token = token;
  const r = await post('/api/config', body);
  toast(r.message, r.ok);
  if (r.ok) { $('f_token').value = ''; loadConfig(); loadStatus(); }
  return false;
}

async function loadLogs() {
  try {
    const r = await (await fetch('/api/logs')).json();
    $('logs').textContent = r.lines.join('\n') || '（暂无日志）';
  } catch (e) { $('logs').textContent = '加载失败'; }
}

async function doAction(action) {
  if (busy) { toast('已有操作进行中，请稍候', false); return; }
  const r = await post('/api/action', {action});
  toast(r.message, r.ok);
  loadStatus();
}

$('btnCheckin').onclick = () => doAction('checkin');
$('btnLogin').onclick = () => doAction('login');
$('btnResume').onclick = () => doAction('resume');
$('btnNotify').onclick = () => doAction('notify_test');

loadConfig(); loadLogs(); loadStatus();
setInterval(loadStatus, 5000);
</script>
</body>
</html>
"""
