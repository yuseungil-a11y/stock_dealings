# 테이블 설계서 — stock_dealings

> `tools/gen_table_doc.py` 가 실제 DB 에서 자동 생성한 문서입니다(직접 수정 금지). 원본 DDL: `db/schema.sql`, 초기 데이터: `db/seed.sql`.

- DBMS: MariaDB 10.11, 문자셋 utf8mb4, 총 **29개 테이블** + 뷰 1개
- 금액/수량/가격 = BIGINT, 비율(%) = DECIMAL(12,4), 시각 = DATETIME(KST)
- 키움 API 문자열 값(부호·0패딩)은 서버모듈이 정수로 파싱해 저장
- 권한: `stock_svr`(서버, SELECT/INSERT/UPDATE/DELETE) · `stock_web`(웹, SELECT 전용 + `app_login_log` INSERT + `app_user` 일부 컬럼 UPDATE)

## 웹 사용자·로그인

### `app_user`

웹 로그인 사용자

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | int(10) unsigned | N | PRI |  |  |
| `username` | varchar(50) | N | UNI |  |  |
| `password_hash` | varchar(255) | N |  |  | PHP password_hash() 결과 (평문 저장 금지) |
| `display_name` | varchar(100) | Y |  | NULL |  |
| `role` | enum('admin','viewer') | N |  | 'viewer' |  |
| `is_active` | tinyint(1) | N |  | 1 |  |
| `failed_count` | smallint(5) unsigned | N |  | 0 | 연속 로그인 실패 횟수 |
| `locked_until` | datetime | Y |  | NULL | 잠금 해제 시각(연속 실패 시) |
| `last_login_at` | datetime | Y |  | NULL |  |
| `created_at` | datetime | N |  | current_timestamp() |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `app_login_log`

웹 로그인 시도 이력

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `user_id` | int(10) unsigned | Y |  | NULL |  |
| `username` | varchar(50) | N | MUL |  |  |
| `success` | tinyint(1) | N |  |  |  |
| `ip_addr` | varchar(45) | Y |  | NULL |  |
| `user_agent` | varchar(255) | Y |  | NULL |  |
| `created_at` | datetime | N | MUL | current_timestamp() |  |

## 계좌·잔고·보유종목

### `account`

증권 계좌

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | int(10) unsigned | N | PRI |  |  |
| `account_no` | varchar(20) | N | MUL |  | ka00001 acctNo |
| `env` | enum('mock','real') | N |  |  | 모의/실전 |
| `alias` | varchar(50) | Y |  | NULL |  |
| `is_active` | tinyint(1) | N |  | 1 |  |
| `created_at` | datetime | N |  | current_timestamp() |  |

### `account_balance`

예수금/평가 스냅샷 이력

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `snapshot_at` | datetime | N |  |  |  |
| `entr` | bigint(20) | Y |  | NULL | 예수금 (kt00001 entr) |
| `d1_entra` | bigint(20) | Y |  | NULL | D+1 추정예수금 |
| `d2_entra` | bigint(20) | Y |  | NULL | D+2 추정예수금 |
| `ord_alow_amt` | bigint(20) | Y |  | NULL | 주문가능금액 |
| `pymn_alow_amt` | bigint(20) | Y |  | NULL | 출금가능금액 |
| `tot_pur_amt` | bigint(20) | Y |  | NULL | 총매입금액 (kt00018) |
| `tot_evlt_amt` | bigint(20) | Y |  | NULL | 총평가금액 |
| `tot_evlt_pl` | bigint(20) | Y |  | NULL | 총평가손익금액 |
| `tot_prft_rt` | decimal(12,4) | Y |  | NULL | 총수익률(%) |
| `prsm_dpst_aset_amt` | bigint(20) | Y |  | NULL | 추정예탁자산 |

### `holding`

