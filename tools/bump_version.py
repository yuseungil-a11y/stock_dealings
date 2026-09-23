"""서버/운영웹 버전 관리 도구 (SemVer MAJOR.MINOR.PATCH).

  python tools/bump_version.py show
  python tools/bump_version.py server patch|minor|major|X.Y.Z  [-m "변경 요약"]...
  python tools/bump_version.py web    patch|minor|major|X.Y.Z  [-m "변경 요약"]...

동작
  * 버전 원천 파일을 갱신한다.
      server : server/stock_svr/__init__.py   (__version__, __released__)
      web    : web/lib/version.php            (STOCK_WEB_VERSION, STOCK_WEB_RELEASED)
  * 각 CHANGELOG.md 의 `## [Unreleased]` 내용을 `## [X.Y.Z] - 날짜` 로 확정하고 새 Unreleased 를 만든다.
    `-m` 로 준 문구는 확정되는 섹션 맨 위 "### 변경" 아래에 추가된다.
  * 아무것도 커밋/태그하지 않는다. 끝에 권장 절차(빌드·배포·태그)를 출력한다.
    (git 작업은 형상관리 담당(Newton)이 표준 커밋 형식으로 수행)

버전 올리기 기준
  patch : 버그 수정·문구/스타일·안전한 내부 개선   (0.1.0 -> 0.1.1)
  minor : 새 기능/새 화면/새 파라미터 (하위 호환)    (0.1.0 -> 0.2.0)
  major : 호환이 깨지는 변경, 또는 실계좌 검증 완료 후 1.0.0
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
TARGETS = {
    "server": {
        "file": ROOT / "server" / "stock_svr" / "__init__.py",
        "ver_re": re.compile(r'(?m)^(__version__\s*=\s*")(\d+\.\d+\.\d+)(")'),
        "date_re": re.compile(r'(?m)^(__released__\s*=\s*")([\d-]+)(")'),
        "changelog": ROOT / "server" / "CHANGELOG.md",
        "label": "서버(stock_svr)",
        "tag": "server",
    },
    "web": {
        "file": ROOT / "web" / "lib" / "version.php",
        "ver_re": re.compile(r"(?m)^(const STOCK_WEB_VERSION\s*=\s*')(\d+\.\d+\.\d+)(';)"),
        "date_re": re.compile(r"(?m)^(const STOCK_WEB_RELEASED\s*=\s*')([\d-]+)(';)"),
        "changelog": ROOT / "web" / "CHANGELOG.md",
        "label": "운영 웹",
        "tag": "web",
    },
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def current(name: str) -> str:
    t = TARGETS[name]
    m = t["ver_re"].search(t["file"].read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"버전 문자열을 찾을 수 없습니다: {t['file']}")
    return m.group(2)


def next_version(cur: str, how: str) -> str:
    if SEMVER.match(how):
        return how
    a, b, c = (int(x) for x in cur.split("."))
    if how == "patch":
        return f"{a}.{b}.{c + 1}"
    if how == "minor":
        return f"{a}.{b + 1}.0"
    if how == "major":
        return f"{a + 1}.0.0"
    raise SystemExit("patch | minor | major | X.Y.Z 중 하나여야 합니다.")


def vkey(v: str) -> tuple[int, int, int]:
    return tuple(int(x) for x in v.split("."))  # type: ignore[return-value]


def build_changelog(path: Path, new: str, today: str, notes: list[str]) -> str:
    """확정된 CHANGELOG 전체 텍스트를 만들어 돌려준다(파일은 쓰지 않음)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?m)^## \[Unreleased\]\s*\n", text)
    if not m:
        raise SystemExit(f"CHANGELOG 에 '## [Unreleased]' 절이 없습니다: {path}")
    head = text[: m.end()]
    rest = text[m.end():]
    nxt = re.search(r"(?m)^## \[", rest)
    unreleased_body = rest[: nxt.start()] if nxt else rest
    tail = rest[nxt.start():] if nxt else ""
    body = unreleased_body.strip("\n")
    if notes:
        note_block = "### 변경\n" + "\n".join(f"- {n}" for n in notes)
        body = note_block + ("\n" + body if body else "")
    if not body.strip():
        print("  (Unreleased 가 비어 있고 -m 도 없어 변경 내용이 비어 있습니다 - CHANGELOG 를 직접 보완하세요)")
    section = f"## [{new}] - {today}\n{body}\n" if body else f"## [{new}] - {today}\n"
    return head + "\n" + section + "\n" + tail.lstrip("\n")


def bump(name: str, how: str, notes: list[str]) -> None:
    t = TARGETS[name]
    cur = current(name)
    new = next_version(cur, how)
    if vkey(new) <= vkey(cur):
        raise SystemExit(f"새 버전({new})은 현재({cur})보다 커야 합니다.")
    today = dt.date.today().isoformat()
    src = t["file"].read_text(encoding="utf-8")
    src = t["ver_re"].sub(lambda m: m.group(1) + new + m.group(3), src, count=1)
    if t["date_re"].search(src):
        src = t["date_re"].sub(lambda m: m.group(1) + today + m.group(3), src, count=1)
    log_text = build_changelog(t["changelog"], new, today, notes)   # 둘 다 계산이 끝난 뒤에만 쓴다(절반 상태 방지)
    t["file"].write_text(src, encoding="utf-8")
    t["changelog"].write_text(log_text, encoding="utf-8")
    print(f"{t['label']}: {cur} -> {new}  ({today})")
    print(f"  갱신: {t['file'].relative_to(ROOT)}, {t['changelog'].relative_to(ROOT)}")
    print("  다음 단계:")
    if name == "server":
        print("   1) powershell -ExecutionPolicy Bypass -File tools\\build_server.ps1 -WithLocalConfig -StopRunning -Launch")
    else:
        print("   1) powershell -ExecutionPolicy Bypass -File tools\\deploy_web.ps1")
    print("   2) 커밋(형상관리 담당): type 은 변경 성격에 맞게 선택, 이슈개요에 버전 명시")
    print(f"   3) 태그: {t['tag']}-v{new}   (예: git tag {t['tag']}-v{new} && git push origin {t['tag']}-v{new})")


def main() -> int:
    ap = argparse.ArgumentParser(description="서버/운영웹 버전 관리")
    ap.add_argument("target", choices=["show", "server", "web"])
    ap.add_argument("how", nargs="?", help="patch | minor | major | X.Y.Z")
    ap.add_argument("-m", "--message", action="append", default=[], help="변경 요약(여러 번 지정 가능)")
    a = ap.parse_args()
    if a.target == "show":
        for n in ("server", "web"):
            print(f"{TARGETS[n]['label']:14} v{current(n)}")
        return 0
    if not a.how:
        ap.error("patch | minor | major | X.Y.Z 를 지정하세요.")
    notes = [m for m in a.message if m.strip()]  # 빈 문자열(-m "")은 무시 - 빈 "### 변경" 절이 남는 걸 막는다
    bump(a.target, a.how, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
