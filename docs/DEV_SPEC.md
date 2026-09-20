# 키움증권 자동매매 프로젝트 — 개발 명세 (제우스 작성, 2026-09-19)

원천 자료: `개발정보.txt`(요구사항), `키움증권_자동매매_조사보고_20260919.html`(조사보고), `kiwoom-rest-api-spec.json`(345개 API 스펙),
`docs/_api_catalog_raw.txt`(API 목록), `docs/_api_digest.txt`(핵심 API 요청/응답 필드 요약 — **구현 시 우선 참조**), `db/schema.sql`, `db/seed.sql`.
(`kiwoom-rest-api-spec.json` 은 UTF-8 JSON, 최상위 키=API ID, 값=`{apiId, apiNm, url, requestIo[], responseIo[], requestExample, responseExample}`.
 `errorCodeList` 키에 오류코드 표. PowerShell 5.1 ConvertFrom-Json 은 `0G/0g` 중복키로 실패하므로 **Python json 사용**.)

## 1. 경로 · 환경
| 항목 | 값 |
|---|---|
| 프로젝트 루트(소스) | `D:\claude_stock_dealings` |
| 서버모듈 소스 | `D:\claude_stock_dealings\server` (모듈명 **stock_svr**) |
| 웹 소스 | `D:\claude_stock_dealings\web` → **배포 경로 `D:\xampp\htdocs\stock`** (외부 URL `https://pms.utinfo.co.kr/stock`, 내부 `http://localhost/stock/`) |
| DB | MariaDB 10.11 / `127.0.0.1:4406` / DB `stock_dealings` (스키마·시드·계정 **이미 구축 완료**) |
| Python | 3.13 (`C:\Users\UT\AppData\Local\Programs\Python\Python313\python.exe`), pip 사용 가능, httpx·pymysql 설치됨 |
| PHP | `D:\xampp\php\php.exe` (xampp, PDO/pdo_mysql/session/openssl/mbstring/curl 활성). Apache 는 **이미 기동 중(80/443)** |
| Node | **없음** → 프런트 빌드 도구 사용 금지. 웹은 PHP + 바닐라 JS/CSS |
| OS | Windows 11, PowerShell 5.1 / Git Bash. **파일 인코딩은 UTF-8(BOM 없음)** |

DB 접속정보는 코드에 쓰지 말고 설정파일에서 읽는다(아래 3절). 사용 가능한 계정: `stock_svr`(서버, DML 권한), `stock_web`(웹, SELECT 전용 + 로그인 관련 최소 쓰기).
**root 계정은 사용하지 않는다.**

## 2. 절대 원칙 (안전)
1. **개발/테스트 중 실제 주문 API(`kt10000~kt10003`, `kt10006~9`, `kt50000~3`, `ust2*`)를 호출하지 않는다.** 주문 코드는 가짜(fake) 클라이언트로만 테스트한다.
2. 보유 키는 **실전투자 계좌용**이다(모의 키 없음). 따라서 서버는 기본이 **실전 조회전용 관찰모드**: 계좌/시세/순위/차트 조회와 신호 기록만 하고 주문은 전송하지 않는다.
3. 주문 전송 게이트(코드와 UI 양쪽에서 강제, 우회 경로 없음):
   `can_send_order = (system_setting.order_enabled == '1') AND (trading_mode == 'mock' OR real_trading_confirm == '1')`
   하나라도 거짓이면 `orders` 에 `is_dry_run=1, status='SIGNAL_ONLY'` 로만 기록.
4. 비밀값(앱키·시크릿키·토큰·DB 비밀번호·웹 비밀번호)은 **로그/DB/화면/커밋 어디에도 평문으로 남기지 않는다.** 앱키·시크릿키는 설정파일이 가리키는 파일에서 읽는다. 토큰은 메모리에만 둔다.
5. `git commit/push` 는 하지 않는다(형상관리는 Newton 전담·사용자 확인 필요).
6. `.gitignore` 에 비밀/원본 파일이 이미 제외돼 있다. 새 비밀 파일을 만들면 반드시 `.gitignore` 에 추가.

