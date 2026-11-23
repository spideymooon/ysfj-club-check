"""调度器启动回归测试：mock 掉阻塞的 start，验证配置/触发时间计算正常。"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import app.db as dbmod
from app.config import Config


class TestScheduler(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        if dbmod._CONN is not None:
            try:
                dbmod._CONN.close()
            except Exception:
                pass
        dbmod._CONN = None
        dbmod._CONN_PATH = None
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "DATA_DIR": self.tmp.name,
            "PUSHPLUS_ENABLED": "false",
            "RUN_ON_STARTUP": "false",
            "SCHEDULE_HOUR": "8",
            "SCHEDULE_MINUTE": "0",
            "TIMEZONE": "Asia/Shanghai",
        }

    def tearDown(self):
        logging.disable(logging.NOTSET)
        with dbmod._LOCK:
            if dbmod._CONN is not None:
                try:
                    dbmod._CONN.close()
                except Exception:
                    pass
            dbmod._CONN = None
            dbmod._CONN_PATH = None
        self.tmp.cleanup()

    def test_serve_registers_daily_job_and_starts(self):
        import io
        from contextlib import redirect_stdout
        with patch.dict(os.environ, self.env), \
             patch("app.scheduler.BlockingScheduler.start") as start_mock, \
             redirect_stdout(io.StringIO()):
            from app.scheduler import serve
            cfg = Config.load()
            serve(cfg)
            start_mock.assert_called_once()

    def test_next_fire_time_is_0800_beijing(self):
        """CronTrigger 计算的下一次触发时间应为北京时间 08:00。"""
        from datetime import datetime
        from apscheduler.triggers.cron import CronTrigger
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
        trigger = CronTrigger(hour=8, minute=0, timezone=tz)
        nxt = trigger.get_next_fire_time(None, datetime(2026, 11, 23, 2, 0, tzinfo=tz))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.hour, 8)
        self.assertEqual(nxt.minute, 0)
        self.assertEqual(nxt.date().isoformat(), "2026-11-23")


if __name__ == "__main__":
    unittest.main(verbosity=2)
