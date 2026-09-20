# 주식 자동매매 — 조회 · 관제 웹

xampp(Apache + PHP 8.2) 위에서 동작하는 **읽기 전용 관제/조회 화면**입니다.
프레임워크 없이 PHP + 바닐라 JS/CSS 로 작성했고, 외부 CDN·폰트·빌드 도구를 사용하지 않습니다.

| 항목 | 값 |
|---|---|
| 소스 | `D:\claude_stock_dealings\web` |
| 배포(웹루트) | `D:\xampp\htdocs\stock` |
| 설정(웹루트 밖) | `D:\xampp\stock_private\config.local.php` |
| 오류 로그(웹루트 밖) | `D:\xampp\stock_private\logs\web_error.log` |
| 내부 URL | `http://localhost/stock/` |
| 외부 URL | `https://pms.utinfo.co.kr/stock` |
| DB 계정 | `stock_web` (SELECT + 로그인 관련 최소 쓰기) |

> 이 웹은 트레이딩 설정을 **변경하지 않습니다**. 쓰기는 로그인 기록과 본인 비밀번호 변경뿐입니다.
> 알고리즘 선택·파라미터·주문 허용 스위치는 서버 프로그램(stock_svr)에서만 변경됩니다.

---

## 1. 폴더 구조

```
web/
  index.php            페이지 단일 진입점(front controller) — 모든 인증/보안 검사를 통과
  api.php              JSON 조회 API 진입점 (GET 전용)
  .htaccess            /stock 폴더 전용 (리스팅 차단, 진입점 외 PHP 차단, 보안 헤더)
  assets/
    css/app.css        자체 호스팅 스타일 (반응형)
    js/app.js          바닐라 JS (인라인 스크립트 없음 — CSP 준수)
    img/favicon.svg
  lib/                 (.htaccess Require all denied + STOCK_APP 상수 가드)
    bootstrap.php      상수·초기화
    config.php         설정 파일 탐색 (웹루트 밖 우선)
    security.php       오류 처리 / 보안 헤더 / 세션 / CSRF
    db.php             PDO (prepared statement, 에뮬레이션 OFF)
    auth.php           로그인 / 잠금 / IP 제한 / 세션 수명 / 비밀번호 변경
    repo.php           조회 전용 SQL
    view.php           페이지네이션 · 필터 · 배지 조각
    routes.php         메뉴/라우트 화이트리스트
  views/               화면 템플릿 (.htaccess Require all denied)
  config/              개발용 설정 위치 (배포 시 웹루트로 복사하지 않음)
    config.example.php
    config.local.php   ← git 제외. 실제 값은 여기에만 둔다.
```

## 2. 메뉴 구성

| 대메뉴 | 소메뉴 | page key | 내용 |
|---|---|---|---|
| 대시보드 | 종합현황 | `dashboard` | 예수금·총평가·손익·보유종목 요약, 서버 연결상태, 최근 이벤트, 30초 자동 갱신 |
| 계좌 | 예수금·잔고 | `account.balance` | 잔고 스냅샷 이력 + 추이 SVG 차트(지표/기간 선택) |
| 계좌 | 보유종목 | `account.holdings` | 수익률 색상, 컬럼 정렬, 합계 |
| 거래 | 체결내역 | `trade.executions` | 기간·종목·매수/매도 필터 |
| 거래 | 주문내역 | `trade.orders` | 신호만 기록된 건(`is_dry_run`/`SIGNAL_ONLY`) 배지 구분 |
| 거래 | 거래내역 | `trade.ledger` | 위탁종합거래내역(kt00015) 정본 |
| 거래 | 매매일지 | `trade.daily` | 일별·종목별 손익 + 기간 합계 + 손익 추이 차트 |
| 전략 | 알고리즘 현황 | `strategy.algorithms` | 선택 여부·우선순위(읽기 전용) |
| 전략 | 파라미터·변경이력 | `strategy.params` | 현재값/기본값/범위, 변경 이력 |
| 전략 | 신호 기록 | `strategy.signals` | BUY/SELL/HOLD/BLOCK, 알고리즘 필터 |
| 전략 | Claude 판단 | `strategy.claude` | Claude 거부권 필터(`llm_decision_log`) 판단 기록 — 오늘 요약 카드, 기간·종목·결과 필터 |
| 시스템 | 서버 상태 | `system.status` | `server_status` 하트비트(지연 경고), 런타임 설정, 실행 이력, API 호출 통계 |
| 시스템 | 이벤트 로그 | `system.events` | 레벨·분류·기간·메시지 필터 |
| 시스템 | 내 정보 | `system.profile` | 내 정보, 비밀번호 변경, 내 로그인 기록 |
| 시스템 | 사용자 목록 | `system.users` | **admin 전용**, 사용자/로그인 시도 이력 |

