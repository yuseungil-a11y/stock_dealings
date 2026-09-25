"""stock_svr - 키움증권 REST API 기반 자동매매 서버모듈.

안전 원칙(DEV_SPEC 2절):
  * 기본은 실전 조회전용 관찰모드. 주문 전송은 Executor 의 단일 게이트를 통과할 때만 수행한다.
  * 앱키/시크릿키/토큰/DB 비밀번호는 로그·DB·화면 어디에도 평문으로 남기지 않는다.
"""

# 서버 버전 — 이 파일이 서버 버전의 유일한 원천이다.
# 직접 고치지 말고 `python tools/bump_version.py server patch|minor|major` 로 올린다(CHANGELOG.md 동시 갱신).
# 형식: MAJOR.MINOR.PATCH (SemVer). 웹 버전과는 독립적으로 관리한다.
__version__ = "1.16.0"
__released__ = "2026-09-25"
