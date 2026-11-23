"""
每日 08:00（北京时间）定时调度。

时区显式指定为 Asia/Shanghai，不依赖容器/宿主机本地时区，
因此 Docker 内即使 TZ 未生效也能准点触发。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, notifier
from .checkin import run_once
from .config import Config, now_str

log = logging.getLogger("youzan.scheduler")


def daily_job(cfg: Config) -> None:
    """每日定时任务主体（serve 与网页控制台共用）。"""
    if db.is_paused(cfg):
        log.warning("当前处于登录失效暂停状态，跳过今日签到")
        try:
            notifier.notify_paused_reminder(cfg)
        except Exception:
            pass
        return
    try:
        verdict = run_once(cfg)
        log.info("定时任务完成：%s", verdict.outcome.value)
    except Exception as exc:
        log.exception("定时任务异常：%s", type(exc).__name__)
        db.audit(cfg, "error", "scheduler_job_error", f"定时任务异常：{type(exc).__name__}")


def make_trigger(cfg: Config) -> CronTrigger:
    return CronTrigger(
        hour=cfg.schedule_hour,
        minute=cfg.schedule_minute,
        timezone=cfg.tz,
    )


def serve(cfg: Config) -> None:
    db.init_db(cfg)
    db.audit(cfg, "info", "scheduler_start",
             f"调度启动：每日 {cfg.schedule_hour:02d}:{cfg.schedule_minute:02d} {cfg.timezone}")

    scheduler = BlockingScheduler(timezone=cfg.tz)

    trigger = make_trigger(cfg)
    scheduler.add_job(
        lambda: daily_job(cfg),
        trigger=trigger,
        id="daily_checkin",
        name="有赞每日自动签到",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # APScheduler 未 start 前 Job 上不一定有 next_run_time（不同版本实现有差异），
    # 直接向 trigger 询问下一次触发时间，避免依赖内部属性。
    try:
        from datetime import datetime
        next_run = trigger.get_next_fire_time(None, datetime.now(cfg.tz))
    except Exception:
        next_run = None
    print("=" * 68)
    print("  有赞每日自动签到 - 调度已启动")
    print(f"  当前时间  ：{now_str(cfg)} ({cfg.timezone})")
    print(f"  每日执行  ：{cfg.schedule_hour:02d}:{cfg.schedule_minute:02d}")
    print(f"  下次执行  ：{next_run}")
    print(f"  数据库    ：{cfg.db_path}")
    print(f"  登录 Profile：{cfg.profile_dir}")
    print("=" * 68)

    if cfg.run_on_startup:
        print("[i] RUN_ON_STARTUP=true，启动时立即执行一次补签")
        try:
            if db.is_paused(cfg):
                print("[!] 当前处于暂停状态，跳过启动补签")
            else:
                run_once(cfg)
        except Exception as exc:
            log.exception("启动补签异常：%s", type(exc).__name__)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("调度器收到退出信号，已停止")
        db.audit(cfg, "info", "scheduler_stop", "调度器已停止")