공통: 기간·종목 필터, 페이지네이션(50건), 계좌 선택(기본값 `env='real'` 우선),
금액 천단위·부호 색(상승/이익 **빨강**, 하락/손실 **파랑**), 빈 데이터 안내문, 표시 시각 KST,
계좌번호 끝 4자리만 표시.

## 3. JSON API (GET 전용, 로그인 필요)

`GET /stock/api.php?r=<name>` — 미인증은 **401 JSON**(리다이렉트하지 않음), GET 외 메서드는 405.

| r | 설명 |
|---|---|
| `ping` | 생존 확인 |
| `status` | `server_status` + 런타임 설정 |
| `dashboard` | 대시보드 전체(잔고/보유합계/상위 보유종목/오늘/Claude 요약/상태/최근 이벤트) — 화면 자동 갱신에 사용 |
| `holdings` | 보유종목 (`sort`, `q`) |
| `balance_series` | 잔고 추이 (`days`=1~365) |
| `events` | 최근 이벤트 (`level`) |
| `accounts` | 계좌 목록(마스킹) |

### 3.1 보유종목 파생 필드 (상장폐지 대응)

`holdings.rows[]` 와 `dashboard.holdings_top[]` 의 각 행에는 기존 컬럼에 더해 다음이 포함됩니다.

| 필드 | 형 | 설명 |
|---|---|---|
| `delisted` | bool | 현재가 `cur_prc = 0` 이거나 종목명이 `(폐)` 로 시작하면 `true` |
| `real_profit_rate` | float\|null | 실제 손익 기준 수익률 = `evltv_prft / pur_amt × 100` (매입금액 0 이하이면 `null`) |

`holdings.totals` / `dashboard.holding_totals` 에는 상장폐지 종목을 뺀 합계가 추가됩니다:
`delisted_cnt`, `pur_amt_ex`, `evlt_amt_ex`, `evltv_prft_ex`, `prft_rt_ex`(분모 0 이면 `null`).

`dashboard.llm` 은 오늘 Claude 판단 요약(`calls`/`blocks`/`errors`/`cache_hits`/`input_tokens`/`output_tokens`/`total`)이며,
`llm_decision_log` 를 읽을 수 없으면 `null` 입니다.

> 키움은 상장폐지 종목의 `prft_rt` 를 `0.00%` 로 내려줍니다. 화면(보유종목·대시보드)에서는
> 종목명 옆 **상장폐지** 배지와 함께 `real_profit_rate` 를 소수 1자리로 표시하고,
> 합계·계좌 요약에는 **"상장폐지 종목 제외 시 X%"** 보조 표기를 덧붙입니다(제외 후 매입금액이 0 이면 생략).

## 4. 보안 설계 요약

* 인증: `password_verify`, 실패 5회 → `locked_until` 15분 잠금, `app_login_log` 기록.
  IP 기준 10분 20회 초과 시 **429**. 계정 존재 여부를 알 수 없도록 **동일 오류 문구**와
  **최소 응답시간(350ms)** 을 적용.
* 세션: `HttpOnly`, HTTPS 일 때 `Secure`, `SameSite=Strict`, `path=/stock`,
  로그인 시 `session_regenerate_id(true)`, 유휴 30분 / 절대 8시간 만료.
* CSRF: 모든 POST 에 토큰(`hash_equals` 검증), 실패 시 403.
* 보안 헤더: CSP `default-src 'self'`(인라인 스크립트 없음), `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Robots-Tag: noindex`, 인증 페이지 `Cache-Control: no-store`.
* SQL: PDO prepared statement만 사용(`ATTR_EMULATE_PREPARES=false`), 식별자 자리는 화이트리스트 매핑.
* 출력: 모든 값 `htmlspecialchars`(`h()`), JS 는 `textContent` 만 사용.
* 오류: `display_errors` OFF, 오류는 **웹루트 밖** 로그 파일로. 화면에는 일반 문구만.
* 비공개 경로: `lib/`, `views/`, `config/` 에 `.htaccess Require all denied` +
  모든 파일 상단 `STOCK_APP` 상수 가드(.htaccess 가 무력화돼도 코드 구조로 차단).
  루트 `.htaccess` 는 `index.php` / `api.php` 외 PHP 직접 실행을 차단하고 디렉터리 리스팅을 끈다.
