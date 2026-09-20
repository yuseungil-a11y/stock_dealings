"""설정 파일(config.local.ini) 로딩.

값에 `%` 가 포함될 수 있으므로 ConfigParser(interpolation=None) 을 사용한다.
앱키/시크릿키는 설정파일에 직접 쓰지 않고 **파일 경로**로 지정하며, 필요한 시점에만 읽는다.
"""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# server/stock_svr/config.py -> server/
# PyInstaller 로 빌드된 exe 에서는 __file__ 이 임시 압축해제 폴더를 가리키므로 exe 가 있는 폴더를 기준으로 한다
# (config/, logs/ 는 exe 옆에 둔다).
if getattr(sys, "frozen", False):
    SERVER_DIR = Path(sys.executable).resolve().parent
else:
    SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SERVER_DIR / "config" / "config.local.ini"
EXAMPLE_CONFIG = SERVER_DIR / "config" / "config.example.ini"


class ConfigError(RuntimeError):
    pass


@dataclass
class DbConfig:
    host: str = "127.0.0.1"
    port: int = 4406
    name: str = "stock_dealings"
    user: str = "stock_svr"
    password: str = field(default="", repr=False)

    def __repr__(self) -> str:  # 비밀번호 노출 방지
        return f"DbConfig(host={self.host!r}, port={self.port}, name={self.name!r}, user={self.user!r}, password='***')"


@dataclass
class KiwoomConfig:
    appkey_file_real: str = ""
    secretkey_file_real: str = ""
    appkey_file_mock: str = ""
    secretkey_file_mock: str = ""
    domain_real: str = "https://api.kiwoom.com"
    domain_mock: str = "https://mockapi.kiwoom.com"
    ws_real: str = "wss://api.kiwoom.com:10000/api/dostk/websocket"
    ws_mock: str = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    min_interval_sec_real: float = 0.25
    min_interval_sec_mock: float = 1.1
    http_timeout_sec: float = 10.0

    def domain(self, env: str) -> str:
        return self.domain_real if env == "real" else self.domain_mock

    def ws_url(self, env: str) -> str:
        return self.ws_real if env == "real" else self.ws_mock

    def min_interval(self, env: str) -> float:
        return self.min_interval_sec_real if env == "real" else self.min_interval_sec_mock

    def key_files(self, env: str) -> tuple[str, str]:
        if env == "real":
            return self.appkey_file_real, self.secretkey_file_real
        return self.appkey_file_mock, self.secretkey_file_mock

    def read_keys(self, env: str) -> tuple[str, str]:
        """앱키/시크릿키를 파일에서 읽는다. 반환값은 절대 로그에 남기지 않는다."""
        ak_path, sk_path = self.key_files(env)
        if not ak_path or not sk_path:
            raise ConfigError(f"[kiwoom] {env} 앱키/시크릿키 파일 경로가 설정되지 않았습니다.")
        try:
            appkey = Path(ak_path).read_text(encoding="utf-8").strip()
            secretkey = Path(sk_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"키 파일을 읽을 수 없습니다: {exc}") from exc
        if not appkey or not secretkey:
            raise ConfigError("키 파일이 비어 있습니다.")
        return appkey, secretkey


@dataclass
class AnthropicConfig:
    """Claude 거부권 필터(`claude_advisor`) 용 설정.

    키움 키와 같은 방식으로 **파일 경로만** 설정하고 값은 필요한 시점에 읽는다.
    모델/확신도/타임아웃 등 운영 파라미터는 DB(`algorithm_param_def`)에서 관리한다.
    """

    apikey_file: str = ""
    max_retries: int = 1

    @property
    def configured(self) -> bool:
        return bool(self.apikey_file)

    def read_key(self) -> str:
        """API 키를 파일에서 읽는다. 반환값은 절대 로그/DB/화면에 남기지 않는다."""
        if not self.apikey_file:
            raise ConfigError("[anthropic] apikey_file 경로가 설정되지 않았습니다.")
        try:
            key = Path(self.apikey_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"Anthropic 키 파일을 읽을 수 없습니다: {exc}") from exc
        if not key:
            raise ConfigError("Anthropic 키 파일이 비어 있습니다.")
        return key


@dataclass
class LoggingConfig:
    dir: str = "logs"
    level: str = "INFO"
    retention_days: int = 7

    @property
    def path(self) -> Path:
        p = Path(self.dir)
        return p if p.is_absolute() else (SERVER_DIR / p)


@dataclass
class AppConfig:
    db: DbConfig = field(default_factory=DbConfig)
    kiwoom: KiwoomConfig = field(default_factory=KiwoomConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: Path | None = None


def _get(section, key, default):
    if section is None:
        return default
    val = section.get(key, None)
    if val is None or val == "":
        return default
    return val


def _to_float(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_int(val, default: int) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise ConfigError(
            f"설정 파일이 없습니다: {cfg_path}\n{EXAMPLE_CONFIG} 를 복사해 config.local.ini 를 만드세요."
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(cfg_path, encoding="utf-8")

    db_s = parser["db"] if parser.has_section("db") else None
    kw_s = parser["kiwoom"] if parser.has_section("kiwoom") else None
    an_s = parser["anthropic"] if parser.has_section("anthropic") else None
    lg_s = parser["logging"] if parser.has_section("logging") else None

    db = DbConfig(
        host=_get(db_s, "host", "127.0.0.1"),
        port=_to_int(_get(db_s, "port", 4406), 4406),
        name=_get(db_s, "name", "stock_dealings"),
        user=_get(db_s, "user", "stock_svr"),
        password=_get(db_s, "password", ""),
    )
    if db.user == "root":
        raise ConfigError("root 계정은 사용할 수 없습니다. stock_svr 계정을 사용하세요.")

    kiwoom = KiwoomConfig(
        appkey_file_real=_get(kw_s, "appkey_file_real", ""),
        secretkey_file_real=_get(kw_s, "secretkey_file_real", ""),
        appkey_file_mock=_get(kw_s, "appkey_file_mock", ""),
        secretkey_file_mock=_get(kw_s, "secretkey_file_mock", ""),
        domain_real=_get(kw_s, "domain_real", "https://api.kiwoom.com"),
        domain_mock=_get(kw_s, "domain_mock", "https://mockapi.kiwoom.com"),
        ws_real=_get(kw_s, "ws_real", "wss://api.kiwoom.com:10000/api/dostk/websocket"),
        ws_mock=_get(kw_s, "ws_mock", "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"),
        min_interval_sec_real=_to_float(_get(kw_s, "min_interval_sec_real", 0.25), 0.25),
        min_interval_sec_mock=_to_float(_get(kw_s, "min_interval_sec_mock", 1.1), 1.1),
        http_timeout_sec=_to_float(_get(kw_s, "http_timeout_sec", 10), 10.0),
    )
    anthropic_cfg = AnthropicConfig(
        apikey_file=_get(an_s, "apikey_file", ""),
        # 전체 지연이 timeout_sec 를 넘지 않도록 SDK 재시도는 낮게 유지한다
        max_retries=max(0, min(3, _to_int(_get(an_s, "max_retries", 1), 1))),
    )
    logging_cfg = LoggingConfig(
        dir=_get(lg_s, "dir", "logs"),
        level=str(_get(lg_s, "level", "INFO")).upper(),
        retention_days=_to_int(_get(lg_s, "retention_days", 7), 7),
    )
    return AppConfig(db=db, kiwoom=kiwoom, anthropic=anthropic_cfg, logging=logging_cfg,
                     source_path=cfg_path)
