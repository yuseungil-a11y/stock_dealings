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

function repo_algorithm_by_id(int $id): ?array
{
    return db_row('SELECT * FROM algorithm WHERE id = ? LIMIT 1', [$id]);
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

/* ================================================================================
 * 관리자 전용 쓰기: 알고리즘 사용여부 · 우선순위 · 파라미터 (2026-09-23 도입)
 *
 * 이 블록만이 이 웹에서 algorithm_selection / algorithm_param_value / algorithm_param_history
 * 에 쓴다. algorithm / algorithm_param_def(정의 자체)는 절대 건드리지 않는다 — stock_web DB
 * 계정에도 그 두 표의 쓰기 권한을 의도적으로 주지 않았다.
 * ================================================================================ */

/** 알고리즘 목록(사용여부 · 우선순위 포함) — repo_algorithms() 와 동일 조회를 관리 화면 이름으로 노출. */
function repo_algorithms_with_selection(): array
{
    return repo_algorithms();
}

/** 알고리즘 파라미터 정의+현재값 — repo_params() 와 동일 조회를 관리 화면 이름으로 노출. */
function repo_algorithm_params(int $algoId): array
{
    return repo_params($algoId);
}

/** 파라미터 값 검증 실패(server/stock_svr/algo/params.py 의 validate() 와 같은 규칙을 PHP로 재현). */
class ParamValidationError extends RuntimeException
{
}

/** 'v1:라벨1,v2:라벨2' 또는 'v1,v2' 형식의 enum_options 파싱. @return array<int,array{0:string,1:string}> */
function param_enum_choices(string $raw): array
{
    $raw = trim($raw);
    if ($raw === '') {
        return [];
    }
    $out = [];
    foreach (explode(',', $raw) as $chunk) {
        $chunk = trim($chunk);
        if ($chunk === '') {
            continue;
        }
        if (str_contains($chunk, ':')) {
            [$v, $lab] = explode(':', $chunk, 2);
            $out[] = [trim($v), trim($lab)];
        } else {
            $out[] = [$chunk, $chunk];
        }
    }
    return $out;
}

/** min_value/max_value 범위 검사(둘 다 선택적, 숫자가 아니면 무시). */
function param_range_check(array $def, float $val, string $label): void
{
    $mn = $def['min_value'] ?? null;
    $mx = $def['max_value'] ?? null;
    if ($mn !== null && $mn !== '' && is_numeric((string)$mn) && $val < (float)$mn) {
        throw new ParamValidationError($label . ': 최소 ' . $mn . ' 이상이어야 합니다.');
    }
    if ($mx !== null && $mx !== '' && is_numeric((string)$mx) && $val > (float)$mx) {
        throw new ParamValidationError($label . ': 최대 ' . $mx . ' 이하여야 합니다.');
    }
}

/** 소수 저장용 문자열 표기(끝의 0·소수점 정리). */
function param_fmt_decimal(float $val): string
{
    $s = rtrim(rtrim(sprintf('%.10f', $val), '0'), '.');
    if ($s === '' || $s === '-' || $s === '-0') {
        return '0';
    }
    return $s;
}

/**
 * value_type 기준 파라미터 값 검증(server/stock_svr/algo/params.py::validate() 와 동일한 규칙).
 * 성공하면 저장용 정규화 문자열을 돌려주고, 실패하면 ParamValidationError 를 던진다.
 */
function param_validate(array $def, mixed $raw): string
{
    $label = (string)($def['label'] ?? $def['param_key'] ?? '?');
    $vtype = strtolower((string)($def['value_type'] ?? 'string'));
    $s = trim((string)($raw ?? ''));

    if ($vtype === 'bool') {
        $low = strtolower($s);
        if (in_array($low, ['1', 'true', 't', 'y', 'yes', 'on'], true)) {
            return '1';
        }
        if (in_array($low, ['0', 'false', 'f', 'n', 'no', 'off'], true)) {
            return '0';
        }
        throw new ParamValidationError($label . ': 참/거짓 값이어야 합니다.');
    }

    if ($vtype === 'enum') {
        $choices = array_column(param_enum_choices((string)($def['enum_options'] ?? '')), 0);
        if ($choices !== [] && !in_array($s, $choices, true)) {
            throw new ParamValidationError($label . ': 허용된 값이 아닙니다 (' . implode(', ', $choices) . ')');
        }
        return $s;
    }

    if ($vtype === 'time') {
        if ($s === '') {
            return ''; // 빈 값 허용(예: 청산 시각 미사용)
        }
        if (!preg_match('/^([01]\d|2[0-3]):([0-5]\d)$/', $s)) {
            throw new ParamValidationError($label . ': HH:MM 형식이어야 합니다.');
        }
        return $s;
    }

    if ($vtype === 'int') {
        if ($s === '' || !is_numeric($s)) {
            throw new ParamValidationError($label . ': 정수를 입력하세요.');
        }
        $f = (float)$s;
        if (!is_finite($f)) {
            throw new ParamValidationError($label . ': 유한한 숫자를 입력하세요.');
        }
        $val = (int)$f;
        param_range_check($def, (float)$val, $label);
        return (string)$val;
    }

    if ($vtype === 'decimal') {
        if ($s === '' || !is_numeric($s)) {
            throw new ParamValidationError($label . ': 숫자를 입력하세요.');
        }
        $f = (float)$s;
        if (!is_finite($f)) {
            throw new ParamValidationError($label . ': 유한한 숫자를 입력하세요.');
        }
        param_range_check($def, $f, $label);
        return param_fmt_decimal($f);
    }

    // string
    if (mb_strlen($s, 'UTF-8') > 100) {
        throw new ParamValidationError($label . ': 100자 이내로 입력하세요.');
    }
    return $s;
}

/**
 * 알고리즘 사용여부 · 우선순위 저장(관리자 전용).
 * is_locked=1(risk_guard) 인 행은 폼에서 온 enabled 값을 완전히 무시하고 항상 1로 강제한다 —
 * 클라이언트가 disabled 속성을 우회해 unchecked 로 POST 해도 서버에서 다시 켠다.
 * @param array<int,array{id:int,enabled:bool,priority:int}> $rows
 * @return array{ok:bool, changed:int}
 */
function repo_update_algorithm_selection(array $rows, string $by): array
{
    $byId = [];
    foreach (repo_algorithms() as $a) {
        $byId[(int)$a['id']] = $a; // 정본(is_locked 포함)은 항상 DB에서 다시 읽는다 — 폼 값을 신뢰하지 않음
    }
    $by = mb_substr($by, 0, 50, 'UTF-8');
    $changed = 0;
    $ok = true;
    foreach ($rows as $r) {
        $id = (int)($r['id'] ?? 0);
        if ($id <= 0 || !isset($byId[$id])) {
            continue; // 실제 존재하는 algorithm.id 가 아니면 무시
        }
        $isLocked = (int)$byId[$id]['is_locked'] === 1;
        $enabled = $isLocked ? 1 : (!empty($r['enabled']) ? 1 : 0);
        $priority = max(1, min(999, (int)($r['priority'] ?? $byId[$id]['priority'])));
        try {
            // stock_web 계정은 algorithm_selection 에 UPDATE 권한만 가진다(설계상 INSERT 없음) —
            // 시드 데이터가 모든 algorithm 에 대해 이 표에 행을 이미 갖고 있어야 한다.
            $n = db_exec(
                'UPDATE algorithm_selection SET is_enabled = ?, priority = ?, updated_by = ?, updated_at = NOW()
                  WHERE algorithm_id = ?',
                [$enabled, $priority, $by, $id]
            );
            if ($n < 1) {
                @error_log('[stock-web] algo_selection_no_row :: algorithm_id=' . $id);
                $ok = false;
                continue;
            }
            $changed++;
        } catch (Throwable $e) {
            @error_log('[stock-web] algo_selection_save_failed :: ' . $e->getMessage());
            $ok = false;
        }
    }
    return ['ok' => $ok, 'changed' => $changed];
}

/**
 * 파라미터 값 저장(관리자 전용). 하나라도 검증에 실패하면 아무것도 저장하지 않는다(all-or-nothing).
 * 실제로 값이 바뀐 필드만 algorithm_param_history 에 남긴다(old_value/new_value/changed_by).
 * 파라미터 간 교차 검증(예: 단기<장기)은 서버(Python registry.build())가 담당하므로 여기서는
 * value_type·min/max·enum 만 검사한다.
 * @param array<string,mixed> $values param_key => 폼에서 온 원값
 * @return array{ok:bool, errors:array<int,string>, changed:int}
 */
function repo_update_algorithm_params(int $algoId, array $values, string $by): array
{
    $defs = repo_params($algoId);
    if ($defs === []) {
        return ['ok' => false, 'errors' => ['이 알고리즘에는 정의된 파라미터가 없습니다.'], 'changed' => 0];
    }
    $by = mb_substr($by, 0, 50, 'UTF-8');

    $errors = [];
    $normalized = [];
    foreach ($defs as $d) {
        $key = (string)$d['param_key'];
        if (!array_key_exists($key, $values)) {
            continue; // 폼에 없는 값은 건드리지 않음
        }
        try {
            $new = param_validate($d, $values[$key]);
        } catch (ParamValidationError $e) {
            $errors[] = $e->getMessage();
            continue;
        }
        $old = (string)($d['current_value'] ?? $d['default_value']);
        $normalized[$key] = ['old' => $old, 'new' => $new];
    }
    if ($errors !== []) {
        return ['ok' => false, 'errors' => $errors, 'changed' => 0];
    }

    $changed = 0;
    $writeErrors = [];
    $pdo = db();
    $pdo->beginTransaction();
    try {
        foreach ($normalized as $key => $nv) {
            if ($nv['old'] === $nv['new']) {
                continue; // 실제로 바뀌지 않음 — 이력도 남기지 않는다
            }
            // stock_web 계정은 algorithm_param_value 에 UPDATE 권한만 가진다(설계상 INSERT 없음) —
            // 행이 없으면(시드 누락) 저장할 수 없으므로 실패로 취급하고 전체를 롤백한다.
            $n = db_exec(
                'UPDATE algorithm_param_value SET value = ?, updated_by = ?, updated_at = NOW()
                  WHERE algorithm_id = ? AND param_key = ?',
                [$nv['new'], $by, $algoId, $key]
            );
            if ($n < 1) {
                $writeErrors[] = $key . ': 저장할 값 행을 찾을 수 없습니다(관리자에게 문의).';
                continue;
            }
            db_exec(
                'INSERT INTO algorithm_param_history (algorithm_id, param_key, old_value, new_value, changed_by, changed_at)
                 VALUES (?,?,?,?,?,NOW())',
                [$algoId, $key, $nv['old'], $nv['new'], $by]
            );
            $changed++;
        }
        if ($writeErrors !== []) {
            $pdo->rollBack();
            return ['ok' => false, 'errors' => $writeErrors, 'changed' => 0];
        }
        $pdo->commit();
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        @error_log('[stock-web] algo_params_save_failed :: ' . $e->getMessage());
        return ['ok' => false, 'errors' => ['저장 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.'], 'changed' => 0];
    }
    return ['ok' => true, 'errors' => [], 'changed' => $changed];
}

/* ------------------------------------------------------- 자동거래 시작/중지 명령 큐 */

/** 웹이 넣을 수 있는 명령 화이트리스트. */
const AUTO_TRADING_COMMANDS = ['start', 'stop'];

/**
 * 자동거래 시작/중지 "명령"만 기록한다(관리자 전용). 이 웹은 auto_trading_command 에
 * INSERT 권한만 가지며, 실제 시작/중지 처리는 서버쪽 폴링 에이전트가 수행한다.
 */
function repo_insert_auto_trading_command(string $command, string $by): bool
{
    if (!in_array($command, AUTO_TRADING_COMMANDS, true)) {
        return false;
    }
    try {
        db_exec(
            "INSERT INTO auto_trading_command (command, requested_by, status, requested_at)
             VALUES (?, ?, 'pending', NOW())",
            [$command, mb_substr($by, 0, 50, 'UTF-8')]
        );
        return true;
    } catch (Throwable $e) {
        @error_log('[stock-web] auto_trading_command_insert_failed :: ' . $e->getMessage());
        return false;
    }
}

/** 최근 명령 이력(최신순) — 서버쪽 에이전트가 표를 아직 만들지 않았으면 조용히 빈 배열. */
function repo_recent_auto_trading_commands(int $limit = 20): array
{
    if (!repo_can_read('auto_trading_command')) {
        return [];
    }
    return db_all(
        'SELECT id, command, requested_by, status, result_message, requested_at, claimed_at, handled_at
           FROM auto_trading_command ORDER BY id DESC LIMIT ?',
        [max(1, min($limit, 200))]
    );
}

/**
 * 자동거래 현재 상태 + 게이트 정보(읽기만, system_setting 을 쓰지는 않는다).
 * @return array{status:?array, trading_mode:string, order_enabled:bool, real_trading_confirm:bool}
 */
function repo_auto_trading_status(): array
{
    $status = db_row(
        "SELECT component, status, message, updated_at, TIMESTAMPDIFF(SECOND, updated_at, NOW()) AS age_sec
           FROM server_status WHERE component = 'auto_trading' LIMIT 1"
    );
    return [
        'status' => $status,
        'trading_mode' => repo_setting('trading_mode', 'real'),
        'order_enabled' => repo_setting('order_enabled', '0') === '1',
        'real_trading_confirm' => repo_setting('real_trading_confirm', '0') === '1',
    ];
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

/* ------------------------------------------- 전략: 산업 트렌드 스캔 (읽기 전용) */

/** 필터 화이트리스트 (값은 이 목록을 통과한 것만 바인딩된다). */
const TREND_REGIONS = ['domestic', 'global'];
const TREND_MATCHES = ['kiwoom_theme_member', 'name_matched', 'unmatched'];
const TREND_SIGNAL_FILTERS = ['with', 'without'];
/** 실행 · 시도의 구분(예약 실행 / 웹에서 요청한 수동 재조사). */
const TREND_TRIGGERS = ['scheduled', 'manual'];
/** 스캔은 하루 1회이므로 이력 목록은 작은 페이지로 나눈다. */
const TREND_RUN_PAGE_SIZE = 20;
/** 시도 이력(실패 포함)도 같은 크기로 나눈다. */
const TREND_ATTEMPT_PAGE_SIZE = 20;
/** 같은 브라우저 세션에서 재조사 요청을 다시 받기까지의 최소 간격(초). */
const TREND_REQUEST_COOLDOWN_SEC = 120;
/** 이 시간(분) 안에 수동 시도 기록이 있으면 아직 처리 중으로 보고 버튼을 잠근다. */
const TREND_MANUAL_RECENT_MIN = 3;

function trend_region_options(): array
{
    return ['' => '전체', 'domestic' => '국내', 'global' => '해외'];
}

function trend_trigger_options(): array
{
    return ['' => '전체', 'scheduled' => '예약', 'manual' => '수동'];
}

function trend_match_options(): array
{
    return ['' => '전체', 'kiwoom_theme_member' => '키움 테마 구성종목',
        'name_matched' => '종목명 일치', 'unmatched' => '미매칭'];
}

function trend_signal_options(): array
{
    return ['' => '전체', 'with' => '신호 있음', 'without' => '신호 없음'];
}

/** 트렌드 스캔 표를 읽을 수 있는지(표가 없거나 권한이 없으면 화면을 안내문으로 대체). */
function repo_trend_available(): bool
{
    return repo_can_read('trend_scan_run') && repo_can_read('trend_scan_candidate');
}

/** 모든 시도(성공·실패 포함) 이력 표를 읽을 수 있는지. */
function repo_trend_attempt_available(): bool
{
    return repo_can_read('trend_scan_attempt');
}

/**
 * 후보(trend_scan_candidate, 별칭 c) 필터 조건.
 * 식별자·연산자는 코드에 고정되어 있고 값은 화이트리스트를 통과한 뒤 바인딩만 된다.
 * @return array{0:string,1:array}
 */
function repo_trend_cand_cond(string $region, string $match, string $signal): array
{
    $w = '';
    $p = [];
    if (in_array($region, TREND_REGIONS, true)) {
        $w .= ' AND c.region = ?';
        $p[] = $region;
    }
    if (in_array($match, TREND_MATCHES, true)) {
        $w .= ' AND c.match_status = ?';
        $p[] = $match;
    }
    $w .= match ($signal) {
        'with' => ' AND c.signal_id IS NOT NULL',
        'without' => ' AND c.signal_id IS NULL',
        default => '',
    };
    return [$w, $p];
}

/**
 * 스캔 실행 이력(최신순). 후보 조건이 있으면 그 조건에 맞는 후보를 가진 실행만 남긴다.
 * 구분(trigger)도 화이트리스트를 통과한 값만 바인딩된다.
 */
function repo_trend_runs(?string $from, ?string $to, string $region, string $match, string $signal,
    string $trigger, int $page): array
{
    if (!repo_trend_available()) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => TREND_RUN_PAGE_SIZE];
    }
    $w = '';
    $p = [];
    if ($from !== null) {
        $w .= ' AND r.scan_date >= ?';
        $p[] = $from;
    }
    if ($to !== null) {
        $w .= ' AND r.scan_date <= ?';
        $p[] = $to;
    }
    if (in_array($trigger, TREND_TRIGGERS, true)) {
        $w .= ' AND r.trigger_type = ?';
        $p[] = $trigger;
    }
    [$cw, $cp] = repo_trend_cand_cond($region, $match, $signal);
    if ($cw !== '') {
        $w .= ' AND EXISTS (SELECT 1 FROM trend_scan_candidate c WHERE c.run_id = r.id' . $cw . ')';
        $p = array_merge($p, $cp);
    }
    $base = ' FROM trend_scan_run r WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT r.id, r.scan_date, r.trigger_type, r.requested_by, r.started_at, r.finished_at, r.status,'
        . ' r.region_scope, r.model,'
        . ' r.domestic_theme_summary, r.research_summary, r.candidate_count, r.signal_count,'
        . ' r.web_search_count, r.input_tokens, r.output_tokens, r.latency_ms, r.error_msg' . $base
        . ' ORDER BY r.scan_date DESC, r.id DESC',
        'SELECT COUNT(*)' . $base,
        $p,
        $page,
        TREND_RUN_PAGE_SIZE
    );
}