현재 보유종목 (kt00018 acnt_evlt_remn_indv_tot)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `account_id` | int(10) unsigned | N | PRI |  |  |
| `stk_cd` | varchar(12) | N | PRI |  | 종목코드(A 접두 제거한 6자리) |
| `stk_nm` | varchar(60) | N |  |  |  |
| `rmnd_qty` | bigint(20) | N |  | 0 | 보유수량 |
| `trde_able_qty` | bigint(20) | Y |  | NULL | 매매가능수량 |
| `pur_pric` | bigint(20) | Y |  | NULL | 매입가(평균단가) |
| `cur_prc` | bigint(20) | Y |  | NULL | 현재가 |
| `pur_amt` | bigint(20) | Y |  | NULL | 매입금액 |
| `evlt_amt` | bigint(20) | Y |  | NULL | 평가금액 |
| `evltv_prft` | bigint(20) | Y |  | NULL | 평가손익 |
| `prft_rt` | decimal(12,4) | Y |  | NULL | 수익률(%) |
| `poss_rt` | decimal(12,4) | Y |  | NULL | 보유비중(%) |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `holding_snapshot`

일별 보유종목 스냅샷(장마감 후 1회)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `snap_date` | date | N |  |  |  |
| `stk_cd` | varchar(12) | N |  |  |  |
| `stk_nm` | varchar(60) | N |  |  |  |
| `rmnd_qty` | bigint(20) | N |  |  |  |
| `pur_pric` | bigint(20) | Y |  | NULL |  |
| `cur_prc` | bigint(20) | Y |  | NULL |  |
| `evlt_amt` | bigint(20) | Y |  | NULL |  |
| `evltv_prft` | bigint(20) | Y |  | NULL |  |
| `prft_rt` | decimal(12,4) | Y |  | NULL |  |

### `position_state`

종목별 전략 상태 (물타기 횟수·투입한도 추적)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `account_id` | int(10) unsigned | N | PRI |  |  |
| `stk_cd` | varchar(12) | N | PRI |  |  |
| `entry_algo` | varchar(50) | Y |  | NULL | 최초 진입 알고리즘 code |
| `first_buy_at` | datetime | Y |  | NULL |  |
| `last_buy_price` | bigint(20) | Y |  | NULL | 마지막 매수가 (물타기 기준) |
| `avg_down_count` | smallint(5) unsigned | N |  | 0 | 물타기(추가매수) 횟수 |
| `total_invested` | bigint(20) | N |  | 0 | 종목 누적 투입금 |
| `stopped` | tinyint(1) | N |  | 0 | 손절 후 재진입 금지 표시 |
| `updated_at` | datetime | N |  | current_timestamp() |  |

## 종목·시세

### `stock_master`

종목 마스터 (ka10099)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `stk_cd` | varchar(12) | N | PRI |  |  |
| `stk_nm` | varchar(60) | N | MUL |  |  |
| `market_code` | varchar(10) | Y |  | NULL | marketCode |
| `market_name` | varchar(30) | Y |  | NULL |  |
| `up_name` | varchar(60) | Y |  | NULL | 업종명 |
| `list_count` | bigint(20) | Y |  | NULL | 상장주식수 |
| `last_price` | bigint(20) | Y |  | NULL | 전일종가 |
| `state` | varchar(100) | Y |  | NULL | 종목상태 |
| `order_warning` | varchar(10) | Y |  | NULL | 투자유의종목 여부 |
| `nxt_enable` | varchar(5) | Y |  | NULL |  |
| `reg_day` | varchar(8) | Y |  | NULL | 상장일 |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `price_daily`

일봉 (ka10081) - 변동성돌파/이동평균 계산용

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `stk_cd` | varchar(12) | N | PRI |  |  |
| `dt` | date | N | PRI |  |  |
| `open_pric` | bigint(20) | Y |  | NULL |  |
| `high_pric` | bigint(20) | Y |  | NULL |  |
| `low_pric` | bigint(20) | Y |  | NULL |  |
| `cur_prc` | bigint(20) | Y |  | NULL | 종가 |
| `trde_qty` | bigint(20) | Y |  | NULL |  |
| `trde_prica` | bigint(20) | Y |  | NULL | 거래대금 |

### `screening_result`

순위/스크리닝 API 조회 결과(모멘텀 후보)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `captured_at` | datetime | N | MUL |  |  |
| `source_api` | varchar(10) | N |  |  | ka10027(등락률상위) / ka10023(거래량급증) 등 |
| `rank_no` | smallint(5) unsigned | Y |  | NULL |  |
| `stk_cd` | varchar(12) | N | MUL |  |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `cur_prc` | bigint(20) | Y |  | NULL |  |
| `flu_rt` | decimal(12,4) | Y |  | NULL | 등락률(%) |
| `now_trde_qty` | bigint(20) | Y |  | NULL |  |
| `sdnin_rt` | decimal(14,4) | Y |  | NULL | 거래량 급증률(%) |

