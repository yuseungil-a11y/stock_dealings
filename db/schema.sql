-- =====================================================================
-- stock_dealings  DB 스키마  (MariaDB 10.11, utf8mb4, InnoDB)
-- 키움증권 REST API 스펙(kiwoom-rest-api-spec.json) 분석 기반 설계
--   * 금액/수량/가격 : BIGINT (API는 부호·0패딩 문자열 -> 파싱 후 정수 저장)
--   * 비율(%)        : DECIMAL(12,4)
--   * 시각           : DATETIME (KST, 서버 로컬시간)
--   * 재실행 안전    : CREATE TABLE IF NOT EXISTS
-- 상세 설명: docs/table_design.md
-- =====================================================================
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
-- 1. 웹 사용자 / 로그인
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
  id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
  username        VARCHAR(50)  NOT NULL,
  password_hash   VARCHAR(255) NOT NULL COMMENT 'PHP password_hash() 결과 (평문 저장 금지)',
  display_name    VARCHAR(100) NULL,
  role            ENUM('admin','viewer') NOT NULL DEFAULT 'viewer',
  is_active       TINYINT(1)   NOT NULL DEFAULT 1,
  failed_count    SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '연속 로그인 실패 횟수',
  locked_until    DATETIME     NULL COMMENT '잠금 해제 시각(연속 실패 시)',
  last_login_at   DATETIME     NULL,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_app_user_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='웹 로그인 사용자';