/**
 * 모든 시도 이력(최신순, 성공·부분성공·실패 모두). trend_scan_run 은 하루 1건만 남지만
 * 이 표는 실패한 시도까지 영구 보존하므로 "왜 결과가 비었는지"를 화면에서 그대로 보여준다.
 */
function repo_trend_attempts(?string $from, ?string $to, string $trigger, int $page): array
{
    if (!repo_trend_attempt_available()) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => TREND_ATTEMPT_PAGE_SIZE];
    }
    $w = '';
    $p = [];
    if ($from !== null) {
        $w .= ' AND scan_date >= ?';
        $p[] = $from;
    }
    if ($to !== null) {
        $w .= ' AND scan_date <= ?';
        $p[] = $to;
    }
    if (in_array($trigger, TREND_TRIGGERS, true)) {
        $w .= ' AND trigger_type = ?';
        $p[] = $trigger;
    }
    $base = ' FROM trend_scan_attempt WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT id, scan_date, trigger_type, requested_by, status, region_scope, model, candidate_count,'
        . ' web_search_count, input_tokens, output_tokens, latency_ms, error_msg, research_summary,'
        . ' started_at, finished_at, created_at' . $base
        . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $p,
        $page,
        TREND_ATTEMPT_PAGE_SIZE
    );
}

