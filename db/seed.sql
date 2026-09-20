-- =====================================================================
-- 초기 데이터 (재실행 안전: INSERT IGNORE / ON DUPLICATE KEY)
-- 안전 기본값 원칙: 주문 비활성(order_enabled=0), 모의투자(trading_mode=mock)
-- =====================================================================
SET NAMES utf8mb4;

-- ---- 전역 설정 -------------------------------------------------------
INSERT IGNORE INTO system_setting (setting_key, value, description) VALUES
 ('trading_mode',        'mock', '거래 환경: mock(모의투자) / real(실계좌)'),
 ('order_enabled',       '0',    '주문 전송 허용 (0=신호만 기록·주문 미전송, 1=전송). 긴급정지 스위치'),
 ('real_trading_confirm','0',    '실계좌 주문 이중확인 (trading_mode=real 이면서 이 값이 1이어야 실주문 전송)'),
 ('poll_interval_sec',   '30',   '알고리즘 평가 주기(초)'),
 ('log_retention_days',  '7',    '로그(파일/DB) 보관 기간(일)');

-- ---- 알고리즘 ---------------------------------------------------------
INSERT INTO algorithm (code, name, role, description, is_locked, sort_order) VALUES
 ('risk_guard',          '리스크 가드(전역 한도)',   'risk',
  '총 투입한도·종목당 한도·손절선·일 손실한도·주문횟수 제한. 모든 주문 전에 항상 평가되며 비활성화할 수 없다.', 1, 1),
 ('momentum_screen',     '모멘텀 스크리닝',          'entry',
  '전일대비 등락률 상위(ka10027) + 거래량 급증(ka10023) 교집합 종목을 후보로 매수. 추격매수 고점 리스크에 유의.', 0, 10),
 ('volatility_breakout', '변동성 돌파',              'entry',
  '당일 시가 + 전일 변동폭(고가-저가) x K 를 상향 돌파하면 매수, 지정 시각에 청산(일봉 ka10081 사용).', 0, 20),
 ('averaging_down',      '분할매수(물타기)',         'risk',
  '보유종목이 평단 대비 지정 % 하락하면 추가매수. 최대 횟수·종목당 투입한도를 넘으면 중단하고 손절선 도달 시 전량 매도.', 0, 30),
 ('ma_cross_filter',     '이동평균 크로스 필터',     'filter',
  '단기 이동평균이 장기 이동평균 아래(데드크로스)인 종목의 신규 진입을 차단하는 시장국면 보조 필터(일봉 ka10081 사용).', 0, 40),
 ('claude_advisor',      'Claude 거부권 필터',       'filter',
  'risk_guard 까지 모두 통과해 곧 주문될 **매수 신호만** Claude API 로 한 번 더 검토해 위험하면 차단한다(1단계 거부권). 매도·손절·청산 신호는 검토하지 않으며, 오류·저확신·상한 초과 시 기본값은 차단(fail_mode). 종목 시세와 최근 일봉만 전송하고 계좌·잔고·키는 전송하지 않는다.', 0, 50)
