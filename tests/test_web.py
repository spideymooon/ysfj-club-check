"""网页控制台 API 离线测试（不依赖真实浏览器与外网）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import web
from app.config import Config


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "DATA_DIR": str(Path(self.tmp.name) / "data"),
            "PUSHPLUS_TOKEN": "",
            "WEB_HOST": "127.0.0.1",
        }
        self._old = {k: os.environ.get(k) for k in self.env}
        for k, v in self.env.items():
            os.environ[k] = v

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if hasattr(self, "server"):
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            self.server.server_close()
        from app import db
        db.close_db()  # 释放模块级连接，否则 Windows 下临时目录删不掉
        self.tmp.cleanup()

    def _start_server(self):
        cfg = Config.load()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        return cfg

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_index_page_served(self):
        self._start_server()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("影视飓风club自动签到", html)
        self.assertIn("/api/config", html)
        # 背景装饰：纯白主题 + 左右水印 + 不影响交互
        self.assertIn("--bg:#ffffff", html)
        self.assertIn("bg-deco--left", html)
        self.assertIn("bg-deco--right", html)
        self.assertIn("position: fixed", html)
        self.assertIn("pointer-events: none", html)

    def test_status_endpoint_shape(self):
        self._start_server()
        s = self._get("/api/status")
        for key in ("now", "paused", "last_login_at", "last_success_date",
                    "schedule", "records", "action", "pushplus_configured"):
            self.assertIn(key, s)
        self.assertFalse(s["paused"])
        self.assertIsInstance(s["records"], list)

    def test_config_token_masked(self):
        os.environ["PUSHPLUS_TOKEN"] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        self._start_server()
        c = self._get("/api/config")
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", json.dumps(c))
        self.assertEqual(c["pushplus_token_masked"], "ABC******WXYZ")
        self.assertTrue(c["pushplus_token_set"])

    def test_config_save_roundtrip_and_mask_semantics(self):
        self._start_server()
        # 保存新 token
        r = self._post("/api/config", {"pushplus_token": "secret-token-999"})
        self.assertTrue(r["ok"], r)
        # .env 中已写入明文（本机文件，不入 Git）
        env_text = (Path(self.tmp.name) / ".env").read_text("utf-8")
        self.assertIn("PUSHPLUS_TOKEN=secret-token-999", env_text)
        # os.environ 已刷新
        self.assertEqual(os.environ["PUSHPLUS_TOKEN"], "secret-token-999")
        # 新读取的配置生效
        self.assertEqual(Config.load().pushplus_token, "secret-token-999")
        # 再次读取接口：脱敏，不回显明文
        c = self._get("/api/config")
        self.assertNotIn("secret-token-999", json.dumps(c))

    def test_config_save_rejects_unknown_field(self):
        self._start_server()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/config", {"EVIL_KEY": "1"})
        self.assertEqual(ctx.exception.code, 400)

    def test_config_save_rejects_bad_hour(self):
        self._start_server()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/config", {"schedule_hour": "25"})
        self.assertEqual(ctx.exception.code, 400)

    def test_config_save_bool_and_schedule(self):
        self._start_server()
        r = self._post("/api/config", {
            "schedule_hour": 9, "schedule_minute": 30,
            "notify_on_already": True, "run_on_startup": True,
            "checkin_id": "12345", "kdt_id": "67890",
        })
        self.assertTrue(r["ok"], r)
        cfg = Config.load()
        self.assertEqual((cfg.schedule_hour, cfg.schedule_minute), (9, 30))
        self.assertTrue(cfg.notify_on_already)
        self.assertTrue(cfg.run_on_startup)
        self.assertEqual(cfg.checkin_id, "12345")
        self.assertEqual(cfg.kdt_id, "67890")

    def test_action_rejects_unknown(self):
        self._start_server()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/action", {"action": "rm -rf"})
        self.assertEqual(ctx.exception.code, 400)

    def test_action_resume_runs(self):
        from app import db
        self._start_server()
        cfg = Config.load()
        db.init_db(cfg)
        db.set_state(cfg, "paused_at", "x")
        r = self._post("/api/action", {"action": "resume"})
        self.assertTrue(r["ok"], r)
        # 动作是异步的，稍等完成
        import time
        for _ in range(30):
            if web.RUNNER.state and web.RUNNER.state.get("status") == "done":
                break
            time.sleep(0.1)
        self.assertTrue(web.RUNNER.state["result"]["ok"])

    def test_token_mask(self):
        self.assertEqual(web.mask_token(""), "")
        self.assertEqual(web.mask_token("short"), "*****")
        self.assertEqual(web.mask_token("12345678"), "********")
        self.assertEqual(web.mask_token("123456789"), "123******6789")


if __name__ == "__main__":
    unittest.main()
