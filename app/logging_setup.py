"""
日志：文件 + 控制台，并强制对所有输出做凭证脱敏。

任何包含 cookie / token / session / 密码字样的键值，以及配置中出现的明文凭证，
都会被替换为 ***REDACTED*** 后才允许写入日志文件或打印到终端。
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from typing import Iterable

from .config import Config

# 显式「键 = 敏感值」形式，例如 token=xxx、Cookie: xxx
_SENSITIVE_KEY = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|token|access[_-]?token|refresh[_-]?token|"
    r"session|sessionid|password|passwd|secret|sign|signature|sid)\b"
    r"(\s*[:=]\s*)(\S+)"
)
# Cookie 串里的会话类键值对，例如 KDTSESSIONID=xxx; nobody_sign=xxx
_COOKIE_PAIR = re.compile(
    r"(?i)\b([a-z0-9_\-]*(?:session|token|auth|passwd|secret|sid|uid|nobody_sign|kdtsessionid)[a-z0-9_\-]*)=([^;\s,]+)"
)
_LOGIN_URL = re.compile(r"(?i)(https?://[^\s'\"]*passport[^\s'\"]*)")


class RedactingFilter(logging.Filter):
    def __init__(self, extra_secrets: Iterable[str] = ()):
        super().__init__()
        self._secrets = [s for s in extra_secrets if s and len(s) >= 6]

    def _scrub(self, text: str) -> str:
        text = _SENSITIVE_KEY.sub(
            lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text
        )
        text = _COOKIE_PAIR.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
        text = _LOGIN_URL.sub("https://passport.youzan.com/login?***REDACTED***", text)
        for secret in self._secrets:
            text = text.replace(secret, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: self._scrub(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        self._scrub(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            record.msg = "***LOG REDACTED (scrub error)***"
            record.args = ()
        return True


def scrub(text: str, cfg: Config | None = None) -> str:
    """对外提供的单次脱敏工具（用于入库前清洗）。"""
    filt = RedactingFilter(cfg.secret_values() if cfg else ())
    return filt._scrub(str(text))


def setup_logging(cfg: Config) -> logging.Logger:
    cfg.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    flt = RedactingFilter(cfg.secret_values())
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        cfg.log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(flt)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(flt)

    root.addHandler(file_handler)
    root.addHandler(console)

    # 压低第三方库噪音
    for name in ("apscheduler", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger("youzan")