/**
 * 최근 N분 안의 수동 재조사 시도(오늘 날짜) 1건. 있으면 아직 처리 중으로 보고 중복 요청을 막는다.
 * (웹 계정은 trend_scan_request 를 조회할 수 없으므로 시도 이력으로 간접 판단한다.)
 */
function repo_trend_manual_recent(int $minutes): ?array
{
    if (!repo_trend_attempt_available()) {
        return null;
    }
    try {
        return db_row(
            'SELECT id, scan_date, trigger_type, requested_by, status, started_at, finished_at, created_at
               FROM trend_scan_attempt
              WHERE trigger_type = ? AND scan_date = ? AND created_at > (NOW() - INTERVAL ? MINUTE)
              ORDER BY created_at DESC, id DESC LIMIT 1',
            ['manual', date('Y-m-d'), $minutes]
        );
    } catch (Throwable $e) {
        return null;
    }
}

/**
 * 수동 재조사 "요청"만 기록한다. 웹이 하는 유일한 업무성 쓰기이며
 * 주문 · 설정 · 알고리즘 파라미터 등 다른 어떤 것도 바꾸지 않는다(실제 조사는 서버 모듈이 수행).
 * stock_web 계정은 이 표에 INSERT 권한만 있으므로 넣은 행을 다시 조회하지 않는다.
 */
