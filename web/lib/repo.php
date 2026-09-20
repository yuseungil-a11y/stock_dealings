<?php
declare(strict_types=1);
/**
 * 조회 전용 리포지토리.
 * 모든 SQL 은 prepared statement 이며, 사용자 입력은 값 바인딩으로만 들어간다.
 * ORDER BY 등 식별자 자리는 반드시 화이트리스트로 매핑한다.
 */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

const PAGE_SIZE = 50;

/* ------------------------------------------------------------------ 계좌 */

function repo_accounts(): array
{
    return db_all(
        "SELECT id, account_no, env, alias, is_active
           FROM account
          ORDER BY (env = 'real') DESC, is_active DESC, id ASC"
    );
}

/** 계좌 선택: 요청값 > 세션 > 기본값(env='real' 우선). */
function repo_resolve_account(array $accounts): ?array
{
    if ($accounts === []) {
        return null;
    }
    $byId = [];
    foreach ($accounts as $a) {
        $byId[(int)$a['id']] = $a;
    }
    $req = isset($_GET['account_id']) ? clean_int($_GET['account_id'], 0) : 0;
    if ($req > 0 && isset($byId[$req])) {
        $_SESSION['account_id'] = $req;
        return $byId[$req];
    }
    $sess = (int)($_SESSION['account_id'] ?? 0);
    if ($sess > 0 && isset($byId[$sess])) {
        return $byId[$sess];
    }
    $first = $accounts[0];
    $_SESSION['account_id'] = (int)$first['id'];
    return $first;
}

function repo_account_label(?array $acct): string
{
    if ($acct === null) {
        return '계좌 없음';
    }
    $env = $acct['env'] === 'real' ? '실전' : '모의';
    $alias = (string)($acct['alias'] ?? '');
    return mask_account((string)$acct['account_no']) . ' [' . $env . ']' . ($alias !== '' ? ' ' . $alias : '');
}

/* ------------------------------------------------------------ 잔고/보유 */

function repo_latest_balance(int $accountId): ?array
{
    return db_row(
        'SELECT * FROM account_balance WHERE account_id = ? ORDER BY snapshot_at DESC, id DESC LIMIT 1',
        [$accountId]
    );
}

/** 일자별 마지막 스냅샷 (추이 차트용). */
function repo_balance_series(int $accountId, int $days = 30): array
{
    return db_all(
        'SELECT b.snapshot_at, b.entr, b.tot_evlt_amt, b.tot_evlt_pl, b.tot_prft_rt, b.prsm_dpst_aset_amt
           FROM account_balance b
           JOIN (SELECT MAX(id) AS mid
                   FROM account_balance
                  WHERE account_id = ?
                    AND snapshot_at >= (CURDATE() - INTERVAL ? DAY)
                  GROUP BY DATE(snapshot_at)) x ON x.mid = b.id
          ORDER BY b.snapshot_at ASC',
        [$accountId, $days]
    );
}

function repo_balance_history(int $accountId, int $limit = 100): array
{
    return db_all(
        'SELECT * FROM account_balance WHERE account_id = ? ORDER BY snapshot_at DESC, id DESC LIMIT ?',
        [$accountId, $limit]
    );
}

/* --------------------------------------------- 상장폐지 · 거래불가 종목 판정 */

/** 종목명이 이 접두로 시작하면 거래불가(상장폐지) 종목으로 본다. */
const HOLDING_DELISTED_PREFIX = '(폐)';
/** SQL 조건 조각 (값은 항상 바인딩: HOLDING_DELISTED_PREFIX . '%'). */
const HOLDING_DELISTED_COND = '(cur_prc = 0 OR stk_nm LIKE ?)';

function holding_delisted_like(): string
{
    return HOLDING_DELISTED_PREFIX . '%';
}

/**
 * 상장폐지·거래불가 보유종목 판정.
 * 현재가가 0 이거나 종목명이 '(폐)' 로 시작하면 true.
 * (현재가가 NULL 인 경우는 '아직 미동기화' 로 보고 제외한다 — 오탐 방지)
 */