## 3. 설정 파일 (이미 생성됨, git 제외)
* 서버: `server/config/config.local.ini` — `[db]`, `[kiwoom]`(앱키/시크릿키 **파일경로**, 도메인, 호출간격), `[logging]` (예시: `config.example.ini`). 값에 `%` 가 있을 수 있으므로 `ConfigParser(interpolation=None)`.
* 웹: `web/config/config.local.php` — `return ['db'=>['host','port','name','user','password']]` (**`stock_web` 계정**).
  웹 배포 시 이 파일은 **웹루트 밖 `D:\xampp\stock_private\config.local.php`** 로 복사되며 앱은 이 경로를 우선 사용한다(없으면 소스 상대경로 fallback — 개발용).
* 런타임 스위치는 DB `system_setting`(trading_mode / order_enabled / real_trading_confirm / poll_interval_sec / log_retention_days). 서버는 매 주기 DB 값을 다시 읽는다(UI 편집이 즉시 반영).

## 4. 키움 REST API 핵심 (스펙 확인 완료)
* 도메인: 실전 `https://api.kiwoom.com`, 모의 `https://mockapi.kiwoom.com`, WS `wss://api.kiwoom.com:10000/api/dostk/websocket` (모의는 mockapi). 모든 TR 은 `POST`, JSON.
* 인증 `au10001` `POST /oauth2/token` body `{grant_type:'client_credentials', appkey, secretkey}` → `{token, token_type, expires_dt('YYYYMMDDHHMMSS'), return_code, return_msg}`. 폐기 `au10002 /oauth2/revoke`.
  * 실측: 실전 도메인에 실전 키로 발급 성공(만료 약 24h). 모의 도메인에 실전 키 → `return_code=2, 8030(투자구분 불일치)`.
* 호출 헤더: `authorization: Bearer <token>`, `api-id: <TR id>`, (연속조회) `cont-yn`, `next-key`. 응답 헤더 `cont-yn=Y` 이면 `next-key` 를 다음 요청 헤더에 넣어 반복. 응답 body 에 `return_code`(0=정상), `return_msg`.
* 오류: `errorCodeList`(예 1700=허용 요청 수 초과 → 백오프 후 재시도). HTTP 200 이어도 `return_code != 0` 이면 실패로 처리.
* 값 형식: 숫자는 **부호/0패딩 문자열**(`"+60700"`, `"-500"`, `"000012345"`), 종목코드에 `A` 접두가 붙을 수 있음 → 파서 유틸 필수. 비율은 문자열 소수.
* 거래소구분: 주문 `dmst_stex_tp` = `KRX|NXT|SOR`, 종목코드 접미(`_NX`,`_AL`)로 거래소 지정. 기본 KRX.
* 주문 `kt10000`(매수)/`kt10001`(매도): `{dmst_stex_tp, stk_cd, ord_qty, ord_uv, trde_tp, cond_uv}`; `trde_tp` 0=보통(지정가) 3=시장가 6=최유리 …; 응답 `ord_no`. 정정 `kt10002`, 취소 `kt10003`.
* 계좌: `ka00001`(계좌번호), `kt00001`(예수금, `qry_tp`=2 일반/3 추정), `kt00018`(계좌평가잔고, `qry_tp`=1 합산/2 개별 → 보유종목 리스트 `acnt_evlt_remn_indv_tot`), `kt00004`(계좌평가현황), `ka10075`(미체결), `ka10076`(체결), `kt00015`(위탁종합거래내역: `strt_dt,end_dt,tp,...`), `ka10170`(당일매매일지 `base_dt`), `ka10077`(당일실현손익상세).
* 시세/순위: `ka10027`(전일대비등락률상위, /api/dostk/rkinfo), `ka10023`(거래량급증), `ka10001`(주식기본정보), `ka10081`(일봉, `base_dt`,`upd_stkpc_tp`), `ka10099`(종목리스트 `mrkt_tp` 0=코스피 10=코스닥).
* WebSocket: 접속 후 `{"trnm":"LOGIN","token":"<token>"}` → 응답 `return_code 0` 확인, 이후 `{"trnm":"REG","grp_no":"1","refresh":"1","data":[{"item":[""],"type":["00"]}]}` 로 구독(주문체결 `00`, 잔고 `04`, 장시작 `0s`, 체결 `0B`(item=종목코드), VI `1h`). 서버가 `{"trnm":"PING"}` 을 보내면 **같은 메시지를 그대로 되돌려 보낸다**. 수신 실시간 데이터 `trnm=='REAL'`, `data[].values{필드번호:값}` (예: 00 → 9203 주문번호, 9001 종목코드, 913 주문상태, 900 주문수량, 910 체결가, 911 체결량, 907 매도수구분, 908 시간, 909 체결번호). 위 LOGIN/PING 세부는 스펙 JSON/PDF(`키움 REST API 문서.pdf`, pdf 스킬 사용 가능)로 재확인하고, 다르면 문서를 따른다.
* Rate limit: 공식 5회/초 vs 실측 상충 → 요청 사이 최소 간격 + 1700 시 지수 백오프. 호출 결과는 `api_call_log` 에 기록(본문/토큰 제외).

