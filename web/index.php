<?php
declare(strict_types=1);
/**
 * 주식 자동매매 조회·관제 웹 — 단일 진입점(front controller).
 * 모든 페이지 요청은 이 파일을 통과하므로 인증/보안 검사를 우회하는 경로가 없다.
 */
define('STOCK_APP', true);
require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/routes.php';

$routes = app_routes();
$expired = null;

/* ---------------------------------------------------------- 세션 수명 */
$reason = auth_check_lifetime();
if ($reason !== null) {
    $expired = $reason;
}

$page = isset($_GET['p']) && is_string($_GET['p']) ? $_GET['p'] : 'dashboard';
$method = strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET'));
$user = auth_user();

/* ------------------------------------------------- 내보내기는 GET 전용 */
if (isset($_GET['export']) && $method !== 'GET') {
    http_response_code(405);
    header('Allow: GET');
    render_standalone(['title' => '허용되지 않는 요청 방식',
        'message' => '내보내기는 GET 요청만 허용됩니다.']);
    exit;
}

/* ---------------------------------------------------------- POST 액션 */
$loginError = null;
$flash = null;
$flashType = 'ok';

if ($method === 'POST') {
    $action = is_string($_POST['action'] ?? null) ? $_POST['action'] : '';
    if (!csrf_valid()) {
        http_response_code(403);
        $tpl = ['title' => '요청 거부', 'message' => '보안 토큰이 유효하지 않습니다. 페이지를 새로 고친 뒤 다시 시도해 주세요.'];
        render_standalone($tpl);
        exit;
    }
    if ($action === 'login') {
        $username = clean_text($_POST['username'] ?? '', 50);
        $password = is_string($_POST['password'] ?? null) ? $_POST['password'] : '';
        $res = auth_login($username, $password);
        if ($res['ok']) {
            $next = is_string($_POST['next'] ?? null) ? $_POST['next'] : 'dashboard';
            if (!isset($routes[$next])) {
                $next = 'dashboard';
            }
            header('Location: ' . url_page($next), true, 303);
            exit;
        }
        http_response_code($res['status']);
        $loginError = $res['message'];
        $page = 'login';
        $user = null;
    } elseif ($action === 'logout') {
        auth_logout();
        header('Location: ' . u('index.php?p=login&bye=1'), true, 303);
        exit;
    } elseif ($action === 'change_password') {
        if ($user === null) {
            header('Location: ' . u('index.php?p=login'), true, 303);
            exit;
        }
        $res = auth_change_password(
            $user['id'],
            is_string($_POST['current_password'] ?? null) ? $_POST['current_password'] : '',
            is_string($_POST['new_password'] ?? null) ? $_POST['new_password'] : '',
            is_string($_POST['confirm_password'] ?? null) ? $_POST['confirm_password'] : ''
        );
        $flash = $res['message'];
        $flashType = $res['ok'] ? 'ok' : 'err';
        $page = 'system.profile';
    } elseif ($action === 'trend_rescan') {
        /* 산업 트렌드 수동 재조사 "요청"만 기록한다(관리자 전용).
         * 이 동작은 trend_scan_request 에 행 하나를 넣는 것이 전부이며,
         * 주문 · 설정 · 알고리즘 파라미터 등 다른 어떤 것도 바꾸지 않는다.
         * 실제 조사는 서버 모듈이 요청을 확인한 뒤 수행한다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '재조사 요청은 관리자만 할 수 있습니다.']);
            exit;
        }
        $lastReq = (int)($_SESSION['trend_req_at'] ?? 0);
        $busy = ($lastReq > 0 && (time() - $lastReq) < TREND_REQUEST_COOLDOWN_SEC)
            || repo_trend_manual_recent(TREND_MANUAL_RECENT_MIN) !== null;
        if ($busy) {
            header('Location: ' . url_page('strategy.trend', ['rq' => 'busy']), true, 303);
            exit;
        }
        $ok = repo_trend_request_insert($user['username']);
        if ($ok) {
            $_SESSION['trend_req_at'] = time();
        }
        header('Location: ' . url_page('strategy.trend', ['rq' => $ok ? 'ok' : 'err']), true, 303);
        exit;
    } else {
        http_response_code(400);
        render_standalone(['title' => '잘못된 요청', 'message' => '처리할 수 없는 요청입니다.']);
        exit;
    }
}

/* ---------------------------------------------------------- 로그인 화면 */
if ($page === 'login' || $user === null) {
    if ($user !== null && $page === 'login') {
        header('Location: ' . url_page('dashboard'), true, 303);
        exit;
    }
    if ($user === null && $page !== 'login') {
        // 미인증 접근 → 로그인으로 리다이렉트 (요청 페이지 보존)
        $next = isset($routes[$page]) ? $page : 'dashboard';
        header('Location: ' . u('index.php?p=login&next=' . rawurlencode($next))
            . ($expired !== null ? '&expired=' . rawurlencode($expired) : ''), true, 303);
        exit;
    }
    $nextParam = isset($_GET['next']) && is_string($_GET['next']) && isset($routes[$_GET['next']])
        ? $_GET['next'] : 'dashboard';
    $expiredParam = $expired ?? (isset($_GET['expired']) && is_string($_GET['expired']) ? $_GET['expired'] : null);
    $bye = isset($_GET['bye']);
    include __DIR__ . '/views/login.php';
    exit;
}

/* ---------------------------------------------------------- 인증 후 라우팅 */
if (!isset($routes[$page])) {
    http_response_code(404);
    $page = 'dashboard';
    $route = $routes['dashboard'];
    $notFound = true;
} else {
    $route = $routes[$page];
    $notFound = false;
}

if (!empty($route['admin']) && !auth_is_admin()) {
    http_response_code(403);
    render_standalone(['title' => '접근 권한 없음', 'message' => '이 화면은 관리자만 볼 수 있습니다.']);
    exit;
}

/* ---------------------------------------------- 분석용 내보내기(CSV/JSON)
 * 인증을 통과한 뒤에만 도달하며, 화면과 동일한 필터를 그대로 사용한다. */
if ($page === 'trade.analysis' && isset($_GET['export']) && is_string($_GET['export'])) {
    require __DIR__ . '/lib/export.php';
    export_trade_analysis(
        clean_enum($_GET['export'], ['csv', 'json'], 'csv'),
        clean_date($_GET['from'] ?? null),
        clean_date($_GET['to'] ?? null),
        clean_text($_GET['q'] ?? '', 40),
        clean_enum($_GET['result'] ?? '', ANALYSIS_RESULTS, '')
    );
}

$accounts = repo_accounts();
$account = repo_resolve_account($accounts);
$accountId = $account !== null ? (int)$account['id'] : null;

ob_start();
include __DIR__ . '/views/' . $route['view'] . '.php';
$content = (string)ob_get_clean();

include __DIR__ . '/views/layout.php';

/* ---------------------------------------------------------- 헬퍼 */

function url_page(string $page, array $extra = []): string
{
    $q = array_merge(['p' => $page], $extra);
    return u('index.php') . '?' . http_build_query($q);
}

/** 레이아웃 없이 단독 메시지 페이지 출력. */
function render_standalone(array $tpl): void
{
    $title = (string)($tpl['title'] ?? '알림');
    $message = (string)($tpl['message'] ?? '');
    echo '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width, initial-scale=1">'
        . '<title>' . h($title) . ' · ' . h(APP_NAME) . '</title>'
        . '<link rel="stylesheet" href="' . h(asset_url('assets/css/app.css')) . '">'
        . favicon_links() . '</head>'
        . '<body class="standalone"><main class="msg-box"><h1>' . h($title) . '</h1><p>' . h($message) . '</p>'
        . '<p><a class="btn" href="' . h(u('index.php')) . '">처음 화면으로</a></p></main></body></html>';
}