function holding_is_delisted(array $r): bool
{
    $cur = $r['cur_prc'] ?? null;
    if ($cur !== null && is_numeric($cur) && (float)$cur === 0.0) {
        return true;
    }
    return str_starts_with((string)($r['stk_nm'] ?? ''), HOLDING_DELISTED_PREFIX);
}

/**
 * 실제 손익 기준 수익률(%) = 평가손익 / 매입금액 × 100.
 * 키움이 상장폐지 종목에 대해 prft_rt=0.00 을 내려주므로 화면에서는 이 값을 쓴다.
 * 매입금액이 0 이하이면 null.
 */
function holding_real_profit_rate(array $r): ?float
{
    $pur = (float)($r['pur_amt'] ?? 0);
    if ($pur <= 0) {
        return null;
    }
    return ((float)($r['evltv_prft'] ?? 0) / $pur) * 100;
}

/** 보유종목 행에 delisted / real_profit_rate 파생 필드를 덧붙인다(기존 필드 유지). */
function repo_decorate_holding(array $r): array
{
    $r['delisted'] = holding_is_delisted($r);
    $r['real_profit_rate'] = holding_real_profit_rate($r);
    return $r;
}

/** 화면 표시용 수익률: 상장폐지 종목은 실제 손익 기준 수익률을 쓴다. */
function holding_display_rate(array $r): ?float
{
    if (!empty($r['delisted'])) {
        return $r['real_profit_rate'] ?? null;
    }
    return $r['prft_rt'] === null ? null : (float)$r['prft_rt'];
}

/** 상장폐지 종목 수익률 설명 문구(툴팁). */
function holding_delisted_note(array $r): string
{
    $cur = $r['cur_prc'] ?? null;
    $zero = $cur !== null && is_numeric($cur) && (float)$cur === 0.0;
    return ($zero ? '현재가 0 — 상장폐지 종목' : '거래불가(상장폐지) 종목')
        . ' · 키움 수익률(0.00%) 대신 실제 손익 기준(평가손익 ÷ 매입금액)으로 표시합니다.';
}

const HOLDING_SORTS = [
    'prft_rt_desc' => 'prft_rt DESC',
    'prft_rt_asc' => 'prft_rt ASC',
    'evlt_amt_desc' => 'evlt_amt DESC',
    'evlt_amt_asc' => 'evlt_amt ASC',
    'evltv_prft_desc' => 'evltv_prft DESC',
    'evltv_prft_asc' => 'evltv_prft ASC',
    'stk_nm_asc' => 'stk_nm ASC',
];

/**
 * 정렬 SQL 조각 + 바인딩 값.
 * 수익률 정렬은 상장폐지 종목의 잘못된 prft_rt(0.00) 대신 실제 손익 기준 수익률로 정렬한다.
 * @return array{0:string,1:array}
 */
function repo_holding_order(string $sort): array
{
    if ($sort === 'prft_rt_desc' || $sort === 'prft_rt_asc') {
        $dir = $sort === 'prft_rt_asc' ? 'ASC' : 'DESC';
        return ['(CASE WHEN ' . HOLDING_DELISTED_COND . ' AND pur_amt > 0'
            . ' THEN (evltv_prft / pur_amt) * 100 ELSE prft_rt END) ' . $dir, [holding_delisted_like()]];
    }
    return [HOLDING_SORTS[$sort] ?? HOLDING_SORTS['evlt_amt_desc'], []];
}

function repo_holdings(int $accountId, string $sort = 'evlt_amt_desc', string $q = ''): array
{
    [$order, $orderParams] = repo_holding_order($sort);
    $sql = 'SELECT * FROM holding WHERE account_id = ?';
    $params = [$accountId];
    if ($q !== '') {
        $sql .= ' AND (stk_cd LIKE ? OR stk_nm LIKE ?)';
        $like = '%' . $q . '%';
        $params[] = $like;
        $params[] = $like;
    }
    // ORDER BY 는 WHERE 뒤에 오므로 바인딩 순서도 WHERE 값 다음이다.
    $sql .= ' ORDER BY ' . $order . ', stk_cd ASC';
    return array_map('repo_decorate_holding', db_all($sql, array_merge($params, $orderParams)));
}