## 5. 서버모듈 stock_svr 요구사항 (`server/`)
Windows 상시 실행 GUI(표준 라이브러리 **tkinter** — 추가 UI 의존성 없음) + 백그라운드 엔진.

**UI**
* 상단 상태바 아이콘(색 원 + 라벨): **DB 접속**, **키움 REST 연결**, **키움 WS 연결**, 시장상태(장중/장외), 모드(REAL/MOCK), **주문허용 여부**(OFF/ON, 실전 ON 은 눈에 띄게 경고색). 녹색=정상 / 황색=주의·재연결중 / 적색=오류 / 회색=미확인. 마우스 오버(툴팁)로 최근 메시지.
* 탭: ① 대시보드(예수금·평가금액·손익 요약 + 보유종목 표) ② **주요 기록**(이벤트 로그 실시간 출력: 시각/레벨/분류/메시지, 레벨 필터, 자동 스크롤) ③ **알고리즘**(선택 체크박스·우선순위, 알고리즘별 파라미터 편집 폼을 `algorithm_param_def` 로 **동적 생성** — 타입별 검증(int/decimal/bool/enum/time/string, min/max), 저장 시 `algorithm_param_value` 갱신 + `algorithm_param_history` 기록, 기본값 복원 버튼) ④ **설정**(거래환경·주문허용·실전 이중확인·평가주기; 실전 주문 ON 시 확인창에서 `REAL` 을 직접 입력해야 활성화) + 엔진 시작/정지 버튼.
* 창을 닫을 때 확인 후 정상 종료(WS 종료, 토큰 폐기 `au10002`, `server_status` 갱신, `algo_run.ended_at`). UI 스레드는 블로킹 금지(엔진은 별도 스레드, 큐로 이벤트 전달).
* 10초 주기로 `server_status` 하트비트(component: server/db/kiwoom_rest/kiwoom_ws/market) 갱신 → 웹 관제 화면이 사용.

**로그**: `server/logs/stock_svr.log` (일 단위 회전, **보관 7일** — `TimedRotatingFileHandler(backupCount=7)` + 시작 시/매일 자정 7일 초과 파일 삭제). 로그 레벨·경로는 설정. DB `event_log`/`api_call_log` 도 7일 초과분 삭제. 비밀값 마스킹 필수. 주요 사건(연결/오류/신호/주문/체결/설정변경)은 `event_log` 에도 기록해 UI 에 출력.