function repo_trend_request_insert(string $username): bool
{
    try {
        db_exec('INSERT INTO trend_scan_request (requested_by) VALUES (?)', [mb_substr($username, 0, 50)]);
        return true;
    } catch (Throwable $e) {
        @error_log('[stock-web] trend_request_insert_failed :: ' . $e->getMessage());
        return false;
    }
}

/**
 * 화면에 보이는 실행들의 후보를 한 번에 조회한다(N+1 금지).
 * 신호로 이어진 후보는 signal_log · orders 를 좌측 조인해 신호/주문 화면으로 연결할 값을 함께 가져온다.
 * @return array<int,array> run_id => 후보 목록
 */
function repo_trend_candidates(array $runIds, string $region, string $match, string $signal): array
{
    if (!repo_trend_available()) {
        return [];
    }
    $ids = [];
    foreach ($runIds as $id) {
        if ($id === null || $id === '' || !is_numeric($id)) {
            continue;
        }
        $ids[(int)$id] = (int)$id;
    }
    if ($ids === []) {
        return [];
    }
    [$cw, $cp] = repo_trend_cand_cond($region, $match, $signal);
    $out = [];
    // 플레이스홀더 개수는 코드가 만들고 값은 모두 바인딩한다.
    foreach (array_chunk(array_values($ids), 200) as $chunk) {
        $ph = implode(',', array_fill(0, count($chunk), '?'));
        $rows = db_all(
            'SELECT c.id, c.run_id, c.region, c.theme, c.rationale, c.confidence, c.kiwoom_theme_cd,
                    c.kiwoom_theme_nm, c.stk_cd, c.stk_nm, c.match_status, c.signal_id, c.created_at,
                    s.signal_type, s.created_at AS signal_time, s.algo_code, s.stk_cd AS signal_stk_cd,
                    s.order_id, o.ord_no, o.status AS order_status, o.is_dry_run, o.created_at AS order_time
               FROM trend_scan_candidate c
               LEFT JOIN signal_log s ON s.id = c.signal_id
               LEFT JOIN orders o ON o.id = s.order_id
              WHERE c.run_id IN (' . $ph . ')' . $cw
            . ' ORDER BY c.run_id ASC, c.region ASC, c.confidence DESC, c.id ASC',
            array_merge($chunk, $cp)
        );
        foreach ($rows as $r) {
            $out[(int)$r['run_id']][] = $r;
        }
    }
    return $out;
}

/** 오늘(또는 지정일) 실행 1건 + 실제 후보/신호 건수. 없으면 null. */
function repo_trend_run_by_date(?string $date = null): ?array
{
    if (!repo_trend_available()) {
        return null;
    }
    $date = $date ?? date('Y-m-d');
    $row = db_row(
        'SELECT id, scan_date, trigger_type, requested_by, status, region_scope, model, candidate_count,
                signal_count, started_at, finished_at, error_msg
           FROM trend_scan_run WHERE scan_date = ? LIMIT 1',
        [$date]
    );
    if ($row === null) {
        return null;
    }
    // 표시 건수는 실제 후보 행을 기준으로 한다(실행 행의 집계값과 어긋나도 화면은 사실을 보여준다).
    $c = db_row(
        'SELECT COUNT(*) AS cnt, COALESCE(SUM(CASE WHEN signal_id IS NOT NULL THEN 1 ELSE 0 END),0) AS sig
           FROM trend_scan_candidate WHERE run_id = ?',
        [(int)$row['id']]
    ) ?? ['cnt' => 0, 'sig' => 0];
    $row['candidates'] = (int)$c['cnt'];
    $row['signals'] = (int)$c['sig'];
    return $row;
}

/**
 * 요약 카드 값 (최근 스캔 · 이번달 후보/매칭률/신호 · 이번달 웹검색·토큰).
 * 표가 없으면 available=false 로만 돌려준다.
 */
function repo_trend_summary(): array
{
    $empty = ['available' => false, 'last' => null, 'today' => null, 'month_from' => date('Y-m-01'),
        'candidates' => 0, 'matched' => 0, 'match_rate' => null, 'signals' => 0,
        'runs' => 0, 'web_search' => 0, 'input_tokens' => 0, 'output_tokens' => 0];
    if (!repo_trend_available()) {
        return $empty;
    }
    $from = date('Y-m-01');
    $to = date('Y-m-t');
    $sum = $empty;
    $sum['available'] = true;
    $sum['last'] = db_row(
        'SELECT id, scan_date, trigger_type, requested_by, status, model, region_scope, candidate_count,
                signal_count, finished_at, error_msg
           FROM trend_scan_run ORDER BY scan_date DESC, id DESC LIMIT 1'
    );
    $sum['today'] = repo_trend_run_by_date();
    $cand = db_row(
        "SELECT COUNT(*) AS cnt,
                COALESCE(SUM(CASE WHEN c.match_status <> 'unmatched' THEN 1 ELSE 0 END),0) AS matched,
                COALESCE(SUM(CASE WHEN c.signal_id IS NOT NULL THEN 1 ELSE 0 END),0) AS signals
           FROM trend_scan_candidate c
           JOIN trend_scan_run r ON r.id = c.run_id
          WHERE r.scan_date >= ? AND r.scan_date <= ?",
        [$from, $to]
    ) ?? [];
    $sum['candidates'] = (int)($cand['cnt'] ?? 0);
    $sum['matched'] = (int)($cand['matched'] ?? 0);
    $sum['signals'] = (int)($cand['signals'] ?? 0);
    $sum['match_rate'] = $sum['candidates'] > 0 ? $sum['matched'] / $sum['candidates'] * 100 : null;
    $runs = db_row(
        'SELECT COUNT(*) AS runs, COALESCE(SUM(web_search_count),0) AS web_search,
                COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens
           FROM trend_scan_run WHERE scan_date >= ? AND scan_date <= ?',
        [$from, $to]
    ) ?? [];
    $sum['runs'] = (int)($runs['runs'] ?? 0);
    $sum['web_search'] = (int)($runs['web_search'] ?? 0);
    $sum['input_tokens'] = (int)($runs['input_tokens'] ?? 0);
    $sum['output_tokens'] = (int)($runs['output_tokens'] ?? 0);
    return $sum;
}

/** 대시보드 한 줄 요약용 — 오늘 실행이 없으면 null. */
function repo_trend_today(): ?array
{
    $row = repo_trend_run_by_date();
    if ($row === null) {
        return null;
    }
    return [
        'scan_date' => (string)$row['scan_date'],
        'status' => (string)$row['status'],
        'candidates' => (int)$row['candidates'],
        'signals' => (int)$row['signals'],
    ];
}

/* --------------------------------------------------------- 거래 분석 (읽기 전용) */

/**
 * 조회 대상 표/뷰를 읽을 수 있는지 확인한다.
 * 표 이름은 코드에 고정된 화이트리스트에서만 오며 사용자 입력이 섞이지 않는다.
 */