/** 보유종목 합계 (+ 상장폐지 종목 제외 합계). */
function repo_holding_totals(int $accountId): array
{
    $like = holding_delisted_like();
    $cond = HOLDING_DELISTED_COND;
    $row = db_row(
        'SELECT COUNT(*) AS cnt, COALESCE(SUM(pur_amt),0) AS pur_amt, COALESCE(SUM(evlt_amt),0) AS evlt_amt,
                COALESCE(SUM(evltv_prft),0) AS evltv_prft,
                COALESCE(SUM(CASE WHEN ' . $cond . ' THEN 1 ELSE 0 END),0) AS delisted_cnt,
                COALESCE(SUM(CASE WHEN ' . $cond . ' THEN 0 ELSE pur_amt END),0) AS pur_amt_ex,
                COALESCE(SUM(CASE WHEN ' . $cond . ' THEN 0 ELSE evlt_amt END),0) AS evlt_amt_ex,
                COALESCE(SUM(CASE WHEN ' . $cond . ' THEN 0 ELSE evltv_prft END),0) AS evltv_prft_ex
           FROM holding WHERE account_id = ?',
        [$like, $like, $like, $like, $accountId]
    );
    $row = $row ?? repo_holding_totals_empty();
    $row['prft_rt'] = ((float)$row['pur_amt']) > 0
        ? ((float)$row['evltv_prft'] / (float)$row['pur_amt']) * 100 : null;
    // 분모(제외 후 매입금액)가 0 이면 보조 표기를 생략하도록 null 을 반환한다.
    $row['prft_rt_ex'] = ((float)$row['pur_amt_ex']) > 0
        ? ((float)$row['evltv_prft_ex'] / (float)$row['pur_amt_ex']) * 100 : null;
    $row['delisted_cnt'] = (int)$row['delisted_cnt'];
    return $row;
}

function repo_holding_totals_empty(): array
{
    return ['cnt' => 0, 'pur_amt' => 0, 'evlt_amt' => 0, 'evltv_prft' => 0, 'prft_rt' => null,
        'delisted_cnt' => 0, 'pur_amt_ex' => 0, 'evlt_amt_ex' => 0, 'evltv_prft_ex' => 0, 'prft_rt_ex' => null];
}

/* --------------------------------------------------------------- 페이징 */

/**
 * 공통 페이지네이션 조회.
 * @return array{rows:array,total:int,page:int,pages:int,size:int}
 */
function repo_paginate(string $selectSql, string $countSql, array $params, int $page, int $size = PAGE_SIZE): array
{
    $total = (int)db_val($countSql, $params, 0);
    $pages = max(1, (int)ceil($total / $size));
    $page = max(1, min($page, $pages));
    $offset = ($page - 1) * $size;
    $rows = db_all($selectSql . ' LIMIT ? OFFSET ?', array_merge($params, [$size, $offset]));
    return ['rows' => $rows, 'total' => $total, 'page' => $page, 'pages' => $pages, 'size' => $size];
}

/** 기간/종목 필터 WHERE 조각 생성. */
function repo_filter(string $dateCol, ?string $from, ?string $to, string $q, array $qCols): array
{
    $where = '';
    $params = [];
    $dateOnly = str_ends_with($dateCol, '_dt');
    if ($from !== null) {
        $where .= ' AND ' . $dateCol . ' >= ?';
        $params[] = $from . ($dateOnly ? '' : ' 00:00:00');
    }
    if ($to !== null) {
        $where .= ' AND ' . $dateCol . ' <= ?';
        $params[] = $to . ($dateOnly ? '' : ' 23:59:59');
    }
    if ($q !== '' && $qCols !== []) {
        $parts = [];
        foreach ($qCols as $c) {
            $parts[] = $c . ' LIKE ?';
            $params[] = '%' . $q . '%';
        }
        $where .= ' AND (' . implode(' OR ', $parts) . ')';
    }
    return [$where, $params];
}

/* ------------------------------------------------------------- 거래 화면 */