**엔진**
* 시작 시: 설정 로드 → DB 연결 → 토큰 발급 → `ka00001` 계좌 확인/`account` 등록(env=현재 모드) → 종목마스터 갱신(`ka10099`, 하루 1회) → WS 연결/구독 → 주기 루프.
* 동기화 서비스: 계좌 잔고/보유(`kt00001`,`kt00018` → `account_balance`,`holding`) 주기 동기화, 미체결/체결(`ka10075`,`ka10076` + WS `00` → `orders`,`executions` upsert), 장마감 후 1회: `kt00015`(최근 며칠) → `trade_ledger`, `ka10170` → `daily_trade_summary`, `holding_snapshot`. 중복 방지는 UNIQUE 키 기반 upsert.
* 알고리즘(`server/stock_svr/algo/`, `algorithm.code` 와 1:1): `risk_guard`(항상 켜짐, 모든 주문 사전 검증: 총투입한도·종목당한도·손절선·일손실한도·일주문횟수·매매시간), `momentum_screen`(ka10027+ka10023 교집합 → `screening_result`), `volatility_breakout`(ka10081 → `price_daily`, 목표가 = 당일시가+전일변동폭×K), `averaging_down`(평단 대비 -drop_pct% 시 추가매수, `position_state.avg_down_count`≤max_steps, 종목당 한도, 손절선 도달 시 전량 매도, **무한 물타기 불가**), `ma_cross_filter`(단기<장기 종목 진입 차단). 공통 인터페이스(예: `Algorithm.evaluate(ctx) -> list[Signal]`)와 레지스트리로 플러그인 구조. 새 알고리즘 추가 시 `algorithm`/`algorithm_param_def` 행 + 모듈만 추가하면 UI에 노출.
* 파이프라인: 평가 주기마다 DB에서 활성 알고리즘·파라미터 재로드 → 진입 신호 → 필터 → risk_guard → 실행기(Executor). Executor 는 2·3절 게이트를 통과할 때만 `kt10000/1` 호출, 그 외 `SIGNAL_ONLY` 기록. 동일 종목 재신호 쿨다운·중복 주문 방지. 모든 신호는 `signal_log`, 주문은 `orders`(+`algo_run`).
* 장 시간(평일 09:00~15:30 KST, 공휴일은 `0s`/API 응답으로 판단 가능한 범위) 밖에서는 조회 동기화만 저간격으로 수행하고 신규 신호는 내지 않는다.
* 예외 격리: 한 서비스의 예외가 엔진 전체를 죽이지 않게 하고, 토큰 만료/네트워크 오류/WS 끊김은 자동 복구(지수 백오프).

**품질**: `pytest` 단위테스트(파서, 파라미터 검증, risk_guard, 각 알고리즘 — fake 데이터, **주문 게이트 테스트 필수**: 게이트 조건 모든 조합에서 fake 클라이언트가 호출되는 경우/안 되는 경우 검증). `python -m stock_svr --check` : 읽기 전용 점검(설정 → DB → 토큰 → `ka00001`/`kt00001`/`kt00018`) 결과를 마스킹해 출력. 실행 스크립트 `run_stock_svr.bat`, `README.md`(설치·실행·설정), `requirements.txt`(pymysql, httpx, websockets 등 최소).

## 6. 웹 요구사항 (`web/` → `D:\xampp\htdocs\stock`)
xampp Apache + PHP(PDO). **`https://pms.utinfo.co.kr/stock` 로 외부 공개**되므로 보안 요구가 높다. 기본은 **읽기 전용 관제/조회** 화면(쓰기는 로그인·비밀번호 변경뿐).