function repo_can_read(string $object): bool
{
    static $cache = [];
    if (!in_array($object, ['v_trade_analysis', 'order_event', 'event_archive', 'api_error_log',
        'trend_scan_run', 'trend_scan_candidate', 'trend_scan_attempt',
        'company_corp_code', 'company_financial', 'company_valuation_daily', 'company_analysis_report',
        'auto_trading_command'], true)) {
        return false;
    }
    if (isset($cache[$object])) {
        return $cache[$object];
    }
    try {
        db_val('SELECT 1 FROM ' . $object . ' LIMIT 1', [], null);
        $cache[$object] = true;
    } catch (Throwable $e) {
        $cache[$object] = false;
    }
    return $cache[$object];
}

/** 거래 분석 결과 유형 필터 화이트리스트. */
const ANALYSIS_RESULTS = ['filled', 'partial', 'failed', 'blocked', 'dryrun'];

/** 내보내기 최대 행 수 (초과분은 잘라내고 화면/메타로 안내). */
const ANALYSIS_EXPORT_MAX = 5000;

function analysis_result_options(): array
{
    return [
        '' => '전체',
        'filled' => '체결완료',
        'partial' => '부분체결',
        'failed' => '실패 · 거부',
        'blocked' => '차단(BLOCK)',
        'dryrun' => '관찰만(기록)',
    ];
}

/**
 * 거래 분석 WHERE 조각.
 * 결과 유형은 화이트리스트 → 고정 SQL 조각 매핑이며 값은 모두 바인딩된다.
 * @return array{0:string,1:array}
 */
function repo_analysis_where(?string $from, ?string $to, string $q, string $result): array
{
    [$w, $p] = repo_filter('signal_time', $from, $to, $q, ['stk_cd', 'stk_nm']);
    $w .= match ($result) {
        'filled' => " AND order_status = 'FILLED'",
        'partial' => " AND order_status = 'PARTIAL'",
        'failed' => " AND order_status IN ('FAILED','REJECTED')",
        'blocked' => " AND signal_type = 'BLOCK'",
        'dryrun' => ' AND is_dry_run = 1',
        default => '',
    };
    return [$w, $p];
}

/** 거래 분석 조회 대상 컬럼(화면·내보내기 공통). */
function repo_analysis_columns(): string
{
    return 'signal_id, signal_time, algo_code, signal_type, stk_cd, stk_nm, score, signal_detail,'
        . ' order_id, ord_no, side, order_kind, order_status, is_dry_run, trde_tp, ord_qty, ord_uv,'
        . ' signal_price, filled_qty, avg_fill_pric, slippage_pct, return_code, return_msg, reject_reason,'
        . ' order_reason, signal_context, params_snapshot, order_time, exec_cnt, exec_amount, exec_fee_tax,'
        . ' llm_model, llm_decision, llm_final, llm_confidence, llm_reasons';
}

function repo_analysis_empty(): array
{
    return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
}

function repo_trade_analysis(?string $from, ?string $to, string $q, string $result, int $page): array
{
    if (!repo_can_read('v_trade_analysis')) {
        return repo_analysis_empty();
    }
    [$w, $p] = repo_analysis_where($from, $to, $q, $result);
    $base = ' FROM v_trade_analysis WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT ' . repo_analysis_columns() . $base . ' ORDER BY signal_time DESC, signal_id DESC',
        'SELECT COUNT(*)' . $base,
        $p,
        $page
    );
}

/** 내보내기용 조회 (최신순, 상한 적용). */
function repo_trade_analysis_export(?string $from, ?string $to, string $q, string $result, int $limit): array
{
    if (!repo_can_read('v_trade_analysis')) {
        return [];
    }
    [$w, $p] = repo_analysis_where($from, $to, $q, $result);
    return db_all(
        'SELECT ' . repo_analysis_columns() . ' FROM v_trade_analysis WHERE 1=1' . $w
        . ' ORDER BY signal_time DESC, signal_id DESC LIMIT ?',
        array_merge($p, [$limit])
    );
}

function repo_trade_analysis_count(?string $from, ?string $to, string $q, string $result): int
{
    if (!repo_can_read('v_trade_analysis')) {
        return 0;
    }
    [$w, $p] = repo_analysis_where($from, $to, $q, $result);
    return (int)db_val('SELECT COUNT(*) FROM v_trade_analysis WHERE 1=1' . $w, $p, 0);
}

/** 거래 분석 요약 카드 값. */
function repo_trade_analysis_summary(?string $from, ?string $to, string $q, string $result): array
{
    $empty = ['signals' => 0, 'orders' => 0, 'filled' => 0, 'failed' => 0, 'blocked' => 0,
        'avg_slippage' => null, 'fee_tax' => 0];
    if (!repo_can_read('v_trade_analysis')) {
        return $empty;
    }
    [$w, $p] = repo_analysis_where($from, $to, $q, $result);
    $row = db_row(
        "SELECT COUNT(*) AS signals,
                COALESCE(SUM(CASE WHEN order_id IS NOT NULL AND COALESCE(is_dry_run,0) = 0 THEN 1 ELSE 0 END),0) AS orders,
                COALESCE(SUM(CASE WHEN order_status = 'FILLED' THEN 1 ELSE 0 END),0) AS filled,
                COALESCE(SUM(CASE WHEN order_status IN ('FAILED','REJECTED') THEN 1 ELSE 0 END),0) AS failed,
                COALESCE(SUM(CASE WHEN signal_type = 'BLOCK' THEN 1 ELSE 0 END),0) AS blocked,
                AVG(slippage_pct) AS avg_slippage,
                COALESCE(SUM(exec_fee_tax),0) AS fee_tax
           FROM v_trade_analysis WHERE 1=1" . $w,
        $p
    );
    if ($row === null) {
        return $empty;
    }
    return [
        'signals' => (int)$row['signals'],
        'orders' => (int)$row['orders'],
        'filled' => (int)$row['filled'],
        'failed' => (int)$row['failed'],
        'blocked' => (int)$row['blocked'],
        'avg_slippage' => $row['avg_slippage'] === null ? null : (float)$row['avg_slippage'],
        'fee_tax' => (float)$row['fee_tax'],
    ];
}

/**
 * 주문 상태 타임라인(order_event) 을 order_id 묶음으로 한 번에 조회한다(N+1 금지).
 * @param array $orderIds 페이지에 표시할 order_id 목록
 * @return array<int,array> order_id => 시간순 이벤트 목록
 */
function repo_order_events(array $orderIds): array
{
    if (!repo_can_read('order_event')) {
        return [];
    }
    $ids = [];
    foreach ($orderIds as $id) {
        if ($id === null || $id === '' || !is_numeric($id)) {
            continue;
        }
        $ids[(int)$id] = (int)$id;
    }
    if ($ids === []) {
        return [];
    }
    $out = [];
    // 플레이스홀더 개수는 코드가 만들고 값은 모두 바인딩한다.
    foreach (array_chunk(array_values($ids), 500) as $chunk) {
        $ph = implode(',', array_fill(0, count($chunk), '?'));
        $rows = db_all(
            'SELECT order_id, ord_no, event_time, event_type, status, filled_qty, remain_qty, price,
                    reject_reason, return_code, message, source
               FROM order_event
              WHERE order_id IN (' . $ph . ')
              ORDER BY order_id ASC, event_time ASC, id ASC',
            $chunk
        );
        foreach ($rows as $r) {
            $out[(int)$r['order_id']][] = $r;
        }
    }
    return $out;
}

