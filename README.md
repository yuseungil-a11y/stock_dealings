# stock_dealings — 키움증권 REST API 자동매매 시스템

| 구성 | 위치 | 설명 |
|---|---|---|
| DB | `db/` | MariaDB `stock_dealings` (25개 테이블). `schema.sql`, `seed.sql`, `setup_db.py` |
| 서버모듈 `stock_svr` | `server/` | Python. 키움 REST/WS 연동, 알고리즘 엔진, Windows GUI(연결상태 아이콘·이벤트 로그·알고리즘/파라미터 편집), 로그 7일 보관 |
| 웹 | `web/` → `D:\xampp\htdocs\stock` | xampp(Apache+PHP). 로그인·보유종목·잔고·거래내역·관제. 외부 `https://pms.utinfo.co.kr/stock` |
| 도구 | `tools/` | 웹 배포, 데모 데이터, 테이블 문서 생성 |
| 문서 | `docs/` | `DEV_SPEC.md`(개발 명세), `table_design.md`(테이블 설계서, 자동생성) |

## 안전 원칙
* 보유 앱키는 **실전투자용** → 서버는 기본 **조회 전용 관찰모드**(`trading_mode=real`, `order_enabled=0`): 신호만 기록하고 주문은 전송하지 않는다.
* 주문 전송 조건: `order_enabled=1` **그리고** (모의 모드 **또는** `real_trading_confirm=1`). 실전 주문을 켤 때는 서버 UI 에서 `REAL` 을 직접 입력해야 한다.
* 비밀값(앱키·시크릿키·DB/웹 비밀번호)은 `config.local.*` 및 키 파일에만 있으며 git 에서 제외된다(`.gitignore`).

## 처음 설치 / 재구축
```
set STOCK_DB_ROOT_PW=<MariaDB root 비밀번호>
set STOCK_ADMIN_USER=<웹 관리자 ID>
set STOCK_ADMIN_PW=<웹 관리자 비밀번호>
python db\setup_db.py          :: 스키마·시드·DB계정·설정파일·관리자 생성 (재실행 안전)
python tools\gen_table_doc.py  :: 테이블 설계서 재생성
```
서버 실행/웹 배포 방법은 `server/README.md`, `web/README.md` 참조.