* 경로: 절대경로 `/` 를 코드에 쓰지 않고 `SCRIPT_NAME` 에서 기준경로를 계산 → `/stock` 하위에서 동작.
* Apache/PHP 전역 설정 파일은 수정하지 않으며 Apache 를 재시작하지 않는다.
  `.htaccess` 에 `php_flag` 계열 지시어를 쓰지 않는다(SAPI 에 따라 500 유발 가능).

## 5. 설치 · 배포

1. `web/config/config.example.php` → `web/config/config.local.php` 로 복사 후 `stock_web` 접속정보 입력.
2. 배포:

```powershell
powershell -ExecutionPolicy Bypass -File D:\claude_stock_dealings\tools\deploy_web.ps1
```

* `web/` → `D:\xampp\htdocs\stock` robocopy `/MIR` 미러(재실행 안전).
* `config/`, `README.md`, 로그·백업 파일은 배포에서 제외.
* `config.local.php` → `D:\xampp\stock_private\` 복사, `logs` 디렉터리 준비.
* 배포 후 `http://localhost/stock/` curl 스모크 테스트 자동 수행(`-SkipSmoke` 로 생략).

> `deploy_web.ps1` 은 Windows PowerShell 5.1 이 한글을 올바로 읽도록 **UTF-8 BOM** 으로 저장돼 있습니다.
> (다른 소스 파일은 모두 UTF-8 BOM 없음)

## 6. 테스트 데이터

```powershell
# 적재 (DEMO-0000 / env=mock 로 격리)
$env:STOCK_DEMO_PW='<임시계정 비밀번호>'    # 로그인 잠금 검증용 임시계정(demo_lock_test) 생성 시에만 필요
python D:\claude_stock_dealings\tools\demo_data.py load

# 잔존 확인
python D:\claude_stock_dealings\tools\demo_data.py status

# 삭제 (작업 종료 시 반드시 실행)
python D:\claude_stock_dealings\tools\demo_data.py clear
```

* 계좌 종속 데이터는 DEMO 계좌 id 로, 그 외(`event_log`/`signal_log`/`api_call_log`/`algo_run`/
  `algorithm_param_history`)는 `[DEMO]` 마커·고정 작성자명으로 표시해 정확히 회수합니다.
* `llm_decision_log` 데모 행(6건: allow/block/error·캐시 적중·토큰·이스케이프 검증용)은
  `stk_cd` 접두 **`DEMO`** 로 격리합니다.
* 보유종목에는 상장폐지 데모 1건(`(폐)데모폐지`, 현재가 0, `prft_rt=0`)이 포함되어
  실제 수익률 `-100.0%` 표시와 "상장폐지 종목 제외 시" 보조 표기를 검증할 수 있습니다.
* 전역 표(`system_setting`, `server_status`, `algorithm_selection`)는 건드리지 않습니다.
* 비밀번호는 환경변수 `STOCK_DEMO_PW` 로만 받고 소스/로그에 남기지 않습니다.

## 7. 품질 점검

```powershell
# 문법 검사
Get-ChildItem -Recurse D:\claude_stock_dealings\web -Filter *.php |
  ForEach-Object { D:\xampp\php\php.exe -n -l $_.FullName }
```

HTTP 시나리오(로그인 성공/실패/잠금, CSRF 누락, 미인증 접근, 보안 헤더, 비공개 디렉터리 차단,
SQL 인젝션·XSS 시도)는 `curl` 로 검증합니다. `deploy_web.ps1` 의 스모크 테스트가 핵심 항목을 자동 확인합니다.

## 8. 운영 시 권장(미적용 — 사용자 결정 필요)

* 외부 공개 구간에 **IP 허용목록** 또는 VPN 제한.
* **2단계 인증(TOTP)** 도입.
* Apache 앞단 **WAF / fail2ban** 연동(현재는 앱 레벨 IP 제한만 존재).
* `Strict-Transport-Security` 는 같은 호스트의 다른 서비스에 영향을 주므로 **적용하지 않았습니다**.
  도메인 전체를 HTTPS 전용으로 확정한 뒤 Apache 가상호스트에서 일괄 적용을 권장합니다.