/** 결과 목록에서 order_id 만 추출. */
function repo_collect_order_ids(array $rows, string $key = 'order_id'): array
{
    $ids = [];
    foreach ($rows as $r) {
        if (isset($r[$key]) && $r[$key] !== null && $r[$key] !== '') {
            $ids[] = $r[$key];
        }
    }
    return $ids;
}

/** 최근 N시간 주문 실패·거부 건수(대시보드 한 줄 요약). */
function repo_recent_order_failures(?int $accountId, int $hours = 24): int
{
    if ($accountId === null) {
        return 0;
    }
    return (int)db_val(
        "SELECT COUNT(*) FROM orders
          WHERE account_id = ? AND status IN ('FAILED','REJECTED')
            AND created_at >= (NOW() - INTERVAL ? HOUR)",
        [$accountId, $hours],
        0
    );
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

/* -------------------------------------------- 시스템: 이벤트 · API 오류 보관 */

/** event_archive (영구 보관본) 조회. */
function repo_event_archive(?string $from, ?string $to, string $level, string $category, string $q, int $page): array
{
    if (!repo_can_read('event_archive')) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
    }
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
    $base = ' FROM event_archive WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT id, created_at, level, category, message' . $base . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_event_archive_categories(): array
{
    if (!repo_can_read('event_archive')) {
        return [];
    }
    return array_column(
        db_all('SELECT DISTINCT category FROM event_archive ORDER BY category LIMIT 50'),
        'category'
    );
}

/** api_error_log (오류 응답 영구 보관) 조회. */
function repo_api_errors(?string $from, ?string $to, string $apiId, string $returnCode, int $page): array
{
    if (!repo_can_read('api_error_log')) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
    }
    [$w, $p] = repo_filter('created_at', $from, $to, '', []);
    $params = $p;
    if ($apiId !== '') {
        $w .= ' AND api_id = ?';
        $params[] = $apiId;
    }
    if ($returnCode !== '') {
        $w .= ' AND return_code = ?';
        $params[] = (int)$returnCode;
    }
    $base = ' FROM api_error_log WHERE 1=1' . $w;
    return repo_paginate(
        'SELECT id, created_at, api_id, http_status, return_code, return_msg, elapsed_ms' . $base
        . ' ORDER BY created_at DESC, id DESC',
        'SELECT COUNT(*)' . $base,
        $params,
        $page
    );
}

function repo_api_error_ids(): array
{
    if (!repo_can_read('api_error_log')) {
        return [];
    }
    return array_column(
        db_all('SELECT DISTINCT api_id FROM api_error_log ORDER BY api_id LIMIT 50'),
        'api_id'
    );
}

function repo_api_error_codes(): array
{
    if (!repo_can_read('api_error_log')) {
        return [];
    }
    return array_column(
        db_all('SELECT DISTINCT return_code FROM api_error_log WHERE return_code IS NOT NULL ORDER BY return_code LIMIT 50'),
        'return_code'
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

/* ============================================================================
 * 기업 재무분석 (리서치 — 읽기 전용, 매매와 완전히 무관)
 *   company_financial        DART 재무제표(분기/사업보고서)
 *   company_valuation_daily  PER/PBR/ROE/부채비율 일별 계산
 *   company_analysis_report  Claude 재무분석 리포트(참고용)
 *   company_corp_code        종목코드 ↔ DART corp_code / 회사명
 * 이 영역의 어떤 함수도 계좌 · 주문 · 신호 표를 읽거나 쓰지 않는다.
 * ========================================================================== */

/** DART 보고서 코드 → 한글 라벨. */
const FIN_REPRT_LABELS = [
    '11013' => '1분기',
    '11012' => '반기',
    '11014' => '3분기',
    '11011' => '사업보고서',
];

/** 재무제표 표에 보여줄 최근 사업연도 수. */
const FIN_YEARS = 5;

/** 밸류에이션 추이 기본/최대 조회 일수. */
const FIN_TREND_DAYS = 90;
const FIN_TREND_DAYS_MAX = 365;

/** 리포트 목록 · 종목 목록의 한 페이지 크기. */
const FIN_PAGE_SIZE = 30;

/** 종목 상세에 함께 보여줄 과거 리포트 수. */
const FIN_REPORT_HISTORY = 10;

/** 리포트 상태 필터 화이트리스트. */
const FIN_STATUSES = ['ok', 'error'];

/**
 * 리포트 목록 정렬 화이트리스트 (키 → 고정 ORDER BY 조각).
 * 사용자 입력은 이 배열의 키로만 들어오며 SQL 조각은 코드에 고정돼 있다.
 */
const FIN_SORTS = [
    'report_desc' => ['label' => '리포트 최신순', 'sql' => 'r.as_of_date DESC, r.stk_cd ASC'],
    'per_asc' => ['label' => 'PER 낮은순', 'sql' => '(v.per IS NULL OR v.per <= 0) ASC, v.per ASC, r.stk_cd ASC'],
    'per_desc' => ['label' => 'PER 높은순', 'sql' => '(v.per IS NULL) ASC, v.per DESC, r.stk_cd ASC'],
    'pbr_asc' => ['label' => 'PBR 낮은순', 'sql' => '(v.pbr IS NULL OR v.pbr <= 0) ASC, v.pbr ASC, r.stk_cd ASC'],
    'roe_desc' => ['label' => 'ROE 높은순', 'sql' => '(v.roe IS NULL) ASC, v.roe DESC, r.stk_cd ASC'],
    'debt_asc' => ['label' => '부채비율 낮은순', 'sql' => '(v.debt_ratio IS NULL) ASC, v.debt_ratio ASC, r.stk_cd ASC'],
    'code_asc' => ['label' => '종목코드순', 'sql' => 'r.stk_cd ASC'],
];

function fin_sort_options(): array
{
    $out = [];
    foreach (FIN_SORTS as $k => $v) {
        $out[$k] = $v['label'];
    }
    return $out;
}

function fin_status_options(): array
{
    return ['' => '전체', 'ok' => '정상', 'error' => '오류'];
}

function fin_reprt_label(?string $code): string
{
    $c = (string)$code;
    return FIN_REPRT_LABELS[$c] ?? ($c === '' ? '-' : $c);
}

/** 재무제표 표를 읽을 수 있는지. */
function repo_fin_available(): bool
{
    return repo_can_read('company_financial');
}

/** 밸류에이션 표를 읽을 수 있는지. */
function repo_fin_valuation_available(): bool
{
    return repo_can_read('company_valuation_daily');
}

/** 분석 리포트 표를 읽을 수 있는지. */
function repo_fin_report_available(): bool
{
    return repo_can_read('company_analysis_report');
}

/** 종목명 후보 3열(코드/회사명/리포트 종목명) 중 첫 번째 값. */
function fin_display_name(array $r): string
{
    foreach (['stk_nm', 'rpt_nm', 'corp_name'] as $k) {
        $v = (string)($r[$k] ?? '');
        if ($v !== '') {
            return $v;
        }
    }
    return (string)($r['stk_cd'] ?? '');
}

/**
 * 재무데이터 또는 분석 리포트가 있는 종목 목록(검색용).
 * 이름은 company_corp_code · company_analysis_report 에서 가져온다.
 */
function repo_fin_stocks(string $q, int $page): array
{
    $finOk = repo_fin_available();
    $rptOk = repo_fin_report_available();
    if (!$finOk && !$rptOk) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => FIN_PAGE_SIZE];
    }
    $ccOk = repo_can_read('company_corp_code');
    $valOk = repo_fin_valuation_available();

    $union = [];
    if ($finOk) {
        $union[] = 'SELECT stk_cd FROM company_financial GROUP BY stk_cd';
    }
    if ($rptOk) {
        $union[] = 'SELECT stk_cd FROM company_analysis_report GROUP BY stk_cd';
    }
    $inner = 'SELECT b.stk_cd AS stk_cd,'
        . ($ccOk ? ' (SELECT cc.corp_name FROM company_corp_code cc WHERE cc.stk_cd = b.stk_cd)' : ' NULL')
        . ' AS corp_name,'
        . ($rptOk
            ? ' (SELECT r.stk_nm FROM company_analysis_report r WHERE r.stk_cd = b.stk_cd'
                . " AND r.stk_nm IS NOT NULL AND r.stk_nm <> '' ORDER BY r.as_of_date DESC, r.id DESC LIMIT 1)"
            : ' NULL')
        . ' AS rpt_nm,'
        . ($finOk ? ' (SELECT COUNT(*) FROM company_financial f WHERE f.stk_cd = b.stk_cd)' : ' 0')
        . ' AS fin_cnt,'
        . ($finOk ? ' (SELECT MAX(f.bsns_year) FROM company_financial f WHERE f.stk_cd = b.stk_cd)' : ' NULL')
        . ' AS last_year,'
        . ($rptOk
            ? ' (SELECT MAX(r.as_of_date) FROM company_analysis_report r WHERE r.stk_cd = b.stk_cd)'
            : ' NULL')
        . ' AS last_report,'
        . ($valOk ? ' (SELECT MAX(v.dt) FROM company_valuation_daily v WHERE v.stk_cd = b.stk_cd)' : ' NULL')
        . ' AS last_val'
        . ' FROM (' . implode(' UNION ', $union) . ') b';

    $w = '';
    $p = [];
    if ($q !== '') {
        $w = ' WHERE (t.stk_cd LIKE ? OR t.corp_name LIKE ? OR t.rpt_nm LIKE ?)';
        $like = '%' . $q . '%';
        $p = [$like, $like, $like];
    }
    $base = ' FROM (' . $inner . ') t' . $w;
    return repo_paginate(
        'SELECT t.*' . $base . ' ORDER BY (t.last_report IS NULL) ASC, t.last_report DESC, t.stk_cd ASC',
        'SELECT COUNT(*)' . $base,
        $p,
        $page,
        FIN_PAGE_SIZE
    );
}