## 주문·체결·거래내역

### `algo_run`

서버 세션(실행) 이력

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `started_at` | datetime | N |  |  |  |
| `ended_at` | datetime | Y |  | NULL |  |
| `env` | enum('mock','real') | N |  |  |  |
| `order_enabled` | tinyint(1) | N |  |  | 해당 실행 시점 주문 허용 여부(0=신호만 기록, 주문 미전송) |
| `note` | varchar(255) | Y |  | NULL |  |

### `orders`

주문 이력 (자동매매 주문 전부 기록)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `run_id` | bigint(20) unsigned | Y |  | NULL |  |
| `algo_code` | varchar(50) | Y |  | NULL | 주문을 낸 알고리즘 |
| `ord_no` | varchar(20) | Y |  | NULL | 키움 주문번호 (전송 실패 시 NULL) |
| `orig_ord_no` | varchar(20) | Y |  | NULL | 정정/취소 시 원주문번호 |
| `side` | enum('BUY','SELL') | N |  |  |  |
| `order_kind` | enum('NEW','MODIFY','CANCEL') | N |  | 'NEW' |  |
| `stk_cd` | varchar(12) | N | MUL |  |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `dmst_stex_tp` | varchar(5) | N |  | 'KRX' |  |
| `trde_tp` | varchar(3) | N |  |  | 매매구분 0:보통 3:시장가 ... |
| `ord_qty` | bigint(20) | N |  |  |  |
| `ord_uv` | bigint(20) | Y |  | NULL | 주문단가(시장가는 NULL) |
| `status` | enum('SIGNAL_ONLY','SENT','ACCEPTED','PARTIAL','FILLED','CANCELED','REJECTED','FAILED') | N |  | 'SENT' |  |
| `filled_qty` | bigint(20) | N |  | 0 |  |
| `avg_fill_pric` | bigint(20) | Y |  | NULL |  |
| `reason` | varchar(255) | Y |  | NULL | 주문 사유(신호 설명) |
| `return_code` | int(11) | Y |  | NULL |  |
| `return_msg` | varchar(255) | Y |  | NULL |  |
| `is_dry_run` | tinyint(1) | N |  | 0 | 1=신호만 기록하고 전송하지 않음 |
| `created_at` | datetime | N |  | current_timestamp() |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |
| `signal_price` | bigint(20) | Y |  | NULL | 신호 발생 시점 기준가(슬리피지 계산용) |
| `signal_context` | text | Y |  | NULL | 신호 맥락 JSON(kind/score/meta 등) |
| `params_snapshot` | text | Y |  | NULL | 주문 시점 알고리즘 파라미터 JSON(risk_guard+진입알고리즘) |
| `reject_reason` | varchar(255) | Y |  | NULL | 거래소 거부사유 |

### `executions`

체결 내역 (WS 00 주문체결 / ka10076)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `ord_no` | varchar(20) | N |  |  |  |
| `cntr_no` | varchar(20) | N |  | '' | 체결번호 |
| `stk_cd` | varchar(12) | N | MUL |  |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `side` | enum('BUY','SELL') | N |  |  |  |
| `cntr_qty` | bigint(20) | N |  |  |  |
| `cntr_pric` | bigint(20) | N |  |  |  |
| `cmsn` | bigint(20) | Y |  | NULL | 당일매매수수료 |
| `tax` | bigint(20) | Y |  | NULL | 당일매매세금 |
| `executed_at` | datetime | N |  |  |  |
| `source` | enum('WS','REST') | N |  | 'WS' |  |
| `created_at` | datetime | N |  | current_timestamp() |  |

### `trade_ledger`