**메뉴(상단 대메뉴 → 소메뉴 드롭다운, 모바일 대응)**
* 대시보드: 종합현황(예수금·총평가·손익·보유종목 요약, 서버 연결상태 아이콘, 최근 이벤트) — 30초 자동 갱신(fetch, GET 전용 JSON)
* 계좌: 예수금/잔고(추이 SVG 차트), **보유종목**(수익률 색상, 정렬)
* 거래: 체결내역, 주문내역(신호만 기록된 건 구분 표시), **거래내역**(kt00015 정본), 매매일지(일별 손익, 기간합계)
* 전략: 알고리즘 현황(선택 여부·우선순위), 파라미터 값/변경이력(읽기 전용), 신호 기록
* 시스템: 서버 상태(`server_status`, 하트비트 지연 경고), 이벤트 로그, 내 정보/비밀번호 변경(현재 비밀번호 확인, 최소 길이·복잡도), 사용자 목록(admin)
* 공통: 기간/종목 필터, 페이지네이션, 계좌 선택(`account`), 금액 천단위·부호 색(상승 빨강/하락 파랑 — 한국 관례), 빈 데이터 안내문, 표시 시각은 KST. 계좌번호는 **끝 4자리만 표시(마스킹)**.
* 로그인 화면(ID/PW). 사용자는 `app_user` 테이블(비밀번호 해시 `password_hash()`). 초기 관리자는 이미 생성됨.

**보안(외부 공개 필수)**: `password_verify`; 로그인 실패 5회 → `locked_until` 15분 잠금 + `app_login_log` 기록, IP 기준 10분 20회 초과 시 429; 계정 존재 여부를 알 수 없는 동일한 오류문구·유사 응답시간; 세션 쿠키 `HttpOnly; Secure(HTTPS일 때); SameSite=Strict; path=/stock`, 로그인 시 `session_regenerate_id(true)`, 유휴 30분/절대 8시간 만료; 모든 POST 에 CSRF 토큰; 보안 헤더(CSP `default-src 'self'`+인라인 스크립트 금지, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, 인증 페이지 `Cache-Control: no-store`, `X-Robots-Tag: noindex`); PDO prepared statement(에뮬레이션 OFF)만 사용, 모든 출력 `htmlspecialchars`; `display_errors` OFF·오류는 웹루트 밖 로그; 디렉터리 리스팅 금지; 인증 없는 접근은 로그인으로 리다이렉트, JSON API 는 401; `lib/`·`config/` 등 비공개 디렉터리는 `.htaccess` `Require all denied` + 직접 접근 방지 상수 가드. 앱은 **`/stock` 하위 경로에서 동작**(절대경로 `/` 금지, 기준경로 자동 계산) 하고 `http://localhost/stock/` 과 `https://pms.utinfo.co.kr/stock/` 양쪽에서 동일하게 동작해야 한다. **Apache/PHP 설정 파일은 수정하지 않고 재시작하지 않는다**(다른 서비스가 같은 Apache 사용). `.htaccess` 가 실제로 적용되는지 HTTP 로 확인하고, 적용되지 않으면 코드 구조로 보호한다.

**배포**: `tools/deploy_web.ps1` — `web/` → `D:\xampp\htdocs\stock`(robocopy 미러, `config.local.php`·`.htaccess` 규칙 고려)와 `config.local.php` → `D:\xampp\stock_private\` 복사. 배포 후 `http://localhost/stock/` 로 스모크 테스트(curl).
**테스트 데이터**: 실 DB 를 오염시키지 않도록 `tools/demo_data.py load|clear` (계좌 `account_no='DEMO-0000', env='mock'` 로 격리, `stock_svr` 계정 사용)를 만들어 화면 검증에 쓰고, **작업 종료 시 반드시 clear** 한다.
**품질**: `php -l` 전 파일, curl 기반 시나리오(로그인 성공/실패/잠금/CSRF 누락/세션 만료/미인증 접근/보안 헤더/비공개 디렉터리 차단/SQL 인젝션·XSS 시도), `web/README.md`.

## 7. 산출물 / 폴더
```
D:\claude_stock_dealings
  db\        schema.sql seed.sql setup_db.py        (완료)
  server\    stock_svr\ tests\ config\ logs\ requirements.txt run_stock_svr.bat README.md
  web\       (PHP 앱)  config\ lib\ public 페이지 ...  README.md
  tools\     deploy_web.ps1  demo_data.py
  docs\      DEV_SPEC.md(본 문서) table_design.md architecture.md ...
```
