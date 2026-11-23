"""
集中配置。

安全约定：
- 所有可能包含凭证的值（Cookie / Token / 密码）一律不出现在代码与仓库里。
- 运行期只从环境变量 / .env 读取；.env 与 data/ 均被 .gitignore 排除。
- 浏览器登录态保存在持久化 Profile 目录，绝不打印、绝不写日志。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """极简 .env 加载器（不覆盖已存在的环境变量）。"""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except (ValueError, TypeError):
        return default


def _b(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _json_dict(key: str) -> dict:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Config:
    # 运行期目录
    data_dir: Path
    db_path: Path
    profile_dir: Path
    log_dir: Path
    capture_path: Path

    # 站点与接口
    base_url: str
    checkin_api: str
    activity_api: str
    result_page: str
    checkin_id: str
    kdt_id: str
    extra_query: dict
    extra_headers: dict

    # 调度
    timezone: str
    tz: ZoneInfo
    schedule_hour: int
    schedule_minute: int
    run_on_startup: bool

    # 浏览器
    headless: bool
    browser_channel: str
    login_wait_seconds: int

    # 请求与重试
    request_timeout: int
    max_retry: int
    retry_backoff: int
    precheck_enabled: bool
    capture_enabled: bool

    # 网页控制台
    web_host: str
    web_port: int

    # 通知
    pushplus_enabled: bool
    pushplus_token: str
    pushplus_topic: str
    pushplus_channel: str
    pushplus_template: str
    notify_on_success: bool
    notify_on_already: bool
    notify_on_failure: bool
    login_hint: str

    @classmethod
    def load(cls) -> "Config":
        data_dir = Path(_s("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
        tz_name = _s("TIMEZONE", "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz_name, tz = "Asia/Shanghai", ZoneInfo("Asia/Shanghai")

        kdt_id = _s("KDT_ID", "149536603")
        checkin_id = _s("CHECKIN_ID", "6287727")
        base_url = _s("BASE_URL", "https://club.ysjf.com").rstrip("/")
        result_page = _s(
            "RESULT_PAGE",
            f"{base_url}/wscump/checkin/result?kdt_id={kdt_id}",
        )

        return cls(
            data_dir=data_dir,
            db_path=Path(_s("DB_PATH", str(data_dir / "checkin.db"))).expanduser().resolve(),
            profile_dir=Path(_s("PROFILE_DIR", str(data_dir / "browser_profile"))).expanduser().resolve(),
            log_dir=Path(_s("LOG_DIR", str(data_dir / "logs"))).expanduser().resolve(),
            capture_path=Path(_s("CAPTURE_PATH", str(data_dir / "captured_request.json"))).expanduser().resolve(),
            base_url=base_url,
            checkin_api=_s("CHECKIN_API", f"{base_url}/wscump/checkin/checkinV2.json"),
            activity_api=_s(
                "ACTIVITY_API",
                f"{base_url}/wscump/checkin/get_activity_by_yzuid_v2.json",
            ),
            result_page=result_page,
            checkin_id=checkin_id,
            kdt_id=kdt_id,
            extra_query=_json_dict("EXTRA_QUERY"),
            extra_headers=_json_dict("EXTRA_HEADERS"),
            timezone=tz_name,
            tz=tz,
            schedule_hour=_i("SCHEDULE_HOUR", 8),
            schedule_minute=_i("SCHEDULE_MINUTE", 0),
            run_on_startup=_b("RUN_ON_STARTUP", False),
            headless=_b("HEADLESS", True),
            browser_channel=_s("BROWSER_CHANNEL", ""),
            login_wait_seconds=_i("LOGIN_WAIT_SECONDS", 600),
            web_host=_s("WEB_HOST", "127.0.0.1"),
            web_port=_i("WEB_PORT", 8787),
            request_timeout=_i("REQUEST_TIMEOUT", 30),
            max_retry=_i("MAX_RETRY", 3),
            retry_backoff=_i("RETRY_BACKOFF", 30),
            precheck_enabled=_b("PRECHECK_ENABLED", True),
            capture_enabled=_b("CAPTURE_ENABLED", True),
            pushplus_enabled=_b("PUSHPLUS_ENABLED", True),
            pushplus_token=_s("PUSHPLUS_TOKEN", ""),
            pushplus_topic=_s("PUSHPLUS_TOPIC", ""),
            pushplus_channel=_s("PUSHPLUS_CHANNEL", "wechat"),
            pushplus_template=_s("PUSHPLUS_TEMPLATE", "html"),
            notify_on_success=_b("NOTIFY_ON_SUCCESS", True),
            notify_on_already=_b("NOTIFY_ON_ALREADY", False),
            notify_on_failure=_b("NOTIFY_ON_FAILURE", True),
            login_hint=_s("LOGIN_HINT", "请重新运行 login 命令完成网页登录"),
        )

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.profile_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)

    def secret_values(self) -> list[str]:
        """需要被日志脱敏过滤掉的明文值。"""
        values = [self.pushplus_token]
        for key in (
            "PUSHPLUS_TOKEN",
            "COOKIE",
            "TOKEN",
            "PASSWORD",
            "ACCESS_TOKEN",
            "SESSION",
        ):
            v = os.environ.get(key, "")
            if v:
                values.append(v)
        return [v for v in values if len(v) >= 6]


def today_str(cfg: Config) -> str:
    """北京时间业务日期 YYYY-MM-DD。"""
    return datetime.now(cfg.tz).strftime("%Y-%m-%d")


def now_str(cfg: Config) -> str:
    """北京时间时间戳字符串。"""
    return datetime.now(cfg.tz).strftime("%Y-%m-%d %H:%M:%S")