위탁종합거래내역 (kt00015) - 증권사 정본 거래내역

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `trde_dt` | date | N |  |  |  |
| `trde_no` | varchar(20) | N |  |  |  |
| `trde_kind_nm` | varchar(30) | Y |  | NULL | 거래종류명(매수/매도/입금...) |
| `rmrk_nm` | varchar(80) | Y |  | NULL | 적요명 |
| `stk_cd` | varchar(12) | Y | MUL | NULL |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `trde_qty` | bigint(20) | Y |  | NULL |  |
| `trde_unit` | bigint(20) | Y |  | NULL | 거래단가 |
| `trde_amt` | bigint(20) | Y |  | NULL | 거래금액 |
| `cmsn` | bigint(20) | Y |  | NULL |  |
| `tax` | bigint(20) | Y |  | NULL | 거래및농특세+소득세 합 |
| `exct_amt` | bigint(20) | Y |  | NULL | 정산금액 |
| `entra_remn` | bigint(20) | Y |  | NULL | 거래후 예수금잔고 |
| `proc_tm` | varchar(20) | Y |  | NULL |  |

### `daily_trade_summary`

당일매매일지 (ka10170) 종목별 일 손익

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `account_id` | int(10) unsigned | N | MUL |  |  |
| `base_dt` | date | N |  |  |  |
| `stk_cd` | varchar(12) | N |  |  |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `buy_qty` | bigint(20) | Y |  | NULL |  |
| `buy_avg_pric` | bigint(20) | Y |  | NULL |  |
| `buy_amt` | bigint(20) | Y |  | NULL |  |
| `sell_qty` | bigint(20) | Y |  | NULL |  |
| `sell_avg_pric` | bigint(20) | Y |  | NULL |  |
| `sell_amt` | bigint(20) | Y |  | NULL |  |
| `cmsn_tax` | bigint(20) | Y |  | NULL | 수수료+제세금 |
| `pl_amt` | bigint(20) | Y |  | NULL | 손익금액 |
| `prft_rt` | decimal(12,4) | Y |  | NULL | 수익률(%) |

## 알고리즘·파라미터

### `algorithm`

알고리즘 목록

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | int(10) unsigned | N | PRI |  |  |
| `code` | varchar(50) | N | UNI |  | 코드 식별자 (server/stock_svr/algo 모듈과 매칭) |
| `name` | varchar(100) | N |  |  |  |
| `role` | enum('entry','risk','filter') | N |  |  | entry=진입, risk=리스크관리(물타기/손절), filter=시장국면 필터 |
| `description` | varchar(500) | Y |  | NULL |  |
| `is_locked` | tinyint(1) | N |  | 0 | 1=항상 활성(비활성화 불가, 예: risk_guard) |
| `sort_order` | smallint(6) | N |  | 0 |  |

### `algorithm_param_def`

파라미터 정의 (편집 UI가 이 정의로 폼을 동적 생성)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | int(10) unsigned | N | PRI |  |  |
| `algorithm_id` | int(10) unsigned | N | MUL |  |  |
| `param_key` | varchar(50) | N |  |  |  |
| `label` | varchar(100) | N |  |  | UI 표시명 |
| `value_type` | enum('int','decimal','bool','string','enum','time') | N |  |  |  |
| `default_value` | varchar(100) | N |  |  |  |
| `min_value` | varchar(50) | Y |  | NULL |  |
| `max_value` | varchar(50) | Y |  | NULL |  |
| `enum_options` | varchar(500) | Y |  | NULL | enum 선택지 'value:라벨,value:라벨' |
| `unit` | varchar(20) | Y |  | NULL |  |
| `description` | varchar(300) | Y |  | NULL |  |
| `sort_order` | smallint(6) | N |  | 0 |  |

### `algorithm_param_value`

파라미터 현재값

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `algorithm_id` | int(10) unsigned | N | PRI |  |  |
| `param_key` | varchar(50) | N | PRI |  |  |
| `value` | varchar(100) | N |  |  |  |
| `updated_by` | varchar(50) | Y |  | NULL |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `algorithm_param_history`

파라미터 변경 이력

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `algorithm_id` | int(10) unsigned | N | MUL |  |  |
| `param_key` | varchar(50) | N |  |  |  |
| `old_value` | varchar(100) | Y |  | NULL |  |
| `new_value` | varchar(100) | N |  |  |  |
| `changed_by` | varchar(50) | Y |  | NULL |  |
| `changed_at` | datetime | N |  | current_timestamp() |  |

