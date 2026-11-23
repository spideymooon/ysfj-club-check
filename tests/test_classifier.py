"""响应判定 / 参数构造 / 日志脱敏 的离线单元测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.classifier import ActivityState, Outcome, classify, parse_activity
from app.config import Config
from app.headers import (
    SENSITIVE_HEADER_KEYS, SENSITIVE_QUERY_KEYS, build, safe_capture_from_request,
    safe_url_for_log,
)
from app.logging_setup import scrub

JSON = "application/json; charset=utf-8"


def _body(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class TestClassifier(unittest.TestCase):
    def test_confirmed_success_shape(self):
        """用户确认的成功响应：code=0 + data.success=true + list[0].isSuccess=true"""
        v = classify(200, JSON, _body({
            "code": 0, "msg": "ok",
            "data": {"success": True, "list": [{"isSuccess": True, "award": "+10积分"}]},
        }))
        self.assertIs(v.outcome, Outcome.SUCCESS)
        self.assertEqual(v.code, 0)
        self.assertTrue(v.ok)
        self.assertFalse(v.retryable)

    def test_success_without_list(self):
        v = classify(200, JSON, _body({"code": 0, "data": {"success": True}}))
        self.assertIs(v.outcome, Outcome.SUCCESS)

    def test_success_string_booleans(self):
        v = classify(200, JSON, _body({
            "code": "0", "data": {"success": "true", "list": [{"isSuccess": "true"}]},
        }))
        self.assertIs(v.outcome, Outcome.SUCCESS)

    def test_already_by_msg(self):
        v = classify(200, JSON, _body({"code": 1001, "msg": "今日已签到"}))
        self.assertIs(v.outcome, Outcome.ALREADY)
        self.assertTrue(v.ok)
        self.assertFalse(v.retryable, "重复签到绝不能重试")

    def test_already_by_list_reason(self):
        v = classify(200, JSON, _body({
            "code": 0,
            "data": {"success": True, "list": [{"isSuccess": False, "failReason": "今日已签到，明天再来"}]},
        }))
        self.assertIs(v.outcome, Outcome.ALREADY)

    def test_already_by_is_checkin(self):
        v = classify(200, JSON, _body({"code": 0, "data": {"isCheckin": True}}))
        self.assertIs(v.outcome, Outcome.ALREADY)

    def test_already_by_custom_code(self):
        v = classify(200, JSON, _body({"code": 40201, "msg": "repeat"}),
                     already_codes={40201})
        self.assertIs(v.outcome, Outcome.ALREADY)

    def test_already_english(self):
        v = classify(200, JSON, _body({"code": 1, "msg": "already signed today"}))
        self.assertIs(v.outcome, Outcome.ALREADY)

    def test_login_expired_real_response(self):
        """实测未登录响应"""
        v = classify(200, JSON, b'{"code":1000000002,"msg":"userId must be greater than or equal to 1 "}')
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)
        self.assertTrue(v.needs_relogin)
        self.assertFalse(v.retryable)

    def test_login_expired_chinese_msg(self):
        v = classify(200, JSON, _body({"code": 302, "msg": "请先登录"}))
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)

    def test_login_expired_401(self):
        v = classify(401, JSON, b'{}')
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)

    def test_login_expired_redirect_url(self):
        v = classify(200, JSON, b'{"code":0}',
                     final_url="https://passport.youzan.com/login?redirectUrl=xxx")
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)

    def test_login_expired_html(self):
        html = "<html><title>登录</title>验证码登录</html>".encode("utf-8")
        v = classify(200, "text/html; charset=utf-8", html)
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)

    def test_login_expired_login_url_in_data(self):
        v = classify(200, JSON, _body({
            "code": 1, "data": {"loginUrl": "https://passport.youzan.com/login"},
        }))
        self.assertIs(v.outcome, Outcome.LOGIN_EXPIRED)

    def test_missing_param_is_failed(self):
        v = classify(200, JSON, _body({"code": 12345, "msg": "缺少必要的参数 - checkinId"}))
        self.assertIs(v.outcome, Outcome.FAILED)
        self.assertFalse(v.retryable)

    def test_business_failure(self):
        v = classify(200, JSON, _body({"code": 500, "msg": "系统繁忙"}))
        self.assertIs(v.outcome, Outcome.FAILED)

    def test_is_success_false_without_reason_is_failed(self):
        v = classify(200, JSON, _body({
            "code": 0, "data": {"success": True, "list": [{"isSuccess": False}]},
        }))
        self.assertIs(v.outcome, Outcome.FAILED)

    def test_non_json_gateway_error_is_retryable(self):
        v = classify(502, "text/html", b"<h1>502 Bad Gateway</h1>")
        self.assertIs(v.outcome, Outcome.ERROR)
        self.assertTrue(v.retryable)

    def test_broken_json_is_retryable(self):
        v = classify(200, JSON, b"not a json")
        self.assertIs(v.outcome, Outcome.ERROR)
        self.assertTrue(v.retryable)

    def test_code0_fallback_success(self):
        v = classify(200, JSON, _body({"code": 0, "msg": "ok", "data": {}}))
        self.assertIs(v.outcome, Outcome.SUCCESS)


class TestActivity(unittest.TestCase):
    def test_guest_not_checked_in(self):
        state = parse_activity(200, JSON, _body({
            "code": 0, "msg": "ok",
            "data": {"isCheckin": False, "continuesDay": 0, "isOpen": True},
        }))
        self.assertFalse(state.login_expired)
        self.assertIs(state.is_checkin, False)
        self.assertEqual(state.continues_day, 0)

    def test_already_checked_in(self):
        state = parse_activity(200, JSON, _body({
            "code": 0, "data": {"isCheckin": True, "continuesDay": 7},
        }))
        self.assertIs(state.is_checkin, True)
        self.assertEqual(state.continues_day, 7)

    def test_login_expired(self):
        state = parse_activity(200, JSON, b'{"code":1000000002,"msg":"userId must be greater than or equal to 1 "}')
        self.assertTrue(state.login_expired)

    def test_bad_json(self):
        state = parse_activity(200, JSON, b"oops")
        self.assertIsNone(state.is_checkin)


class TestHeaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "DATA_DIR": self.tmp.name,
            "PUSHPLUS_ENABLED": "false",
            "CAPTURE_ENABLED": "false",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_cookie_header_ever(self):
        with patch.dict(os.environ, self.env):
            cfg = Config.load()
            _, params, headers = build(cfg, user_agent="UA-test")
        self.assertEqual(params["checkinId"], "6287727")
        self.assertEqual(params["kdt_id"], "149536603")
        self.assertNotIn("cookie", [k.lower() for k in headers])
        self.assertNotIn("authorization", [k.lower() for k in headers])
        self.assertEqual(headers["Referer"], cfg.result_page)
        self.assertEqual(headers["User-Agent"], "UA-test")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_capture_strips_secrets(self):
        raw = safe_capture_from_request(
            "GET",
            "https://club.ysjf.com/wscump/checkin/checkinV2.json"
            "?checkinId=6287727&kdt_id=149536603&app_id=wx123&access_token=SECRET123",
            {
                "Cookie": "KDTSESSIONID=ABCD; nobody_sign=EFGH",
                "Authorization": "Bearer SECRET",
                "Referer": "https://club.ysjf.com/wscump/checkin/result?kdt_id=149536603",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "UA-real",
            },
        )
        self.assertIn("app_id", raw["params"])
        self.assertNotIn("access_token", raw["params"])
        for key in raw["headers"]:
            self.assertNotIn(key.lower(), SENSITIVE_HEADER_KEYS)

    def test_build_merges_capture_and_extra(self):
        env = dict(self.env)
        env["EXTRA_QUERY"] = '{"extraFlag":"1"}'
        env["EXTRA_HEADERS"] = '{"Extra-Data":"{}"}'
        with patch.dict(os.environ, env):
            cfg = Config.load()
            capture = {
                "params": {"app_id": "wx-from-capture", "access_token": "SHOULD_BE_DROPPED"},
                "headers": {"User-Agent": "UA-captured", "Cookie": "SHOULD_BE_DROPPED"},
            }
            _, params, headers = build(cfg, user_agent="UA-default", capture=capture)
        self.assertEqual(params["app_id"], "wx-from-capture")
        self.assertEqual(params["extraFlag"], "1")
        self.assertNotIn("access_token", params)
        self.assertEqual(headers["User-Agent"], "UA-captured")
        self.assertEqual(headers["Extra-Data"], "{}")
        self.assertNotIn("cookie", [k.lower() for k in headers])

    def test_safe_url_for_log(self):
        line = safe_url_for_log("https://x/api", {"checkinId": "1", "access_token": "LEAK"})
        self.assertNotIn("LEAK", line)
        self.assertIn("access_token=***", line)


class TestRedaction(unittest.TestCase):
    def test_token_and_cookie_masked(self):
        text = "token=abc123SECRET cookie: KDTSESSIONID=YZ999888; nobody_sign=NNN777"
        out = scrub(text)
        self.assertNotIn("abc123SECRET", out)
        self.assertNotIn("YZ999888", out)
        self.assertNotIn("NNN777", out)
        self.assertIn("***REDACTED***", out)

    def test_config_secret_masked(self):
        with patch.dict(os.environ, {
            "DATA_DIR": tempfile.gettempdir(),
            "PUSHPLUS_TOKEN": "SUPERSECRET_TOKEN_VALUE",
        }):
            cfg = Config.load()
            out = scrub("push failed with SUPERSECRET_TOKEN_VALUE", cfg)
        self.assertNotIn("SUPERSECRET_TOKEN_VALUE", out)

    def test_login_url_masked(self):
        out = scrub("redirect to https://passport.youzan.com/login?redirectUrl=SENSITIVE")
        self.assertNotIn("SENSITIVE", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
