<?php
declare(strict_types=1);
/**
 * JSON 조회 API (GET 전용, 읽기 전용).
 * 미인증 요청은 401 JSON 으로 응답한다(리다이렉트하지 않음).
 */
define('STOCK_APP', true);
define('STOCK_JSON', true);
require __DIR__ . '/lib/bootstrap.php';

function json_out(array $payload, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

if (strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET')) !== 'GET') {
    header('Allow: GET');
    json_out(['ok' => false, 'error' => 'GET 요청만 허용됩니다.'], 405);
}

$expired = auth_check_lifetime();
$user = auth_user();
if ($user === null) {
    json_out(['ok' => false, 'error' => '인증이 필요합니다.', 'code' => 'unauthenticated',
        'expired' => $expired], 401);
}

$r = isset($_GET['r']) && is_string($_GET['r']) ? $_GET['r'] : '';
$accounts = repo_accounts();
$account = repo_resolve_account($accounts);
$accountId = $account !== null ? (int)$account['id'] : null;

$serverTime = date('Y-m-d H:i:s');

switch ($r) {
    case 'ping':
        json_out(['ok' => true, 'server_time' => $serverTime, 'user' => $user['username'], 'web_version' => STOCK_WEB_VERSION]);

    case 'status':
        $rows = [];
        foreach (repo_server_status() as $row) {
            $eff = repo_effective_status($row);
            $rows[] = [
                'component' => $row['component'],
                'status' => $eff['status'],
                'raw_status' => $row['status'],
                'label' => status_label($eff['status']),
                'message' => $row['message'],
                'note' => $eff['note'],
                'updated_at' => $row['updated_at'],
                'age_sec' => $eff['age'],
            ];
        }
        json_out(['ok' => true, 'server_time' => $serverTime, 'components' => $rows,
            'settings' => repo_system_settings()]);

    case 'dashboard':
        $d = repo_dashboard($accountId);
        $status = [];
        foreach (repo_server_status() as $row) {
            $eff = repo_effective_status($row);
            $status[] = [
                'component' => $row['component'],
                'status' => $eff['status'],
                'label' => status_label($eff['status']),
                'message' => $row['message'],
                'note' => $eff['note'],
                'updated_at' => $row['updated_at'],
            ];
        }
        json_out([
            'ok' => true,
            'server_time' => $serverTime,
            'account' => $account === null ? null : [
                'id' => (int)$account['id'],
                'account_no_masked' => mask_account((string)$account['account_no']),
                'env' => $account['env'],
                'alias' => $account['alias'],
            ],
            'balance' => $d['balance'],
            'holding_totals' => $d['holding_totals'],
            // 상장폐지 종목 표시를 위해 상위 보유종목에도 delisted / real_profit_rate 를 포함한다.
            'holdings_top' => $accountId === null ? []
                : array_slice(repo_holdings($accountId, 'evlt_amt_desc'), 0, 5),
            'today' => [
                'pl_amt' => $d['today_pl'],
                'orders' => $d['today_orders'],
                'signals' => $d['today_signals'],
                'fail_24h' => $d['fail_24h'],
            ],
            'llm' => $d['llm'],
            // 오늘 산업 트렌드 스캔 요약(오늘 실행이 없으면 null)
            'trend' => $d['trend'],
            'components' => $status,
            'events' => repo_recent_events(10),
        ]);

    case 'holdings':
        if ($accountId === null) {
            json_out(['ok' => true, 'server_time' => $serverTime, 'rows' => [], 'totals' => null]);
        }
        $sort = clean_enum($_GET['sort'] ?? '', array_keys(HOLDING_SORTS), 'evlt_amt_desc');
        json_out(['ok' => true, 'server_time' => $serverTime,
            'rows' => repo_holdings($accountId, $sort, clean_text($_GET['q'] ?? '', 30)),
            'totals' => repo_holding_totals($accountId)]);

    case 'balance_series':
        if ($accountId === null) {
            json_out(['ok' => true, 'server_time' => $serverTime, 'rows' => []]);
        }
        json_out(['ok' => true, 'server_time' => $serverTime,
            'rows' => repo_balance_series($accountId, clean_int($_GET['days'] ?? 30, 30, 1, 365))]);

    case 'events':
        $level = clean_enum($_GET['level'] ?? '', ['DEBUG', 'INFO', 'WARN', 'ERROR'], '');
        json_out(['ok' => true, 'server_time' => $serverTime,
            'rows' => repo_events(null, null, $level, '', '', 1)['rows']]);

    /* ------------------------------------------------ 기업 재무분석 (리서치)
     * 읽기 전용 · 계좌/주문과 무관. 하루 1회만 바뀌므로 자동 갱신에는 쓰지 않는다. */
    case 'research_stocks':
        json_out(['ok' => true, 'server_time' => $serverTime,
            'available' => repo_fin_available() || repo_fin_report_available(),
            'rows' => repo_fin_stocks(clean_text($_GET['q'] ?? '', 40),
                clean_int($_GET['page'] ?? 1, 1, 1, 100000))['rows']]);

    case 'research_company':
        $stkRaw = clean_text($_GET['stk'] ?? '', 12);
        if (preg_match('/^[A-Za-z0-9]{1,12}$/', $stkRaw) !== 1) {
            json_out(['ok' => false, 'error' => '종목코드(stk)가 필요합니다.'], 400);
        }
        $rpt = repo_fin_report_latest($stkRaw);
        json_out(['ok' => true, 'server_time' => $serverTime,
            'stk_cd' => $stkRaw,
            'names' => repo_fin_stock_name($stkRaw),
            'financials' => repo_fin_statements($stkRaw, FIN_YEARS),
            'valuation' => repo_fin_valuation_latest($stkRaw),
            // 본문(report_text)은 화면에서만 보여준다(응답 크기 · 용도 분리).
            'report' => $rpt === null ? null : [
                'as_of_date' => $rpt['as_of_date'],
                'model' => $rpt['model'],
                'status' => $rpt['status'],
                'summary' => $rpt['summary'],
                'error_msg' => $rpt['error_msg'],
                'created_at' => $rpt['created_at'],
            ],
            'disclaimer' => '참고용 리서치 데이터입니다. 매매를 자동으로 실행하지 않습니다.']);

    case 'accounts':
        $rows = [];
        foreach ($accounts as $a) {
            $rows[] = [
                'id' => (int)$a['id'],
                'account_no_masked' => mask_account((string)$a['account_no']),
                'env' => $a['env'],
                'alias' => $a['alias'],
                'is_active' => (int)$a['is_active'],
            ];
        }
        json_out(['ok' => true, 'server_time' => $serverTime, 'rows' => $rows,
            'selected' => $accountId]);

    default:
        json_out(['ok' => false, 'error' => '알 수 없는 요청입니다.',
            'available' => ['ping', 'status', 'dashboard', 'holdings', 'balance_series', 'events', 'accounts',
                'research_stocks', 'research_company']], 404);
}
