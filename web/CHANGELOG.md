# 운영 웹 변경 이력

형식: [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) · 버전: [SemVer](https://semver.org/lang/ko/) `MAJOR.MINOR.PATCH`
버전은 `python tools/bump_version.py web patch|minor|major` 로만 올립니다(이 파일과 `lib/version.php` 를 함께 갱신).
배포는 `tools\deploy_web.ps1` (외부 https://pms.utinfo.co.kr/stock).

> 버전 체계: 최초 관리 버전부터 `1.x.y` (초기 개발 중 임시 번호 `0.x.y` 는 첫 자리만 1로 바꿔 이어서 표기). 운영 웹 버전은 서로 독립적으로 올립니다.

## [Unreleased]

## [1.1.1] - 2026-09-20
### 변경
- 웹 사이트 아이콘(파비콘·홈 화면 아이콘)을 서버 프로그램과 같은 주식 상승 이미지로 변경(기존 SVG 제거). `python tools/make_icon.py` 로 재생성

## [1.1.0] - 2026-09-20
초기 버전(읽기 전용 관제).

### 추가
- 로그인(잠금·CSRF·보안 헤더·세션 만료), 대시보드(30초 갱신)
- 계좌(예수금·잔고 추이, 보유종목 — 상장폐지 종목 실수익률 표시), 거래(체결·주문·거래내역·매매일지)
- 전략(알고리즘 현황, 파라미터·변경 이력, 신호 기록, Claude 판단)
- 거래 분석(신호→주문→체결 슬리피지·타임라인, CSV/JSON 내보내기), 이벤트·API 오류 보관 조회
- 시스템(서버 상태, 이벤트 로그, 내 정보/비밀번호 변경, 사용자 목록)
- 화면 우측 상단 버전 표시(웹/서버)
