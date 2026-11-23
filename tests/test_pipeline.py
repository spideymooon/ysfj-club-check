"""
完整签到流程的离线端到端测试（注入假浏览器，不访问网络）。

覆盖：成功落库 / 幂等保护 / 前置检查跳过 / 重复签到 / 登录失效暂停 /
业务失败不重试 / 网络错误有限重试 / 强制补签。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import app.db as dbmod
from app import db
from app.checkin import run_once
from app.classifier import Outcome
from app.config import Config

JSON = "application/json; charset=utf-8"


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class FakeResponse:
    def __init__(self, status=200, ctype=JSON, body=b"{}", url=""):
        self.status = status
        self.headers = {"content-type": ctype}
        self._body = body
        self.url = url or "https://club.ysjf.com/wscump/checkin/checkinV2.json"

    def body(self):
        return self._body


class FakeRequestAPI:
    def __init__(self, activity: FakeResponse, checkin: FakeResponse):
        self.activity = activity
        self.checkin = checkin
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if "get_activity" in url:
            return self.activity
        return self.checkin


class FakePage:
    def __init__(self, url):
        self.url = url

    def on(self, event, cb):
        pass

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        return None

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "签到结果"


class FakeContext:
    def __init__(self, api, page_url):
        self.request = api
        self.pages = []
        self._page_url = page_url

    def new_page(self):
        return FakePage(self._page_url)


class FakeSession:
    def __init__(self, cfg, api):
        self.cfg = cfg
        self.api = api
        self.closed = False

    def start(self):
        return FakeContext(self.api, self.cfg.result_page)

    def new_page(self):
        return FakePage(self.cfg.result_page)

    def user_agent(self):
        return "Mozilla/5.0 (offline-test)"

    def close(self):
        self.closed = True


class TestPipeline(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        # 每个测试用独立数据库，避免模块级连接串味
        if dbmod._CONN is not None:
            try:
                dbmod._CONN.close()
            except Exception:
                pass
        dbmod._CONN = None
        dbmod._CONN_PATH = None

        self.tmp = tempfile.TemporaryDirectory()
        self.base_env = {
            "DATA_DIR": self.tmp.name,
            "PUSHPLUS_ENABLED": "false",
            "CAPTURE_ENABLED": "false",
            "PRECHECK_ENABLED": "true",
            "MAX_RETRY": "3",
            "RETRY_BACKOFF": "0",
            "NOTIFY_ON_SUCCESS": "false",
            "NOTIFY_ON_FAILURE": "false",
        }

    def tearDown(self):
        logging.disable(logging.NOTSET)
        # Windows 下必须先关闭 SQLite 连接，否则临时目录文件被占用无法删除
        with dbmod._LOCK:
            if dbmod._CONN is not None:
                try:
                    dbmod._CONN.close()
                except Exception:
                    pass
            dbmod._CONN = None
            dbmod._CONN_PATH = None
        self.tmp.cleanup()

    def _cfg(self, **overrides):
        env = dict(self.base_env)
        env.update({k: str(v) for k, v in overrides.items()})
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Config.load()

    def _script(self, *, activity=None, checkin=None):
        a = activity or FakeResponse(body=_json({
            "code": 0, "msg": "ok",
            "data": {"isCheckin": False, "continuesDay": 3, "isOpen": True},
        }))
        c = checkin or FakeResponse(body=_json({
            "code": 0, "msg": "ok",
            "data": {"success": True, "list": [{"isSuccess": True, "award": "+10积分"}]},
        }))
        return FakeRequestAPI(a, c)

    def _factory(self, api):
        return lambda cfg: FakeSession(cfg, api)

    def test_success_and_idempotency(self):
        cfg = self._cfg()
        api = self._script()
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.SUCCESS)
        self.assertEqual(sum("checkinV2" in u for u in api.calls), 1)

        from app.config import today_str
        self.assertTrue(db.today_has_terminal(cfg, today_str(cfg)))
        rows = db.recent_records(cfg)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["biz_date"], today_str(cfg))

        # 第二次执行应被幂等保护拦截，不发任何请求
        api2 = self._script()
        v2 = run_once(cfg, session_factory=self._factory(api2))
        self.assertIs(v2.outcome, Outcome.SKIPPED_DONE)
        self.assertEqual(api2.calls, [], "幂等保护下不应发起任何请求")

    def test_force_bypasses_idempotency(self):
        cfg = self._cfg()
        api = self._script()
        run_once(cfg, session_factory=self._factory(api))
        api2 = self._script()
        v = run_once(cfg, force=True, session_factory=self._factory(api2))
        self.assertIs(v.outcome, Outcome.SUCCESS)
        self.assertEqual(sum("checkinV2" in u for u in api2.calls), 1)

    def test_precheck_already_skips_checkin_request(self):
        cfg = self._cfg()
        api = self._script(activity=FakeResponse(body=_json({
            "code": 0, "data": {"isCheckin": True, "continuesDay": 5},
        })))
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.ALREADY)
        self.assertTrue(v.ok)
        self.assertEqual(sum("checkinV2" in u for u in api.calls), 0,
                         "今日已签到时不应再发签到请求")
        self.assertEqual(db.recent_records(cfg)[0]["status"], "already")

    def test_duplicate_response_is_terminal(self):
        cfg = self._cfg(PRECHECK_ENABLED="false")
        api = self._script(checkin=FakeResponse(body=_json({
            "code": 1001, "msg": "今日已签到，请勿重复提交",
        })))
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.ALREADY)
        self.assertEqual(len(api.calls), 1, "重复签到不能重试")

    def test_login_expired_pauses(self):
        cfg = self._cfg(PRECHECK_ENABLED="false")
        api = self._script(checkin=FakeResponse(body=_json({
            "code": 1000000002, "msg": "userId must be greater than or equal to 1 ",
        })))
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)
        self.assertTrue(v.needs_relogin)
        self.assertTrue(db.is_paused(cfg))
        self.assertEqual(len(api.calls), 1, "登录失效不能重试")
        self.assertEqual(db.recent_records(cfg)[0]["status"], "login_expired")

        # 暂停后调度再执行应直接跳过
        v2 = run_once(cfg, session_factory=self._factory(self._script()))
        self.assertIs(v2.outcome, Outcome.SKIPPED_PAUSED)

    def test_business_failure_no_retry(self):
        cfg = self._cfg(PRECHECK_ENABLED="false")
        api = self._script(checkin=FakeResponse(body=_json({
            "code": 12345, "msg": "缺少必要的参数 - checkinId",
        })))
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.FAILED)
        self.assertEqual(len(api.calls), 1)

    def test_transient_error_limited_retry(self):
        cfg = self._cfg(PRECHECK_ENABLED="false", MAX_RETRY="3")
        api = self._script(checkin=FakeResponse(
            status=502, ctype="text/html", body=b"<h1>502</h1>"))
        v = run_once(cfg, session_factory=self._factory(api))
        self.assertIs(v.outcome, Outcome.ERROR)
        self.assertEqual(len(api.calls), 3, "仅网络类错误重试，且不超过 MAX_RETRY")

    def test_no_cookie_in_real_request_headers(self):
        cfg = self._cfg()
        api = self._script()
        run_once(cfg, session_factory=self._factory(api))
        for url in api.calls:
            self.assertNotIn("cookie=", url.lower())
        # 构造出的请求头里也不应出现 Cookie
        captured = {}
        orig = FakeRequestAPI.get

        def spy(self_inner, url, params=None, headers=None, timeout=None):
            captured.update(headers or {})
            return orig(self_inner, url, params=params, headers=headers, timeout=timeout)

        FakeRequestAPI.get = spy
        try:
            api2 = self._script()
            run_once(cfg, force=True, session_factory=self._factory(api2))
        finally:
            FakeRequestAPI.get = orig
        self.assertNotIn("cookie", [k.lower() for k in captured])
        self.assertIn("referer", [k.lower() for k in captured])
        self.assertIn("x-requested-with", [k.lower() for k in captured])


if __name__ == "__main__":
    unittest.main(verbosity=2)
