"""버전 표시/관리 규약 테스트 (서버 v0.1.0~ / 웹은 하트비트 메시지에서 서버 버전을 읽는다)."""
import re
from pathlib import Path

import stock_svr
from stock_svr import __released__, __version__

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# 웹(lib/version.php app_server_version)이 쓰는 정규식과 동일해야 한다
WEB_RE = re.compile(r"\bv(\d+\.\d+\.\d+)\b")
ROOT = Path(__file__).resolve().parents[2]


def test_version_is_semver_and_dated():
    assert SEMVER.match(__version__), __version__
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", __released__), __released__
    assert stock_svr.__version__ == __version__


def test_heartbeat_message_format_is_parsable_by_web():
    msg = f"heartbeat 09:55:34 · v{__version__}"
    m = WEB_RE.search(msg)
    assert m and m.group(1) == __version__


def test_runner_heartbeat_includes_version():
    src = (Path(stock_svr.__file__).parent / "engine" / "runner.py").read_text(encoding="utf-8")
    assert "· v{__version__}" in src


def test_changelog_has_current_version_section():
    log = (ROOT / "server" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in log, "CHANGELOG.md 에 현재 서버 버전 절이 없습니다 (bump_version.py 로 올리세요)"
    assert "## [Unreleased]" in log


def test_ui_shows_version_top_right():
    src = (Path(stock_svr.__file__).parent / "ui" / "app.py").read_text(encoding="utf-8")
    assert 'text=f"v{__version__}"' in src and ".pack(side=\"right\"" in src
    assert 'self.title(f"stock_svr v{__version__}' in src


# --- 회귀: App.account_id 는 '메서드'다. 함수 자체를 넘기면 총자산 조회가 조용히 실패한다(유효 한도 미리보기 "확인 불가") ---
def test_start_dialog_passes_account_id_value_not_method():
    src = (Path(stock_svr.__file__).parent / "ui" / "app.py").read_text(encoding="utf-8")
    assert "collect_start_info(self.db, self.account_id())" in src
    assert "collect_start_info(self.db, self.account_id)" not in src.replace("self.account_id())", "")


def test_limit_preview_asset_works_with_method_style_account_id():
    from types import SimpleNamespace

    from stock_svr.ui.algo_tab import AlgoTab

    class _Db:
        def latest_balance(self, account_id):
            assert isinstance(account_id, int), f"계좌 번호(int)가 와야 한다: {account_id!r}"
            return {"prsm_dpst_aset_amt": 287200}

    class _RealLikeApp:            # 실제 App 처럼 account_id 가 메서드
        db = _Db()

        def account_id(self):
            return 2

    stub = SimpleNamespace(app=_RealLikeApp())
    assert AlgoTab._asset(stub) == 287200

    class _ValueStyleApp:          # 값 형태도 허용
        db = _Db()
        account_id = 2

    assert AlgoTab._asset(SimpleNamespace(app=_ValueStyleApp())) == 287200

    class _NoAccountApp:
        db = _Db()

        def account_id(self):
            return None

    assert AlgoTab._asset(SimpleNamespace(app=_NoAccountApp())) is None


# --- 서버 프로그램 아이콘(주식 상승 이미지) ---
def test_icon_assets_exist_and_are_valid():
    from PIL import Image  # 개발/테스트 환경에서만 사용(런타임 의존성 아님)

    assets = ROOT / "server" / "assets"
    ico, png = assets / "stock_svr.ico", assets / "stock_svr.png"
    assert ico.exists() and png.exists(), "python tools/make_icon.py 로 아이콘을 생성하세요"
    with Image.open(ico) as im:
        sizes = set(im.info.get("sizes", []))
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes, sizes
    with Image.open(png) as im:
        assert im.size == (256, 256)


def test_spec_bundles_icon_and_sets_exe_icon():
    spec = (ROOT / "server" / "stock_svr.spec").read_text(encoding="utf-8")
    assert "icon='assets/stock_svr.ico'" in spec
    assert "('assets/stock_svr.ico', 'assets')" in spec


def test_app_applies_window_icon_and_resolves_paths():
    from stock_svr.ui import app as app_mod

    assert app_mod._asset_path("stock_svr.ico").exists()
    src = (Path(stock_svr.__file__).parent / "ui" / "app.py").read_text(encoding="utf-8")
    assert "apply_window_icon(self)" in src


# --- 장운영구분(WS 0s 필드 215) 판단 버그 수정 회귀 ---
# 215 는 코스피/코스닥 "본장" 상태(0/2/3/4/8/9)뿐 아니라 NXT·선물옵션·시간외
# 알림(a/b/g/h/o/s/e/f/P~V 등)도 같은 필드로 온다. 09:00 "3"(장시작) 직후
# NXT "R"(메인마켓 시작 알림) 이 오면 그 값으로 상태를 덮어써서, 그 뒤로는
# market_open() 이 계속 False 를 반환해 하루 종일 신규 신호가 끊긴다
# (2026-09-22 실서버에서 실제 관측된 장애 - 자동거래는 ON 인데 신호가 하나도 안 남).
def test_main_market_codes_cover_open_and_close_only():
    from stock_svr.engine.runner import MAIN_MARKET_CODES
    assert MAIN_MARKET_CODES == {"0", "2", "3", "4", "8", "9"}
    for code in ("a", "b", "g", "h", "o", "s", "e", "f", "P", "Q", "R", "S", "T", "U", "V"):
        assert code not in MAIN_MARKET_CODES, code


def test_market_open_survives_unrelated_215_codes_after_open():
    """09:00 장시작(3) 이후 NXT/선옵 등 무관한 215 코드가 와도 market_open 이 유지되어야 한다."""
    from unittest.mock import patch

    from stock_svr.config import AppConfig
    from stock_svr.engine.runner import Engine

    from conftest import FakeDb

    eng = Engine(AppConfig(), FakeDb())

    with patch("stock_svr.engine.runner.is_market_open", return_value=True):
        eng._on_real("0s", "", {"215": "3"})
        assert eng.market_open() is True

        for code in ("R", "o", "P", "Q"):    # NXT/선옵 알림 - 본장과 무관
            eng._on_real("0s", "", {"215": code})
            assert eng.market_open() is True, f"215={code} 로 장중 상태가 무너지면 안 된다"

        eng._on_real("0s", "", {"215": "4"})  # 실제 장마감은 여전히 반영되어야 한다
        assert eng.market_open() is False