/** 종목 상세 화면의 머리글용 이름(리포트 → corp_code 순). */
function repo_fin_stock_name(string $stkCd): array
{
    $out = ['stk_cd' => $stkCd, 'stk_nm' => null, 'corp_name' => null, 'corp_code' => null];
    if (repo_fin_report_available()) {
        $out['stk_nm'] = db_val(
            'SELECT stk_nm FROM company_analysis_report WHERE stk_cd = ?'
            . " AND stk_nm IS NOT NULL AND stk_nm <> '' ORDER BY as_of_date DESC, id DESC LIMIT 1",
            [$stkCd]
        );
    }
    if (repo_can_read('company_corp_code')) {
        $row = db_row('SELECT corp_code, corp_name FROM company_corp_code WHERE stk_cd = ?', [$stkCd]);
        if ($row !== null) {
            $out['corp_code'] = $row['corp_code'];
            $out['corp_name'] = $row['corp_name'];
        }
    }
    return $out;
}

/** 최근 N개 사업연도의 재무제표(연도 내림차순 · 보고서는 사업보고서 → 3분기 → 반기 → 1분기). */
function repo_fin_statements(string $stkCd, int $years = FIN_YEARS): array
{
    if (!repo_fin_available()) {
        return [];
    }
    $maxYear = db_val('SELECT MAX(bsns_year) FROM company_financial WHERE stk_cd = ?', [$stkCd]);
    if ($maxYear === null) {
        return [];
    }
    $fromYear = (int)$maxYear - max(0, $years - 1);
    return db_all(
        'SELECT bsns_year, reprt_code, revenue, operating_profit, net_profit, total_assets,'
        . ' total_liabilities, total_equity, eps, operating_cash_flow, shares_outstanding, fetched_at'
        . ' FROM company_financial WHERE stk_cd = ? AND bsns_year >= ?'
        . " ORDER BY bsns_year DESC, FIELD(reprt_code,'11013','11012','11014','11011') DESC",
        [$stkCd, $fromYear]
    );
}

/** 최근 밸류에이션 1건. */
function repo_fin_valuation_latest(string $stkCd): ?array
{
    if (!repo_fin_valuation_available()) {
        return null;
    }
    return db_row(
        'SELECT stk_cd, dt, cur_prc, eps_ttm, bps, per, pbr, roe, debt_ratio, financial_asof'
        . ' FROM company_valuation_daily WHERE stk_cd = ? ORDER BY dt DESC LIMIT 1',
        [$stkCd]
    );
}

/** 밸류에이션 추이(최근 N일, 오래된 날짜 → 최신 순으로 되돌려준다). */
function repo_fin_valuation_series(string $stkCd, int $days = FIN_TREND_DAYS): array
{
    if (!repo_fin_valuation_available()) {
        return [];
    }
    $days = max(1, min($days, FIN_TREND_DAYS_MAX));
    $rows = db_all(
        'SELECT dt, cur_prc, eps_ttm, bps, per, pbr, roe, debt_ratio, financial_asof'
        . ' FROM company_valuation_daily WHERE stk_cd = ? ORDER BY dt DESC LIMIT ?',
        [$stkCd, $days]
    );
    return array_reverse($rows);
}

/** 종목의 최신 분석 리포트 1건(상태 무관). */
function repo_fin_report_latest(string $stkCd): ?array
{
    if (!repo_fin_report_available()) {
        return null;
    }
    return db_row(
        'SELECT id, stk_cd, stk_nm, as_of_date, model, summary, report_text, input_tokens, output_tokens,'
        . ' latency_ms, status, error_msg, created_at'
        . ' FROM company_analysis_report WHERE stk_cd = ? ORDER BY as_of_date DESC, id DESC LIMIT 1',
        [$stkCd]
    );
}