function repo_executions(int $accountId, ?string $from, ?string $to, string $q, string $side, int $page): array
{
    [$w, $p] = repo_filter('executed_at', $from, $to, $q, ['stk_cd', 'stk_nm', 'ord_no']);
    $params = array_merge([$accountId], $p);
    if ($side === 'BUY' || $side === 'SELL') {
        $w .= ' AND side = ?';
        $params[] = $side;
    }
    $base = ' FROM executions WHERE account_id = ?' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY executed_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_orders(int $accountId, ?string $from, ?string $to, string $q, string $kind, int $page): array
{
    [$w, $p] = repo_filter('created_at', $from, $to, $q, ['stk_cd', 'stk_nm', 'ord_no', 'algo_code']);
    $params = array_merge([$accountId], $p);
    if ($kind === 'signal') {
        $w .= " AND (is_dry_run = 1 OR status = 'SIGNAL_ONLY')";
    } elseif ($kind === 'sent') {
        $w .= " AND is_dry_run = 0 AND status <> 'SIGNAL_ONLY'";
    }
    $base = ' FROM orders WHERE account_id = ?' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_ledger(int $accountId, ?string $from, ?string $to, string $q, int $page): array
{
    [$w, $p] = repo_filter('trde_dt', $from, $to, $q, ['stk_cd', 'stk_nm', 'trde_kind_nm', 'rmrk_nm']);
    $params = array_merge([$accountId], $p);
    $base = ' FROM trade_ledger WHERE account_id = ?' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY trde_dt DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_daily(int $accountId, ?string $from, ?string $to, string $q, int $page): array
{
    [$w, $p] = repo_filter('base_dt', $from, $to, $q, ['stk_cd', 'stk_nm']);
    $params = array_merge([$accountId], $p);
    $base = ' FROM daily_trade_summary WHERE account_id = ?' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY base_dt DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_daily_totals(int $accountId, ?string $from, ?string $to, string $q): array
{
    [$w, $p] = repo_filter('base_dt', $from, $to, $q, ['stk_cd', 'stk_nm']);
    $row = db_row(
        'SELECT COUNT(*) AS cnt, COALESCE(SUM(buy_amt),0) AS buy_amt, COALESCE(SUM(sell_amt),0) AS sell_amt,
                COALESCE(SUM(cmsn_tax),0) AS cmsn_tax, COALESCE(SUM(pl_amt),0) AS pl_amt
           FROM daily_trade_summary WHERE account_id = ?' . $w,
        array_merge([$accountId], $p)
    );
    return $row ?? ['cnt' => 0, 'buy_amt' => 0, 'sell_amt' => 0, 'cmsn_tax' => 0, 'pl_amt' => 0];
}

/* ------------------------------------------------------------- 전략 화면 */

function repo_algorithms(): array
{
    return db_all(
        'SELECT a.id, a.code, a.name, a.role, a.description, a.is_locked, a.sort_order,
                COALESCE(s.is_enabled, 0) AS is_enabled, COALESCE(s.priority, a.sort_order) AS priority,
                s.updated_by, s.updated_at
           FROM algorithm a
           LEFT JOIN algorithm_selection s ON s.algorithm_id = a.id
          ORDER BY a.sort_order ASC, a.id ASC'
    );
}

function repo_algorithm_by_code(string $code): ?array
{
    return db_row('SELECT * FROM algorithm WHERE code = ? LIMIT 1', [$code]);
}

function repo_params(int $algorithmId): array
{
    return db_all(
        'SELECT d.param_key, d.label, d.value_type, d.default_value, d.min_value, d.max_value,
                d.enum_options, d.unit, d.description, d.sort_order,
                v.value AS current_value, v.updated_by, v.updated_at
           FROM algorithm_param_def d
           LEFT JOIN algorithm_param_value v
                  ON v.algorithm_id = d.algorithm_id AND v.param_key = d.param_key
          WHERE d.algorithm_id = ?
          ORDER BY d.sort_order ASC, d.param_key ASC',
        [$algorithmId]
    );
}

function repo_param_history(int $algorithmId, int $page): array
{
    $base = ' FROM algorithm_param_history WHERE algorithm_id = ?';
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY changed_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        [$algorithmId],
        $page,
        20
    );
}

function repo_signals(?string $from, ?string $to, string $q, string $type, string $algo, int $page): array
{
    [$w, $p] = repo_filter('created_at', $from, $to, $q, ['stk_cd', 'stk_nm', 'detail']);
    $params = $p;
    if (in_array($type, ['BUY', 'SELL', 'HOLD', 'BLOCK'], true)) {
        $w .= ' AND signal_type = ?';
        $params[] = $type;
    }
    if ($algo !== '') {
        $w .= ' AND algo_code = ?';
        $params[] = $algo;
    }
    $base = ' FROM signal_log WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

/* --------------------------------------------- 전략: Claude 판단(거부권 필터) */

/** 결과 필터 화이트리스트. */
const LLM_RESULTS = ['pass', 'block', 'error'];

/** llm_decision_log 조회 가능 여부(표가 없거나 권한이 없으면 화면을 조용히 비운다). */
function repo_llm_available(): bool
{
    static $ok = null;
    if ($ok !== null) {
        return $ok;
    }
    try {
        db_val('SELECT 1 FROM llm_decision_log LIMIT 1', [], null);
        $ok = true;
    } catch (Throwable $e) {
        $ok = false;
    }
    return $ok;
}

/** 결과 필터 → WHERE 조각. 식별자·연산자는 코드에 고정되어 있고 값만 바인딩한다. */
function repo_llm_result_cond(string $result): string
{
    return match ($result) {
        'pass' => " AND final_action = 'pass'",
        'block' => " AND final_action = 'block'",
        'error' => " AND decision = 'error'",
        default => '',
    };
}

function repo_llm_decisions(?string $from, ?string $to, string $q, string $result, int $page): array
{
    if (!repo_llm_available()) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
    }
    [$w, $p] = repo_filter('created_at', $from, $to, $q, ['stk_cd', 'stk_nm']);
    $w .= repo_llm_result_cond($result);
    $base = ' FROM llm_decision_log WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT id, created_at, run_id, stk_cd, stk_nm, source_algo, side, model, decision, final_action,
                confidence, reasons, risk_flags, input_summary, from_cache, latency_ms,
                input_tokens, output_tokens, error_msg, order_id' . $base . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $p,
        $page
    );
}

/**
 * 오늘 Claude 판단 요약 + 전체 건수.
 * 표가 없으면 null (화면/대시보드에서 안내문으로 처리).
 */
function repo_llm_summary(): ?array
{
    static $cache = null;
    if ($cache !== null) {
        return $cache === false ? null : $cache;
    }
    if (!repo_llm_available()) {
        $cache = false;
        return null;
    }
    $today = date('Y-m-d');
    $row = db_row(
        "SELECT COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN final_action = 'block' THEN 1 ELSE 0 END),0) AS blocks,
                COALESCE(SUM(CASE WHEN decision = 'error' THEN 1 ELSE 0 END),0) AS errors,
                COALESCE(SUM(CASE WHEN from_cache = 1 THEN 1 ELSE 0 END),0) AS cache_hits,
                COALESCE(SUM(input_tokens),0) AS input_tokens,
                COALESCE(SUM(output_tokens),0) AS output_tokens
           FROM llm_decision_log
          WHERE created_at >= ? AND created_at <= ?",
        [$today . ' 00:00:00', $today . ' 23:59:59']
    ) ?? [];
    $sum = [
        'date' => $today,
        'calls' => (int)($row['calls'] ?? 0),
        'blocks' => (int)($row['blocks'] ?? 0),
        'errors' => (int)($row['errors'] ?? 0),
        'cache_hits' => (int)($row['cache_hits'] ?? 0),
        'input_tokens' => (int)($row['input_tokens'] ?? 0),
        'output_tokens' => (int)($row['output_tokens'] ?? 0),
        'total' => (int)db_val('SELECT COUNT(*) FROM llm_decision_log', [], 0),
    ];
    $cache = $sum;
    return $sum;
}

/* ------------------------------------------------------------- 시스템 화면 */

function repo_server_status(): array
{
    return db_all(
        "SELECT component, status, message, updated_at,
                TIMESTAMPDIFF(SECOND, updated_at, NOW()) AS age_sec
           FROM server_status
          ORDER BY FIELD(component,'server','db','kiwoom_rest','kiwoom_ws','market'), component"
    );
}

/** 하트비트 지연을 반영한 실효 상태. */
function repo_effective_status(array $row): array
{
    $age = $row['age_sec'] === null ? null : (int)$row['age_sec'];
    $status = (string)$row['status'];
    $note = '';
    if ($age === null) {
        $status = 'unknown';
    } elseif ($age > APP_HEARTBEAT_ERROR_SEC) {
        $status = 'error';
        $note = '하트비트 ' . ago($row['updated_at']) . ' (지연)';
    } elseif ($age > APP_HEARTBEAT_WARN_SEC) {
        if ($status === 'ok') {
            $status = 'warn';
        }
        $note = '하트비트 지연 ' . $age . '초';
    }
    return ['status' => $status, 'note' => $note, 'age' => $age];
}

function repo_system_settings(): array
{
    return db_all('SELECT setting_key, value, description, updated_by, updated_at FROM system_setting ORDER BY setting_key');
}

function repo_setting(string $key, string $default = ''): string
{
    $v = db_val('SELECT value FROM system_setting WHERE setting_key = ? LIMIT 1', [$key], null);
    return $v === null ? $default : (string)$v;
}

function repo_events(?string $from, ?string $to, string $level, string $category, string $q, int $page): array
{
    [$w, $p] = repo_filter('created_at', $from, $to, $q, ['message']);
    $params = $p;
    if (in_array($level, ['DEBUG', 'INFO', 'WARN', 'ERROR'], true)) {
        $w .= ' AND level = ?';
        $params[] = $level;
    }
    if ($category !== '') {
        $w .= ' AND category = ?';
        $params[] = $category;
    }
    $base = ' FROM event_log WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT *' . $base . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_recent_events(int $limit = 10): array
{
    return db_all('SELECT id, level, category, message, created_at FROM event_log ORDER BY id DESC LIMIT ?', [$limit]);
}

function repo_event_categories(): array
{
    return array_column(
        db_all('SELECT DISTINCT category FROM event_log ORDER BY category LIMIT 50'),
        'category'
    );
}

function repo_algo_codes(): array
{
    return array_column(db_all('SELECT code FROM algorithm ORDER BY sort_order, code'), 'code');
}

function repo_users(): array
{
    return db_all(
        'SELECT id, username, display_name, role, is_active, failed_count, locked_until, last_login_at, created_at
           FROM app_user ORDER BY id ASC'
    );
}

function repo_login_log(int $limit = 50): array
{
    return db_all(
        'SELECT id, username, success, ip_addr, created_at FROM app_login_log ORDER BY id DESC LIMIT ?',
        [$limit]
    );
}

function repo_my_login_log(string $username, int $limit = 20): array
{
    return db_all(
        'SELECT id, success, ip_addr, created_at FROM app_login_log WHERE username = ? ORDER BY id DESC LIMIT ?',
        [$username, $limit]
    );
}

function repo_user(int $id): ?array
{
    return db_row(
        'SELECT id, username, display_name, role, is_active, last_login_at, created_at FROM app_user WHERE id = ?',
        [$id]
    );
}

/* ------------------------------------------------------------- 대시보드 */

function repo_dashboard(?int $accountId): array
{
    $balance = $accountId !== null ? repo_latest_balance($accountId) : null;
    $totals = $accountId !== null ? repo_holding_totals($accountId) : repo_holding_totals_empty();
    $today = date('Y-m-d');
    $todayPl = $accountId !== null
        ? (float)db_val('SELECT COALESCE(SUM(pl_amt),0) FROM daily_trade_summary WHERE account_id = ? AND base_dt = ?',
            [$accountId, $today], 0)
        : 0.0;
    $todayOrders = $accountId !== null
        ? (int)db_val('SELECT COUNT(*) FROM orders WHERE account_id = ? AND created_at >= ?', [$accountId, $today . ' 00:00:00'], 0)
        : 0;
    $todaySignals = (int)db_val('SELECT COUNT(*) FROM signal_log WHERE created_at >= ?', [$today . ' 00:00:00'], 0);
    return [
        'balance' => $balance,
        'holding_totals' => $totals,
        'today_pl' => $todayPl,
        'today_orders' => $todayOrders,
        'today_signals' => $todaySignals,
        'llm' => repo_llm_summary(),
    ];
}