### `algorithm_selection`

사용 알고리즘 선택(조합 가능)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `algorithm_id` | int(10) unsigned | N | PRI |  |  |
| `is_enabled` | tinyint(1) | N |  | 0 |  |
| `priority` | smallint(6) | N |  | 100 | 작을수록 먼저 평가 |
| `updated_by` | varchar(50) | Y |  | NULL |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `signal_log`

알고리즘 신호 기록

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `run_id` | bigint(20) unsigned | Y |  | NULL |  |
| `algo_code` | varchar(50) | N |  |  |  |
| `stk_cd` | varchar(12) | N | MUL |  |  |
| `stk_nm` | varchar(60) | Y |  | NULL |  |
| `signal_type` | enum('BUY','SELL','HOLD','BLOCK') | N |  |  | BLOCK=리스크/필터로 차단됨 |
| `score` | decimal(14,4) | Y |  | NULL |  |
| `detail` | varchar(500) | Y |  | NULL |  |
| `order_id` | bigint(20) unsigned | Y |  | NULL | 연결된 주문 |
| `created_at` | datetime | N | MUL | current_timestamp() |  |

## 시스템·관제

### `system_setting`

전역 설정 (order_enabled=긴급정지 스위치 등)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `setting_key` | varchar(50) | N | PRI |  |  |
| `value` | varchar(255) | N |  |  |  |
| `description` | varchar(300) | Y |  | NULL |  |
| `updated_by` | varchar(50) | Y |  | NULL |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `server_status`

서버 하트비트/연결상태 (웹 관제 화면용)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `component` | varchar(30) | N | PRI |  | server / db / kiwoom_rest / kiwoom_ws / market |
| `status` | enum('ok','warn','error','unknown') | N |  | 'unknown' |  |
| `message` | varchar(255) | Y |  | NULL |  |
| `updated_at` | datetime | N |  | current_timestamp() |  |

### `event_log`

주요 이벤트 기록 (서버 UI/웹 출력용, 1주일 보관)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `level` | enum('DEBUG','INFO','WARN','ERROR') | N | MUL | 'INFO' |  |
| `category` | varchar(30) | N |  |  | system/auth/order/algo/sync/ws ... |
| `message` | varchar(500) | N |  |  |  |
| `created_at` | datetime | N | MUL | current_timestamp() |  |

### `api_call_log`

키움 API 호출 로그 (Rate limit 실측용, 1주일 보관). 요청/응답 본문·토큰은 저장하지 않음

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `api_id` | varchar(10) | N | MUL |  |  |
| `http_status` | smallint(6) | Y |  | NULL |  |
| `return_code` | int(11) | Y |  | NULL |  |
| `return_msg` | varchar(255) | Y |  | NULL |  |
| `elapsed_ms` | int(11) | Y |  | NULL |  |
| `created_at` | datetime | N | MUL | current_timestamp() |  |

## 거래 분석 기록(영구 보관)

### `order_event`

주문 상태 변화 이력(접수→부분체결→체결/거부/취소). 영구 보관

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `order_id` | bigint(20) unsigned | Y | MUL | NULL | orders.id |
| `account_id` | int(10) unsigned | Y | MUL | NULL |  |
| `ord_no` | varchar(20) | Y |  | NULL |  |
| `event_time` | datetime | N | MUL | current_timestamp() |  |
| `event_type` | enum('CREATED','SENT','ACCEPTED','PARTIAL','FILLED','CANCELED','REJECTED','FAILED','UNKNOWN','MODIFIED','NOTE') | N |  |  |  |
| `status` | varchar(20) | Y |  | NULL | 이 시점의 orders.status |
| `filled_qty` | bigint(20) | Y |  | NULL |  |
| `remain_qty` | bigint(20) | Y |  | NULL |  |
| `price` | bigint(20) | Y |  | NULL | 체결가/주문가 |
| `reject_reason` | varchar(255) | Y |  | NULL | 거래소 거부사유(WS 919 등) |
| `return_code` | int(11) | Y |  | NULL |  |
| `message` | varchar(255) | Y |  | NULL |  |
| `source` | enum('EXECUTOR','WS','REST') | N |  | 'EXECUTOR' |  |