CREATE TABLE IF NOT EXISTS app_login_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     INT UNSIGNED NULL,
  username    VARCHAR(50)  NOT NULL,
  success     TINYINT(1)   NOT NULL,
  ip_addr     VARCHAR(45)  NULL,
  user_agent  VARCHAR(255) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_login_log_created (created_at),
  KEY ix_login_log_user (username, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='웹 로그인 시도 이력';

-- ---------------------------------------------------------------------
-- 2. 계좌 / 잔고 / 보유종목   (API: ka00001, kt00001, kt00004, kt00018, kt00005)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_no  VARCHAR(20)  NOT NULL COMMENT 'ka00001 acctNo',
  env         ENUM('mock','real') NOT NULL COMMENT '모의/실전',
  alias       VARCHAR(50)  NULL,
  is_active   TINYINT(1)   NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_account (account_no, env)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='증권 계좌';

CREATE TABLE IF NOT EXISTS account_balance (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id          INT UNSIGNED NOT NULL,
  snapshot_at         DATETIME NOT NULL,
  entr                BIGINT NULL COMMENT '예수금 (kt00001 entr)',
  d1_entra            BIGINT NULL COMMENT 'D+1 추정예수금',
  d2_entra            BIGINT NULL COMMENT 'D+2 추정예수금',
  ord_alow_amt        BIGINT NULL COMMENT '주문가능금액',
  pymn_alow_amt       BIGINT NULL COMMENT '출금가능금액',
  tot_pur_amt         BIGINT NULL COMMENT '총매입금액 (kt00018)',
  tot_evlt_amt        BIGINT NULL COMMENT '총평가금액',
  tot_evlt_pl         BIGINT NULL COMMENT '총평가손익금액',
  tot_prft_rt         DECIMAL(12,4) NULL COMMENT '총수익률(%)',
  prsm_dpst_aset_amt  BIGINT NULL COMMENT '추정예탁자산',
  PRIMARY KEY (id),
  KEY ix_balance_acct_time (account_id, snapshot_at),
  CONSTRAINT fk_balance_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='예수금/평가 스냅샷 이력';

CREATE TABLE IF NOT EXISTS holding (
  account_id      INT UNSIGNED NOT NULL,
  stk_cd          VARCHAR(12)  NOT NULL COMMENT '종목코드(A 접두 제거한 6자리)',
  stk_nm          VARCHAR(60)  NOT NULL,
  rmnd_qty        BIGINT NOT NULL DEFAULT 0 COMMENT '보유수량',
  trde_able_qty   BIGINT NULL COMMENT '매매가능수량',
  pur_pric        BIGINT NULL COMMENT '매입가(평균단가)',
  cur_prc         BIGINT NULL COMMENT '현재가',
  pur_amt         BIGINT NULL COMMENT '매입금액',
  evlt_amt        BIGINT NULL COMMENT '평가금액',
  evltv_prft      BIGINT NULL COMMENT '평가손익',
  prft_rt         DECIMAL(12,4) NULL COMMENT '수익률(%)',
  poss_rt         DECIMAL(12,4) NULL COMMENT '보유비중(%)',
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, stk_cd),
  CONSTRAINT fk_holding_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='현재 보유종목 (kt00018 acnt_evlt_remn_indv_tot)';

CREATE TABLE IF NOT EXISTS holding_snapshot (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id      INT UNSIGNED NOT NULL,
  snap_date       DATE NOT NULL,
  stk_cd          VARCHAR(12) NOT NULL,
  stk_nm          VARCHAR(60) NOT NULL,
  rmnd_qty        BIGINT NOT NULL,
  pur_pric        BIGINT NULL,
  cur_prc         BIGINT NULL,
  evlt_amt        BIGINT NULL,
  evltv_prft      BIGINT NULL,
  prft_rt         DECIMAL(12,4) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_holding_snap (account_id, snap_date, stk_cd),
  CONSTRAINT fk_hsnap_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='일별 보유종목 스냅샷(장마감 후 1회)';

CREATE TABLE IF NOT EXISTS position_state (
  account_id        INT UNSIGNED NOT NULL,
  stk_cd            VARCHAR(12)  NOT NULL,
  entry_algo        VARCHAR(50)  NULL COMMENT '최초 진입 알고리즘 code',
  first_buy_at      DATETIME NULL,
  last_buy_price    BIGINT NULL COMMENT '마지막 매수가 (물타기 기준)',
  avg_down_count    SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '물타기(추가매수) 횟수',
  total_invested    BIGINT NOT NULL DEFAULT 0 COMMENT '종목 누적 투입금',
  stopped           TINYINT(1) NOT NULL DEFAULT 0 COMMENT '손절 후 재진입 금지 표시',
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, stk_cd),
  CONSTRAINT fk_posstate_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='종목별 전략 상태 (물타기 횟수·투입한도 추적)';

-- ---------------------------------------------------------------------
-- 3. 종목 / 시세   (API: ka10099, ka10001, ka10081)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_master (
  stk_cd          VARCHAR(12) NOT NULL,
  stk_nm          VARCHAR(60) NOT NULL,
  market_code     VARCHAR(10) NULL COMMENT 'marketCode',
  market_name     VARCHAR(30) NULL,
  up_name         VARCHAR(60) NULL COMMENT '업종명',
  list_count      BIGINT NULL COMMENT '상장주식수',
  last_price      BIGINT NULL COMMENT '전일종가',
  state           VARCHAR(100) NULL COMMENT '종목상태',
  order_warning   VARCHAR(10) NULL COMMENT '투자유의종목 여부',
  nxt_enable      VARCHAR(5) NULL,
  reg_day         VARCHAR(8) NULL COMMENT '상장일',
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (stk_cd),
  KEY ix_stock_name (stk_nm)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='종목 마스터 (ka10099)';

CREATE TABLE IF NOT EXISTS price_daily (
  stk_cd      VARCHAR(12) NOT NULL,
  dt          DATE NOT NULL,
  open_pric   BIGINT NULL,
  high_pric   BIGINT NULL,
  low_pric    BIGINT NULL,
  cur_prc     BIGINT NULL COMMENT '종가',
  trde_qty    BIGINT NULL,
  trde_prica  BIGINT NULL COMMENT '거래대금',
  PRIMARY KEY (stk_cd, dt)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='일봉 (ka10081) - 변동성돌파/이동평균 계산용';

CREATE TABLE IF NOT EXISTS screening_result (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  captured_at   DATETIME NOT NULL,
  source_api    VARCHAR(10) NOT NULL COMMENT 'ka10027(등락률상위) / ka10023(거래량급증) 등',
  rank_no       SMALLINT UNSIGNED NULL,
  stk_cd        VARCHAR(12) NOT NULL,
  stk_nm        VARCHAR(60) NULL,
  cur_prc       BIGINT NULL,
  flu_rt        DECIMAL(12,4) NULL COMMENT '등락률(%)',
  now_trde_qty  BIGINT NULL,
  sdnin_rt      DECIMAL(14,4) NULL COMMENT '거래량 급증률(%)',
  PRIMARY KEY (id),
  KEY ix_screen_time (captured_at),
  KEY ix_screen_stk (stk_cd, captured_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='순위/스크리닝 API 조회 결과(모멘텀 후보)';

-- ---------------------------------------------------------------------
-- 4. 주문 / 체결 / 거래내역   (API: kt10000~3, ka10075, ka10076, WS 00, kt00015, ka10170)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo_run (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  started_at    DATETIME NOT NULL,
  ended_at      DATETIME NULL,
  env           ENUM('mock','real') NOT NULL,
  order_enabled TINYINT(1) NOT NULL COMMENT '해당 실행 시점 주문 허용 여부(0=신호만 기록, 주문 미전송)',
  note          VARCHAR(255) NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='서버 세션(실행) 이력';

CREATE TABLE IF NOT EXISTS orders (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id     INT UNSIGNED NOT NULL,
  run_id         BIGINT UNSIGNED NULL,
  algo_code      VARCHAR(50) NULL COMMENT '주문을 낸 알고리즘',
  ord_no         VARCHAR(20) NULL COMMENT '키움 주문번호 (전송 실패 시 NULL)',
  orig_ord_no    VARCHAR(20) NULL COMMENT '정정/취소 시 원주문번호',
  side           ENUM('BUY','SELL') NOT NULL,
  order_kind     ENUM('NEW','MODIFY','CANCEL') NOT NULL DEFAULT 'NEW',
  stk_cd         VARCHAR(12) NOT NULL,
  stk_nm         VARCHAR(60) NULL,
  dmst_stex_tp   VARCHAR(5) NOT NULL DEFAULT 'KRX',
  trde_tp        VARCHAR(3) NOT NULL COMMENT '매매구분 0:보통 3:시장가 ...',
  ord_qty        BIGINT NOT NULL,
  ord_uv         BIGINT NULL COMMENT '주문단가(시장가는 NULL)',
  status         ENUM('SIGNAL_ONLY','SENT','ACCEPTED','PARTIAL','FILLED','CANCELED','REJECTED','FAILED') NOT NULL DEFAULT 'SENT',
  filled_qty     BIGINT NOT NULL DEFAULT 0,
  avg_fill_pric  BIGINT NULL,
  reason         VARCHAR(255) NULL COMMENT '주문 사유(신호 설명)',
  return_code    INT NULL,
  return_msg     VARCHAR(255) NULL,
  is_dry_run     TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1=신호만 기록하고 전송하지 않음',
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_orders_acct_time (account_id, created_at),
  KEY ix_orders_ordno (account_id, ord_no),
  KEY ix_orders_stk (stk_cd, created_at),
  CONSTRAINT fk_orders_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='주문 이력 (자동매매 주문 전부 기록)';

CREATE TABLE IF NOT EXISTS executions (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id  INT UNSIGNED NOT NULL,
  ord_no      VARCHAR(20) NOT NULL,
  cntr_no     VARCHAR(20) NOT NULL DEFAULT '' COMMENT '체결번호',
  stk_cd      VARCHAR(12) NOT NULL,
  stk_nm      VARCHAR(60) NULL,
  side        ENUM('BUY','SELL') NOT NULL,
  cntr_qty    BIGINT NOT NULL,
  cntr_pric   BIGINT NOT NULL,
  cmsn        BIGINT NULL COMMENT '당일매매수수료',
  tax         BIGINT NULL COMMENT '당일매매세금',
  executed_at DATETIME NOT NULL,
  source      ENUM('WS','REST') NOT NULL DEFAULT 'WS',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_exec (account_id, ord_no, cntr_no),
  KEY ix_exec_time (account_id, executed_at),
  KEY ix_exec_stk (stk_cd, executed_at),
  CONSTRAINT fk_exec_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='체결 내역 (WS 00 주문체결 / ka10076)';

CREATE TABLE IF NOT EXISTS trade_ledger (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id    INT UNSIGNED NOT NULL,
  trde_dt       DATE NOT NULL,
  trde_no       VARCHAR(20) NOT NULL,
  trde_kind_nm  VARCHAR(30) NULL COMMENT '거래종류명(매수/매도/입금...)',
  rmrk_nm       VARCHAR(80) NULL COMMENT '적요명',
  stk_cd        VARCHAR(12) NULL,
  stk_nm        VARCHAR(60) NULL,
  trde_qty      BIGINT NULL,
  trde_unit     BIGINT NULL COMMENT '거래단가',
  trde_amt      BIGINT NULL COMMENT '거래금액',
  cmsn          BIGINT NULL,
  tax           BIGINT NULL COMMENT '거래및농특세+소득세 합',
  exct_amt      BIGINT NULL COMMENT '정산금액',
  entra_remn    BIGINT NULL COMMENT '거래후 예수금잔고',
  proc_tm       VARCHAR(20) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_ledger (account_id, trde_dt, trde_no),
  KEY ix_ledger_stk (stk_cd, trde_dt),
  CONSTRAINT fk_ledger_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='위탁종합거래내역 (kt00015) - 증권사 정본 거래내역';

CREATE TABLE IF NOT EXISTS daily_trade_summary (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id    INT UNSIGNED NOT NULL,
  base_dt       DATE NOT NULL,
  stk_cd        VARCHAR(12) NOT NULL,
  stk_nm        VARCHAR(60) NULL,
  buy_qty       BIGINT NULL,
  buy_avg_pric  BIGINT NULL,
  buy_amt       BIGINT NULL,
  sell_qty      BIGINT NULL,
  sell_avg_pric BIGINT NULL,
  sell_amt      BIGINT NULL,
  cmsn_tax      BIGINT NULL COMMENT '수수료+제세금',
  pl_amt        BIGINT NULL COMMENT '손익금액',
  prft_rt       DECIMAL(12,4) NULL COMMENT '수익률(%)',
  PRIMARY KEY (id),
  UNIQUE KEY uq_dts (account_id, base_dt, stk_cd),
  CONSTRAINT fk_dts_account FOREIGN KEY (account_id) REFERENCES account (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='당일매매일지 (ka10170) 종목별 일 손익';

-- ---------------------------------------------------------------------
-- 5. 알고리즘 / 파라미터   (UI에서 선택·편집)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algorithm (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  code         VARCHAR(50)  NOT NULL COMMENT '코드 식별자 (server/stock_svr/algo 모듈과 매칭)',
  name         VARCHAR(100) NOT NULL,
  role         ENUM('entry','risk','filter') NOT NULL COMMENT 'entry=진입, risk=리스크관리(물타기/손절), filter=시장국면 필터',
  description  VARCHAR(500) NULL,
  is_locked    TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1=항상 활성(비활성화 불가, 예: risk_guard)',
  sort_order   SMALLINT NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_algorithm_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='알고리즘 목록';

CREATE TABLE IF NOT EXISTS algorithm_param_def (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  algorithm_id  INT UNSIGNED NOT NULL,
  param_key     VARCHAR(50) NOT NULL,
  label         VARCHAR(100) NOT NULL COMMENT 'UI 표시명',
  value_type    ENUM('int','decimal','bool','string','enum','time') NOT NULL,
  default_value VARCHAR(100) NOT NULL,
  min_value     VARCHAR(50) NULL,
  max_value     VARCHAR(50) NULL,
  enum_options  VARCHAR(500) NULL COMMENT "enum 선택지 'value:라벨,value:라벨'",
  unit          VARCHAR(20) NULL,
  description   VARCHAR(300) NULL,
  sort_order    SMALLINT NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_param_def (algorithm_id, param_key),
  CONSTRAINT fk_paramdef_algo FOREIGN KEY (algorithm_id) REFERENCES algorithm (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='파라미터 정의 (편집 UI가 이 정의로 폼을 동적 생성)';

CREATE TABLE IF NOT EXISTS algorithm_param_value (
  algorithm_id  INT UNSIGNED NOT NULL,
  param_key     VARCHAR(50) NOT NULL,
  value         VARCHAR(100) NOT NULL,
  updated_by    VARCHAR(50) NULL,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (algorithm_id, param_key),
  CONSTRAINT fk_paramval_algo FOREIGN KEY (algorithm_id) REFERENCES algorithm (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='파라미터 현재값';

CREATE TABLE IF NOT EXISTS algorithm_param_history (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  algorithm_id  INT UNSIGNED NOT NULL,
  param_key     VARCHAR(50) NOT NULL,
  old_value     VARCHAR(100) NULL,
  new_value     VARCHAR(100) NOT NULL,
  changed_by    VARCHAR(50) NULL,
  changed_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_param_hist (algorithm_id, param_key, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='파라미터 변경 이력';

CREATE TABLE IF NOT EXISTS algorithm_selection (
  algorithm_id  INT UNSIGNED NOT NULL,
  is_enabled    TINYINT(1) NOT NULL DEFAULT 0,
  priority      SMALLINT NOT NULL DEFAULT 100 COMMENT '작을수록 먼저 평가',
  updated_by    VARCHAR(50) NULL,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (algorithm_id),
  CONSTRAINT fk_algosel_algo FOREIGN KEY (algorithm_id) REFERENCES algorithm (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='사용 알고리즘 선택(조합 가능)';

CREATE TABLE IF NOT EXISTS signal_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id      BIGINT UNSIGNED NULL,
  algo_code   VARCHAR(50) NOT NULL,
  stk_cd      VARCHAR(12) NOT NULL,
  stk_nm      VARCHAR(60) NULL,
  signal_type ENUM('BUY','SELL','HOLD','BLOCK') NOT NULL COMMENT 'BLOCK=리스크/필터로 차단됨',
  score       DECIMAL(14,4) NULL,
  detail      VARCHAR(500) NULL,
  order_id    BIGINT UNSIGNED NULL COMMENT '연결된 주문',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_signal_time (created_at),
  KEY ix_signal_stk (stk_cd, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='알고리즘 신호 기록';

-- ---------------------------------------------------------------------
-- 6. 시스템 / 관제
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_setting (
  setting_key  VARCHAR(50) NOT NULL,
  value        VARCHAR(255) NOT NULL,
  description  VARCHAR(300) NULL,
  updated_by   VARCHAR(50) NULL,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='전역 설정 (order_enabled=긴급정지 스위치 등)';

CREATE TABLE IF NOT EXISTS server_status (
  component    VARCHAR(30) NOT NULL COMMENT 'server / db / kiwoom_rest / kiwoom_ws / market',
  status       ENUM('ok','warn','error','unknown') NOT NULL DEFAULT 'unknown',
  message      VARCHAR(255) NULL,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (component)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='서버 하트비트/연결상태 (웹 관제 화면용)';

CREATE TABLE IF NOT EXISTS event_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  level       ENUM('DEBUG','INFO','WARN','ERROR') NOT NULL DEFAULT 'INFO',
  category    VARCHAR(30) NOT NULL COMMENT 'system/auth/order/algo/sync/ws ...',
  message     VARCHAR(500) NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_event_time (created_at),
  KEY ix_event_level (level, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='주요 이벤트 기록 (서버 UI/웹 출력용, 1주일 보관)';

CREATE TABLE IF NOT EXISTS api_call_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  api_id      VARCHAR(10) NOT NULL,
  http_status SMALLINT NULL,
  return_code INT NULL,
  return_msg  VARCHAR(255) NULL,
  elapsed_ms  INT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_apilog_time (created_at),
  KEY ix_apilog_api (api_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='키움 API 호출 로그 (Rate limit 실측용, 1주일 보관). 요청/응답 본문·토큰은 저장하지 않음';

-- ---------------------------------------------------------------------
-- 7. Claude 검토(거부권 필터) 판단 기록
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_decision_log (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  run_id         BIGINT UNSIGNED NULL,
  stk_cd         VARCHAR(12) NOT NULL,
  stk_nm         VARCHAR(60) NULL,
  source_algo    VARCHAR(50) NULL COMMENT '검토 대상 신호를 낸 알고리즘 code',
  side           ENUM('BUY','SELL') NOT NULL DEFAULT 'BUY',
  model          VARCHAR(50) NOT NULL,
  decision       ENUM('allow','block','error') NOT NULL COMMENT 'error=호출 실패/무효 응답(fail_mode 적용)',
  final_action   ENUM('pass','block') NOT NULL COMMENT '실제 파이프라인 결과(신뢰도·fail_mode 적용 후)',
  confidence     SMALLINT NULL COMMENT '0~100',
  reasons        VARCHAR(1000) NULL COMMENT 'Claude 가 제시한 근거(요약)',
  risk_flags     VARCHAR(300) NULL,
  input_summary  TEXT NULL COMMENT 'Claude 에 보낸 입력 요약(JSON). 계좌번호·잔고·키 미포함',
  from_cache     TINYINT(1) NOT NULL DEFAULT 0,
  latency_ms     INT NULL,
  input_tokens   INT NULL,
  output_tokens  INT NULL,
  error_msg      VARCHAR(255) NULL,
  order_id       BIGINT UNSIGNED NULL,
  PRIMARY KEY (id),
  KEY ix_llm_time (created_at),
  KEY ix_llm_stk (stk_cd, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Claude 거부권 필터 판단 기록(모델·근거·토큰·결과). 1주일 이상 보관 가능';

-- ---------------------------------------------------------------------
-- 8. 거래 분석용 기록 (영구 보관 — 7일 로그 정리 대상 아님)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_event (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NULL COMMENT 'orders.id',
  account_id    INT UNSIGNED NULL,
  ord_no        VARCHAR(20) NULL,
  event_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  event_type    ENUM('CREATED','SENT','ACCEPTED','PARTIAL','FILLED','CANCELED','REJECTED','FAILED','UNKNOWN','MODIFIED','NOTE') NOT NULL,
  status        VARCHAR(20) NULL COMMENT '이 시점의 orders.status',
  filled_qty    BIGINT NULL,
  remain_qty    BIGINT NULL,
  price         BIGINT NULL COMMENT '체결가/주문가',
  reject_reason VARCHAR(255) NULL COMMENT '거래소 거부사유(WS 919 등)',
  return_code   INT NULL,
  message       VARCHAR(255) NULL,
  source        ENUM('EXECUTOR','WS','REST') NOT NULL DEFAULT 'EXECUTOR',
  PRIMARY KEY (id),
  KEY ix_oe_order (order_id, event_time),
  KEY ix_oe_time (event_time),
  KEY ix_oe_ordno (account_id, ord_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='주문 상태 변화 이력(접수→부분체결→체결/거부/취소). 영구 보관';

CREATE TABLE IF NOT EXISTS event_archive (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  level       ENUM('DEBUG','INFO','WARN','ERROR') NOT NULL,
  category    VARCHAR(30) NOT NULL,
  message     VARCHAR(500) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_ea_time (created_at),
  KEY ix_ea_cat (category, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='주요 이벤트 영구 보관본(WARN/ERROR + 주문·알고리즘·엔진 이벤트). event_log 는 7일 정리, 이 테이블은 archive_retention_days(기본 365일)';

CREATE TABLE IF NOT EXISTS api_error_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  api_id      VARCHAR(10) NOT NULL,
  http_status SMALLINT NULL,
  return_code INT NULL,
  return_msg  VARCHAR(255) NULL,
  elapsed_ms  INT NULL,
  PRIMARY KEY (id),
  KEY ix_ael_time (created_at),
  KEY ix_ael_api (api_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='키움 API 오류 응답만 영구 보관(정상 호출 로그 api_call_log 는 7일 정리)';

-- orders 확장 (재실행 안전). 신호 시점 맥락 저장 → 슬리피지·당시 설정 분석용
ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS signal_price   BIGINT NULL COMMENT '신호 발생 시점 기준가(슬리피지 계산용)',
  ADD COLUMN IF NOT EXISTS signal_context TEXT NULL COMMENT '신호 맥락 JSON(kind/score/meta 등)',
  ADD COLUMN IF NOT EXISTS params_snapshot TEXT NULL COMMENT '주문 시점 알고리즘 파라미터 JSON(risk_guard+진입알고리즘)',
  ADD COLUMN IF NOT EXISTS reject_reason  VARCHAR(255) NULL COMMENT '거래소 거부사유';

-- Claude 등 외부 분석용: 신호 → 주문 → 체결 → Claude 판단을 한 줄로
CREATE OR REPLACE VIEW v_trade_analysis AS
SELECT
  s.id AS signal_id, s.created_at AS signal_time, s.algo_code, s.signal_type,
  s.stk_cd, s.stk_nm, s.score, s.detail AS signal_detail,
  o.id AS order_id, o.ord_no, o.side, o.order_kind, o.status AS order_status, o.is_dry_run,
  o.trde_tp, o.ord_qty, o.ord_uv, o.signal_price, o.filled_qty, o.avg_fill_pric,
  CASE WHEN o.signal_price > 0 AND o.avg_fill_pric > 0
       THEN ROUND((o.avg_fill_pric - o.signal_price) / o.signal_price * 100, 3) END AS slippage_pct,
  o.return_code, o.return_msg, o.reject_reason, o.reason AS order_reason,
  o.signal_context, o.params_snapshot, o.created_at AS order_time,
  (SELECT COUNT(*) FROM executions e WHERE e.account_id = o.account_id AND e.ord_no = o.ord_no) AS exec_cnt,
  (SELECT SUM(e.cntr_qty * e.cntr_pric) FROM executions e WHERE e.account_id = o.account_id AND e.ord_no = o.ord_no) AS exec_amount,
  (SELECT SUM(COALESCE(e.cmsn,0) + COALESCE(e.tax,0)) FROM executions e WHERE e.account_id = o.account_id AND e.ord_no = o.ord_no) AS exec_fee_tax,
  l.model AS llm_model, l.decision AS llm_decision, l.final_action AS llm_final,
  l.confidence AS llm_confidence, l.reasons AS llm_reasons
FROM signal_log s
LEFT JOIN orders o ON o.id = s.order_id
LEFT JOIN llm_decision_log l ON l.id = COALESCE(
  (SELECT MAX(x.id) FROM llm_decision_log x WHERE x.order_id = o.id),
  (SELECT MAX(x.id) FROM llm_decision_log x
    WHERE x.stk_cd = s.stk_cd AND x.order_id IS NULL
      AND x.created_at BETWEEN s.created_at - INTERVAL 2 MINUTE AND s.created_at + INTERVAL 2 MINUTE));

-- ---------------------------------------------------------------------
-- 9. 산업 트렌드 스캔 (Claude, 하루 1회 — claude_trend_scan 알고리즘)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trend_scan_run (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  scan_date         DATE NOT NULL,
  started_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at       DATETIME NULL,
  status            ENUM('ok','partial','error') NOT NULL DEFAULT 'ok',
  region_scope      VARCHAR(20) NOT NULL DEFAULT 'domestic_global',
  model             VARCHAR(50) NOT NULL,
  domestic_theme_summary TEXT NULL COMMENT 'ka90001 상위 테마 요약(1단계 조사 프롬프트에 넣은 근거 데이터, 감사용)',
  research_summary  TEXT NULL COMMENT '1단계(웹 조사) 응답 원문 요약(감사용, 비밀값 없음)',
  candidate_count   INT NOT NULL DEFAULT 0,
  signal_count      INT NOT NULL DEFAULT 0 COMMENT '실제 매수 신호로 이어진 후보 수',
  web_search_count  INT NULL,
  input_tokens       INT NULL,
  output_tokens      INT NULL,
  latency_ms         INT NULL,
  error_msg          VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_trend_scan_date (scan_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='산업 트렌드 스캔 1일 1회 실행 기록(claude_trend_scan)';

CREATE TABLE IF NOT EXISTS trend_scan_candidate (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id          BIGINT UNSIGNED NOT NULL,
  region          ENUM('domestic','global') NOT NULL,
  theme           VARCHAR(120) NOT NULL,
  rationale       VARCHAR(500) NULL,
  confidence      SMALLINT NULL COMMENT '0~100',
  kiwoom_theme_cd VARCHAR(20) NULL COMMENT '국내 테마가 ka90001 테마그룹과 매칭된 경우',
  kiwoom_theme_nm VARCHAR(60) NULL,
  stk_cd          VARCHAR(12) NULL COMMENT 'stock_master 매칭 성공 시에만 채움(미매칭은 NULL - 추측 금지)',
  stk_nm          VARCHAR(60) NULL,
  match_status    ENUM('kiwoom_theme_member','name_matched','unmatched') NOT NULL,
  signal_id       BIGINT UNSIGNED NULL COMMENT 'signal_log.id (매수 신호로 이어졌으면)',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_tsc_run (run_id),
  KEY ix_tsc_stk (stk_cd, created_at),
  CONSTRAINT fk_tsc_run FOREIGN KEY (run_id) REFERENCES trend_scan_run (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='트렌드 스캔이 골라낸 테마/후보종목(매칭 실패도 투명하게 기록, 웹에서 조회)';

-- trend_scan_run 은 scan_date UNIQUE(하루 1개 = "그날의 최신 공식 결과")를 그대로 유지한다.
-- 웹에서 수동 재조사를 요청하면 이 행을 그대로 덮어쓴다(trigger_type='manual'). 실패했던 이전
-- 시도의 내용은 아래 trend_scan_attempt(append-only 감사로그)에 남아 사라지지 않는다.
ALTER TABLE trend_scan_run
  ADD COLUMN IF NOT EXISTS trigger_type ENUM('scheduled','manual') NOT NULL DEFAULT 'scheduled' AFTER scan_date,
  ADD COLUMN IF NOT EXISTS requested_by VARCHAR(50) NULL COMMENT '수동 재조사를 누른 웹 사용자(scheduled 는 NULL)' AFTER trigger_type;

CREATE TABLE IF NOT EXISTS trend_scan_attempt (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  scan_date         DATE NOT NULL,
  trigger_type      ENUM('scheduled','manual') NOT NULL,
  requested_by      VARCHAR(50) NULL,
  status            ENUM('ok','partial','error') NOT NULL,
  region_scope      VARCHAR(20) NULL,
  model             VARCHAR(50) NULL,
  candidate_count   INT NULL,
  web_search_count  INT NULL,
  input_tokens      INT NULL,
  output_tokens     INT NULL,
  latency_ms        INT NULL,
  error_msg         VARCHAR(255) NULL,
  research_summary  TEXT NULL COMMENT '1단계 조사 원문 스냅샷(실패 사유 포함, 감사용)',
  started_at        DATETIME NOT NULL,
  finished_at       DATETIME NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_tsa_date (scan_date, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='산업 트렌드 스캔의 모든 시도(자동+수동) 이력. trend_scan_run 은 하루 최신 결과만 남기지만, 이 테이블은 실패한 시도도 영구 보존해 투명성을 유지한다';

CREATE TABLE IF NOT EXISTS trend_scan_request (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  requested_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  requested_by  VARCHAR(50) NOT NULL COMMENT '웹 로그인 사용자명(admin)',
  status        ENUM('pending','processing','done','error') NOT NULL DEFAULT 'pending',
  run_id        BIGINT UNSIGNED NULL COMMENT '처리 완료 시 trend_scan_run.id',
  error_msg     VARCHAR(255) NULL,
  processed_at  DATETIME NULL,
  PRIMARY KEY (id),
  KEY ix_tsr_status (status, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='웹의 "지금 다시 조사" 버튼이 남기는 요청. 서버가 주기적으로 확인해 처리하고 상태를 갱신한다(웹은 Anthropic/키움 자격증명이 없어 직접 실행 불가)';

SET FOREIGN_KEY_CHECKS = 1;
