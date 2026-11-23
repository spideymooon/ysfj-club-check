"""
命令行入口。

  init         初始化目录与 SQLite
  login        有头浏览器人工登录（首次 / 登录失效后重新登录）
  probe        抓取结果页真实签到请求，补充参数与 Header
  once         单次签到（本地联调用）
  serve        启动每日 08:00 定时调度（纯命令行，Docker 默认）
  web          启动网页控制台 + 每日定时调度（本机推荐）
  status       查看状态与最近签到记录
  resume       手动解除「登录失效暂停」
  notify-test  测试 PushPlus 推送
"""
from __future__ import annotations

import argparse
import sys

from . import db, notifier
from .browser import do_login, do_probe
from .checkin import run_once
from .classifier import Outcome
from .config import Config, now_str
from .logging_setup import setup_logging
from .scheduler import serve


def _print_status(cfg: Config) -> None:
    paused = db.is_paused(cfg)
    print("=" * 68)
    print(f"  当前时间      : {now_str(cfg)} ({cfg.timezone})")
    print(f"  运行状态      : {'已暂停（需重新登录）' if paused else '正常'}")
    if paused:
        print(f"  暂停原因      : {db.get_state(cfg, 'pause_reason', '')}")
        print(f"  暂停时间      : {db.get_state(cfg, 'paused_at', '')}")
    print(f"  上次登录      : {db.get_state(cfg, 'last_login_at', '从未')}")
    print(f"  上次成功日期  : {db.get_state(cfg, 'last_success_date', '从未')}")
    print(f"  真实请求捕获  : {db.get_state(cfg, 'captured_at', '未捕获')}")
    print(f"  Profile 目录  : {cfg.profile_dir}")
    print(f"  数据库        : {cfg.db_path}")
    print(f"  日志文件      : {cfg.log_dir / 'app.log'}")
    print("-" * 68)
    rows = db.recent_records(cfg, 10)
    if not rows:
        print("  暂无签到记录")
    else:
        print("  最近签到记录：")
        for r in rows:
            print(f"    #{r['id']:<4} {r['biz_date']}  {r['status']:<14} "
                  f"code={r['resp_code'] or '-':<12} {r['finished_at'] or ''}")
            if r["message"]:
                print(f"          {r['message']}")
    print("=" * 68)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="有赞每日自动签到（Python + Playwright）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化目录与 SQLite")
    sub.add_parser("login", help="有头浏览器人工登录并保存登录态")
    sub.add_parser("probe", help="抓取结果页真实签到请求，补充参数/Header")

    p_once = sub.add_parser("once", help="执行一次签到（本地测试用）")
    p_once.add_argument("--force", action="store_true",
                        help="忽略幂等保护与暂停状态，强制请求一次")

    sub.add_parser("serve", help="启动每日定时调度（纯命令行，Docker 默认命令）")

    p_web = sub.add_parser("web", help="启动网页控制台 + 每日定时调度（本机推荐）")

    sub.add_parser("status", help="查看运行状态与最近记录")
    sub.add_parser("resume", help="手动解除登录失效暂停")
    sub.add_parser("notify-test", help="测试 PushPlus 推送")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    setup_logging(cfg)

    if args.command == "init":
        cfg.ensure_dirs()
        db.init_db(cfg)
        print(f"[OK] 初始化完成：{cfg.data_dir}")
        print(f"     数据库   : {cfg.db_path}")
        print(f"     Profile : {cfg.profile_dir}")
        print(f"     日志     : {cfg.log_dir / 'app.log'}")
        return 0

    if args.command == "login":
        return 0 if do_login(cfg, headless=False) else 1

    if args.command == "probe":
        do_probe(cfg)
        return 0

    if args.command == "once":
        verdict = run_once(cfg, force=args.force)
        print("-" * 68)
        print(f"  签到结果 : {verdict.outcome.value}")
        print(f"  code     : {verdict.code}")
        print(f"  说明     : {verdict.message}")
        if verdict.raw_excerpt:
            print(f"  响应摘要 : {verdict.raw_excerpt[:300]}")
        print("-" * 68)
        if verdict.outcome is Outcome.LOGIN_EXPIRED:
            print("[!] 登录态失效，已暂停签到并推送通知，请执行 login 重新登录")
            return 2
        return 0 if verdict.ok or verdict.outcome is Outcome.FAILED else 1

    if args.command == "serve":
        serve(cfg)
        return 0

    if args.command == "web":
        from .web import run_web
        run_web(cfg)
        return 0

    if args.command == "status":
        db.init_db(cfg)
        _print_status(cfg)
        return 0

    if args.command == "resume":
        db.init_db(cfg)
        db.resume(cfg)
        db.audit(cfg, "info", "resume", "手动解除登录失效暂停")
        print("[OK] 已解除暂停状态。若登录态确实已恢复，建议先执行 once 验证。")
        return 0

    if args.command == "notify-test":
        ok = notifier.send(
            cfg,
            "【测试】有赞签到通知",
            "这是一条来自有赞自动签到的 PushPlus 测试消息。<br>如果你收到，说明通知配置正确。",
            force=True,
        )
        print("[OK] 推送已发送" if ok else "[!] 推送失败，请检查 PUSHPLUS_TOKEN / 实名认证")
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