### `event_archive`

주요 이벤트 영구 보관본(WARN/ERROR + 주문·알고리즘·엔진 이벤트). event_log 는 7일 정리, 이 테이블은 archive_retention_days(기본 365일)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `created_at` | datetime | N | MUL | current_timestamp() |  |
| `level` | enum('DEBUG','INFO','WARN','ERROR') | N |  |  |  |
| `category` | varchar(30) | N | MUL |  |  |
| `message` | varchar(500) | N |  |  |  |

### `api_error_log`

키움 API 오류 응답만 영구 보관(정상 호출 로그 api_call_log 는 7일 정리)

| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `id` | bigint(20) unsigned | N | PRI |  |  |
| `created_at` | datetime | N | MUL | current_timestamp() |  |
| `api_id` | varchar(10) | N | MUL |  |  |
| `http_status` | smallint(6) | Y |  | NULL |  |
| `return_code` | int(11) | Y |  | NULL |  |
| `return_msg` | varchar(255) | Y |  | NULL |  |
| `elapsed_ms` | int(11) | Y |  | NULL |  |

## 기타

- `llm_decision_log` — Claude 거부권 필터 판단 기록(모델·근거·토큰·결과). 1주일 이상 보관 가능

## 분석용 뷰

> 읽기 전용입니다. 서버 코드는 뷰에 쓰지 않습니다.

### `v_trade_analysis`

| 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|
| `signal_id` | bigint(20) unsigned | N |  |
| `signal_time` | datetime | N |  |
| `algo_code` | varchar(50) | N |  |
| `signal_type` | enum('BUY','SELL','HOLD','BLOCK') | N | BLOCK=리스크/필터로 차단됨 |
| `stk_cd` | varchar(12) | N |  |
| `stk_nm` | varchar(60) | Y |  |
| `score` | decimal(14,4) | Y |  |
| `signal_detail` | varchar(500) | Y |  |
| `order_id` | bigint(20) unsigned | Y |  |
| `ord_no` | varchar(20) | Y | 키움 주문번호 (전송 실패 시 NULL) |
| `side` | enum('BUY','SELL') | Y |  |
| `order_kind` | enum('NEW','MODIFY','CANCEL') | Y |  |
| `order_status` | enum('SIGNAL_ONLY','SENT','ACCEPTED','PARTIAL','FILLED','CANCELED','REJECTED','FAILED') | Y |  |
| `is_dry_run` | tinyint(1) | Y | 1=신호만 기록하고 전송하지 않음 |
| `trde_tp` | varchar(3) | Y | 매매구분 0:보통 3:시장가 ... |
| `ord_qty` | bigint(20) | Y |  |
| `ord_uv` | bigint(20) | Y | 주문단가(시장가는 NULL) |
| `signal_price` | bigint(20) | Y | 신호 발생 시점 기준가(슬리피지 계산용) |
| `filled_qty` | bigint(20) | Y |  |
| `avg_fill_pric` | bigint(20) | Y |  |
| `slippage_pct` | decimal(27,3) | Y |  |
| `return_code` | int(11) | Y |  |
| `return_msg` | varchar(255) | Y |  |
| `reject_reason` | varchar(255) | Y | 거래소 거부사유 |
| `order_reason` | varchar(255) | Y | 주문 사유(신호 설명) |
| `signal_context` | text | Y | 신호 맥락 JSON(kind/score/meta 등) |
| `params_snapshot` | text | Y | 주문 시점 알고리즘 파라미터 JSON(risk_guard+진입알고리즘) |
| `order_time` | datetime | Y |  |
| `exec_cnt` | bigint(21) | Y |  |
| `exec_amount` | decimal(60,0) | Y |  |
| `exec_fee_tax` | decimal(42,0) | Y |  |
| `llm_model` | varchar(50) | Y |  |
| `llm_decision` | enum('allow','block','error') | Y | error=호출 실패/무효 응답(fail_mode 적용) |
| `llm_final` | enum('pass','block') | Y | 실제 파이프라인 결과(신뢰도·fail_mode 적용 후) |
| `llm_confidence` | smallint(6) | Y | 0~100 |
| `llm_reasons` | varchar(1000) | Y | Claude 가 제시한 근거(요약) |