ON DUPLICATE KEY UPDATE name=VALUES(name), role=VALUES(role), description=VALUES(description),
                        is_locked=VALUES(is_locked), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: risk_guard ---------------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'max_total_invest'   k,'총 투입 한도'          label,'int'     t,'1000000' d,'1' mn,'1000000000' mx,NULL eo,'원'  u,'전체 계좌에서 알고리즘이 매수에 쓸 수 있는 총 금액 한도' ds,1 so UNION ALL
 SELECT 'max_invest_per_stock','종목당 최대 투입금',   'int',    '300000',  '1','1000000000',NULL,'원','한 종목에 누적 투입할 수 있는 최대 금액(물타기 포함). 실제 한도는 비중 한도와 비교해 더 작은 값',2 UNION ALL
 SELECT 'max_total_invest_pct','총 투입 비중',         'decimal','100',     '0.1','100',      NULL,'%','총자산(추정예탁자산) 대비 알고리즘 전체 누적 투입 상한. 실제 한도 = min(절대한도, 총자산x비중)',21 UNION ALL
 SELECT 'max_invest_per_stock_pct','종목당 최대 투입 비중','decimal','10',   '0.1','100',      NULL,'%','총자산(추정예탁자산) 대비 한 종목 누적 투입 상한. 실제 한도 = min(절대한도, 총자산x비중)',22 UNION ALL
 SELECT 'stop_loss_pct',       '손절 라인',            'decimal','-15',     '-99','-0.1',     NULL,'%','평단 대비 이 수익률 이하가 되면 전량 매도 (0 불가·반드시 음수)',3 UNION ALL
 SELECT 'daily_loss_limit_pct','일 손실 한도',         'decimal','-3',      '-99','-0.1',     NULL,'%','당일 계좌 손실률이 이 값 이하이면 당일 신규매수 중단 (0 불가·반드시 음수)',4 UNION ALL
 SELECT 'max_orders_per_day',  '일 최대 주문 횟수',    'int',    '30',      '1','1000',       NULL,'회','당일 전송하는 주문 수 상한',5 UNION ALL
 SELECT 'trade_start_time',    '매매 시작 시각',       'time',   '09:05',   NULL,NULL,        NULL,'HH:MM','이 시각 이전에는 신규 진입 금지(장 초반 변동성 회피)',6 UNION ALL
 SELECT 'trade_end_time',      '신규진입 종료 시각',   'time',   '15:15',   NULL,NULL,        NULL,'HH:MM','이 시각 이후에는 신규 진입 금지',7 UNION ALL
 SELECT 'exchange',            '거래소',               'enum',   'KRX',     NULL,NULL,        'KRX:KRX,NXT:NXT,SOR:SOR',NULL,'주문 시 국내거래소구분 (기본 KRX)',8
) p ON a.code='risk_guard'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: momentum_screen ----------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'market'            k,'시장구분'            label,'enum'    t,'000' d,NULL mn,NULL mx,'000:전체,001:코스피,101:코스닥' eo,NULL u,'조회 대상 시장 (mrkt_tp)' ds,1 so UNION ALL
 SELECT 'min_flu_rt',        '최소 상승률',         'decimal','3',    '0','30',    NULL,'%','전일대비 등락률이 이 값 이상인 종목만 후보',2 UNION ALL
 SELECT 'max_flu_rt',        '최대 상승률',         'decimal','15',   '1','30',    NULL,'%','이 값을 넘는 급등 종목은 추격매수 방지를 위해 제외',3 UNION ALL
 SELECT 'min_volume_surge_rt','최소 거래량 급증률', 'decimal','100',  '0','100000',NULL,'%','거래량 급증률(sdnin_rt) 하한',4 UNION ALL
 SELECT 'min_trde_qty',      '최소 거래량',         'int',    '100000','0','1000000000',NULL,'주','현재 거래량 하한 (유동성 확보)',5 UNION ALL
 SELECT 'min_price',         '최소 주가',           'int',    '1000', '0','1000000',NULL,'원','동전주 제외',6 UNION ALL
 SELECT 'exclude_etf',       'ETF/ETN/스팩 제외',   'bool',   '1',    NULL,NULL,   NULL,NULL,'1이면 ETF·ETN·스팩 종목 제외',7 UNION ALL
 SELECT 'top_n',             '후보 상위 N개',       'int',    '5',    '1','50',    NULL,'개','평가 주기마다 검토할 상위 종목 수',8 UNION ALL
 SELECT 'buy_amount',        '1회 매수금액',        'int',    '100000','10000','100000000',NULL,'원','신규 진입 시 종목당 최초 매수금액',9 UNION ALL
 SELECT 'max_new_per_day',   '일 신규 진입 종목수', 'int',    '3',    '1','100',   NULL,'종목','하루에 새로 진입할 최대 종목 수 (0 불가)',10 UNION ALL
 SELECT 'order_type',        '주문 유형',           'enum',   '3',    NULL,NULL,   '3:시장가,0:지정가(보통),6:최유리지정가',NULL,'kt10000 trde_tp',11
) p ON a.code='momentum_screen'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: volatility_breakout ------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'k_value'           k,'K 값'                label,'decimal' t,'0.5' d,'0.1' mn,'1.5' mx,NULL eo,NULL u,'돌파 목표가 = 당일 시가 + (전일 고가-전일 저가) x K' ds,1 so UNION ALL
 SELECT 'min_range_pct',     '최소 전일 변동폭',    'decimal','1.5',  '0','30',  NULL,'%','전일 변동폭(고저차/종가)이 이 값 미만이면 제외(변동성 부족)',2 UNION ALL
 SELECT 'watch_symbols',     '감시 종목(콤마구분)', 'string', '',     NULL,NULL, NULL,NULL,'비우면 모멘텀 후보/보유종목 대상. 예: 005930,000660',3 UNION ALL
 SELECT 'buy_amount',        '1회 매수금액',        'int',    '100000','10000','100000000',NULL,'원','돌파 시 매수금액',4 UNION ALL
 SELECT 'liquidate_time',    '청산 시각',           'time',   '15:15',NULL,NULL, NULL,'HH:MM','이 시각에 이 알고리즘으로 진입한 포지션을 전량 청산(빈값이면 청산 안 함)',5 UNION ALL
 SELECT 'order_type',        '주문 유형',           'enum',   '3',    NULL,NULL, '3:시장가,0:지정가(보통),6:최유리지정가',NULL,'kt10000 trde_tp',6
) p ON a.code='volatility_breakout'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: averaging_down -----------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'drop_pct'          k,'물타기 하락률'       label,'decimal' t,'10' d,'1' mn,'50' mx,NULL eo,'%' u,'마지막 매수가 대비 이 % 이상 하락하면 추가매수' ds,1 so UNION ALL
 SELECT 'step_buy_amount',   '1회 추가매수금액',    'int',    '100000','10000','100000000',NULL,'원','물타기 1회당 매수금액',2 UNION ALL
 SELECT 'max_steps',         '최대 물타기 횟수',    'int',    '3',    '0','10',    NULL,'회','종목당 추가매수 최대 횟수 (0이면 물타기 안 함) - 무한 물타기 방지',3 UNION ALL
 SELECT 'cooldown_min',      '재시도 대기시간',     'int',    '30',   '0','1440',  NULL,'분','직전 물타기 후 이 시간 동안은 추가매수 금지',4 UNION ALL
 SELECT 'order_type',        '주문 유형',           'enum',   '3',    NULL,NULL,   '3:시장가,0:지정가(보통),6:최유리지정가',NULL,'kt10000 trde_tp',5
) p ON a.code='averaging_down'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: ma_cross_filter ----------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'short_period'      k,'단기 이동평균 기간' label,'int' t,'5' d,'2' mn,'60' mx,NULL eo,'일' u,'단기 MA 기간' ds,1 so UNION ALL
 SELECT 'long_period',       '장기 이동평균 기간', 'int',    '20',   '5','240',   NULL,'일','장기 MA 기간 (단기보다 커야 함)',2 UNION ALL
 SELECT 'block_mode',        '차단 방식',          'enum',   'below','',NULL,     'below:단기<장기이면 진입차단,cross_down:데드크로스 직후만 차단',NULL,'신규 진입 차단 조건',3
) p ON a.code='ma_cross_filter'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 파라미터 정의: claude_advisor -----------------------------------
INSERT INTO algorithm_param_def (algorithm_id, param_key, label, value_type, default_value, min_value, max_value, enum_options, unit, description, sort_order)
SELECT a.id, p.k, p.label, p.t, p.d, p.mn, p.mx, p.eo, p.u, p.ds, p.so FROM algorithm a JOIN (
 SELECT 'model'             k,'검토 모델'           label,'enum' t,'claude-opus-5' d,NULL mn,NULL mx,'claude-opus-5:Claude Opus 5,claude-sonnet-5:Claude Sonnet 5,claude-haiku-4-5:Claude Haiku 4.5' eo,NULL u,'검토에 사용할 Claude 모델. 정확도는 Opus > Sonnet > Haiku, 비용/속도는 반대' ds,1 so UNION ALL
 SELECT 'effort',            '사고 강도',          'enum',   'low',  NULL,NULL, 'low:낮음(빠름·저비용),medium:보통,high:높음(정밀·고비용)',NULL,'모델이 쓰는 추론량. Haiku 4.5 는 이 옵션을 지원하지 않아 무시된다',2 UNION ALL
 SELECT 'min_confidence',    '최소 확신도',        'int',    '70',   '0','100',  NULL,'점','allow 판정이라도 확신도가 이 값 미만이면 차단한다',3 UNION ALL
 SELECT 'fail_mode',         '오류 시 처리',       'enum',   'block',NULL,NULL,  'block:차단(안전),allow:통과',NULL,'API 연결·타임아웃·5xx 등 **인프라 오류**일 때의 처리. 거부·무효응답·명시적 block 은 이 값과 무관하게 항상 차단',4 UNION ALL
 SELECT 'cache_minutes',     '결과 캐시 시간',     'int',    '30',   '0','240',  NULL,'분','같은 종목·방향·출처 알고리즘의 판단을 이 시간 동안 재사용(0이면 매번 호출)',5 UNION ALL
 SELECT 'max_calls_per_day', '일 최대 호출 수',    'int',    '50',   '1','500',  NULL,'회','당일 Claude 호출 상한. 초과하면 오류 시 처리(fail_mode)를 따른다',6 UNION ALL
 SELECT 'timeout_sec',       '응답 대기 시간',     'int',    '30',   '5','120',  NULL,'초','이 시간 안에 응답이 없으면 인프라 오류로 처리',7 UNION ALL
 SELECT 'review_averaging_down','물타기도 검토',   'bool',   '1',    NULL,NULL,  NULL,NULL,'1이면 분할매수(물타기) 추가매수 신호도 검토한다(0이면 신규 진입만 검토)',8
) p ON a.code='claude_advisor'
ON DUPLICATE KEY UPDATE label=VALUES(label), value_type=VALUES(value_type), default_value=VALUES(default_value),
  min_value=VALUES(min_value), max_value=VALUES(max_value), enum_options=VALUES(enum_options), unit=VALUES(unit),
  description=VALUES(description), sort_order=VALUES(sort_order);

-- ---- 현재값 = 기본값 (이미 값이 있으면 보존) --------------------------
INSERT IGNORE INTO algorithm_param_value (algorithm_id, param_key, value, updated_by)
SELECT algorithm_id, param_key, default_value, 'seed' FROM algorithm_param_def;

-- ---- 알고리즘 선택 기본값: risk_guard만 켬 (나머지는 UI에서 명시적으로 선택) ----
INSERT IGNORE INTO algorithm_selection (algorithm_id, is_enabled, priority, updated_by)
SELECT id, IF(code='risk_guard',1,0), sort_order, 'seed' FROM algorithm;

-- ---- 서버 상태 초기 행 ------------------------------------------------
INSERT IGNORE INTO server_status (component, status, message) VALUES
 ('server','unknown','서버 미기동'),
 ('auto_trading','unknown','중지'),
 ('db','unknown',NULL),
 ('kiwoom_rest','unknown',NULL),
 ('kiwoom_ws','unknown',NULL),
 ('market','unknown',NULL);