/** 특정 날짜의 분석 리포트(과거 리포트 열람). */
function repo_fin_report_by_date(string $stkCd, string $date): ?array
{
    if (!repo_fin_report_available()) {
        return null;
    }
    return db_row(
        'SELECT id, stk_cd, stk_nm, as_of_date, model, summary, report_text, input_tokens, output_tokens,'
        . ' latency_ms, status, error_msg, created_at'
        . ' FROM company_analysis_report WHERE stk_cd = ? AND as_of_date = ? LIMIT 1',
        [$stkCd, $date]
    );
}

/** 종목의 리포트 이력 목록(본문 제외). */
function repo_fin_report_history(string $stkCd, int $limit = FIN_REPORT_HISTORY): array
{
    if (!repo_fin_report_available()) {
        return [];
    }
    return db_all(
        'SELECT id, as_of_date, model, status, summary, error_msg, input_tokens, output_tokens, latency_ms, created_at'
        . ' FROM company_analysis_report WHERE stk_cd = ? ORDER BY as_of_date DESC, id DESC LIMIT ?',
        [$stkCd, max(1, min($limit, 100))]
    );
}

/**
 * 리포트 목록 WHERE 조각.
 * 값은 전부 바인딩되고, 비교 연산자·컬럼은 코드에 고정돼 있다.
 * @return array{0:string,1:array}
 */
function repo_fin_report_where(string $q, string $status, array $range): array
{
    $w = '';
    $p = [];
    if ($q !== '') {
        $w .= ' AND (r.stk_cd LIKE ? OR r.stk_nm LIKE ? OR cc.corp_name LIKE ?)';
        $like = '%' . $q . '%';
        $p[] = $like;
        $p[] = $like;
        $p[] = $like;
    }
    if (in_array($status, FIN_STATUSES, true)) {
        $w .= ' AND r.status = ?';
        $p[] = $status;
    }
    foreach ([['per', 'v.per'], ['pbr', 'v.pbr'], ['roe', 'v.roe'], ['debt', 'v.debt_ratio']] as [$key, $col]) {
        if (($range[$key . '_min'] ?? null) !== null) {
            $w .= ' AND ' . $col . ' >= ?';
            $p[] = (string)$range[$key . '_min'];
        }
        if (($range[$key . '_max'] ?? null) !== null) {
            $w .= ' AND ' . $col . ' <= ?';
            $p[] = (string)$range[$key . '_max'];
        }
    }
    return [$w, $p];
}

/** 리포트 목록 FROM 조각 (종목별 최신 리포트 + 최신 밸류에이션). */
function repo_fin_report_from(): string
{
    $ccOk = repo_can_read('company_corp_code');
    $valOk = repo_fin_valuation_available();
    $sql = ' FROM company_analysis_report r'
        . ' JOIN (SELECT stk_cd, MAX(as_of_date) AS d FROM company_analysis_report GROUP BY stk_cd) m'
        . ' ON m.stk_cd = r.stk_cd AND m.d = r.as_of_date';
    $sql .= $valOk
        ? ' LEFT JOIN (SELECT stk_cd, MAX(dt) AS d FROM company_valuation_daily GROUP BY stk_cd) vm'
            . ' ON vm.stk_cd = r.stk_cd'
            . ' LEFT JOIN company_valuation_daily v ON v.stk_cd = vm.stk_cd AND v.dt = vm.d'
        : ' LEFT JOIN (SELECT NULL AS stk_cd, NULL AS per, NULL AS pbr, NULL AS roe, NULL AS debt_ratio,'
            . ' NULL AS cur_prc, NULL AS dt, NULL AS financial_asof) v ON 1 = 0';
    $sql .= $ccOk
        ? ' LEFT JOIN company_corp_code cc ON cc.stk_cd = r.stk_cd'
        : ' LEFT JOIN (SELECT NULL AS stk_cd, NULL AS corp_name) cc ON 1 = 0';
    return $sql . ' WHERE 1=1';
}

/** 분석 리포트가 있는 종목 목록(종목별 최신 리포트 1건 + 최신 밸류에이션). */
function repo_fin_reports(string $q, string $status, array $range, string $sort, int $page): array
{
    if (!repo_fin_report_available()) {
        return ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => FIN_PAGE_SIZE];
    }
    [$w, $p] = repo_fin_report_where($q, $status, $range);
    $base = repo_fin_report_from() . $w;
    $order = FIN_SORTS[$sort]['sql'] ?? FIN_SORTS['report_desc']['sql'];
    return repo_paginate(
        'SELECT r.id, r.stk_cd, r.stk_nm, cc.corp_name, r.as_of_date, r.model, r.summary, r.status, r.error_msg,'
        . ' r.created_at, v.dt AS val_dt, v.cur_prc, v.per, v.pbr, v.roe, v.debt_ratio, v.financial_asof'
        . $base . ' ORDER BY ' . $order,
        'SELECT COUNT(*)' . $base,
        $p,
        $page,
        FIN_PAGE_SIZE
    );
}

/** 리포트 목록 요약(같은 필터 조건 기준). */
function repo_fin_report_summary(string $q, string $status, array $range): array
{
    $empty = ['stocks' => 0, 'ok' => 0, 'error' => 0, 'last_date' => null,
        'avg_per' => null, 'avg_pbr' => null, 'avg_roe' => null, 'avg_debt' => null];
    if (!repo_fin_report_available()) {
        return $empty;
    }
    [$w, $p] = repo_fin_report_where($q, $status, $range);
    $row = db_row(
        'SELECT COUNT(*) AS stocks,'
        . " SUM(CASE WHEN r.status = 'ok' THEN 1 ELSE 0 END) AS ok_cnt,"
        . " SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS err_cnt,"
        . ' MAX(r.as_of_date) AS last_date,'
        . ' AVG(CASE WHEN v.per > 0 THEN v.per END) AS avg_per,'
        . ' AVG(CASE WHEN v.pbr > 0 THEN v.pbr END) AS avg_pbr,'
        . ' AVG(v.roe) AS avg_roe, AVG(v.debt_ratio) AS avg_debt'
        . repo_fin_report_from() . $w,
        $p
    );
    if ($row === null) {
        return $empty;
    }
    return [
        'stocks' => (int)$row['stocks'],
        'ok' => (int)$row['ok_cnt'],
        'error' => (int)$row['err_cnt'],
        'last_date' => $row['last_date'],
        'avg_per' => $row['avg_per'] === null ? null : (float)$row['avg_per'],
        'avg_pbr' => $row['avg_pbr'] === null ? null : (float)$row['avg_pbr'],
        'avg_roe' => $row['avg_roe'] === null ? null : (float)$row['avg_roe'],
        'avg_debt' => $row['avg_debt'] === null ? null : (float)$row['avg_debt'],
    ];
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
        'trend' => repo_trend_today(),
        'fail_24h' => repo_recent_order_failures($accountId, 24),
    ];
}
