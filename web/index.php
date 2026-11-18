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

/* 303 리다이렉트 뒤(항상 GET)에 한 번만 보여줄 플래시 메시지를 세션에서 꺼낸다.
 * algo_selection_save / algo_params_save / auto_trading_start / auto_trading_stop 처럼
 * "admin 검사 → CSRF 검사 → 처리 → 303" 뒤에도 구체적인 결과 문구를 보여주기 위함
 * (trend_rescan 은 화면 자체 쿼리 파라미터로 처리하므로 이 메커니즘을 쓰지 않는다). */
if ($method !== 'POST') {
    $fl = flash_pop();
    if ($fl['msg'] !== null) {
        $flash = $fl['msg'];
        $flashType = $fl['type'];
    }
}

if ($method === 'POST') {
    $action = is_string($_POST['action'] ?? null) ? $_POST['action'] : '';
    if (!csrf_valid()) {
        /* 로그인 요청에 한해서만 막다른 403 페이지 대신 로그인 폼을 다시 보여준다.
         * 사파리 계열 + 리버스 프록시 HTTPS 환경에서 방금 세팅된 세션 쿠키가 첫 POST 에
         * 실리지 않아 첫 로그인이 항상 CSRF 실패하던 문제(v1.6.2 SameSite=Lax 완화 후에도 재발) 대응.
         * - 미인증 상태일 때만 적용(로그인된 세션의 토큰은 건드리지 않음 → 기존 하드 403 유지).
         * - 토큰을 새로 발급해 폼에 실어 주고, 아이디는 유지·비밀번호는 다시 입력받는다.
         * - 인증(auth_login)은 수행하지 않으며 상태 변경도 없다. 응답 코드는 403 유지.
         * login 이외의 모든 액션은 아래 기존 동작(하드 403)을 그대로 따른다. */
        if ($action === 'login' && $user === null) {
            unset($_SESSION['csrf']);
            csrf_token();
            http_response_code(403);
            $loginNotice = '세션이 갱신되었습니다. 다시 로그인해 주세요.';
            $loginUsername = clean_text($_POST['username'] ?? '', 50);
            $nextParam = is_string($_POST['next'] ?? null) && isset($routes[$_POST['next']])
                ? $_POST['next'] : 'dashboard';
            $expiredParam = null;
            $bye = false;
            include __DIR__ . '/views/login.php';
            exit;
        }
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
    } elseif ($action === 'trade_report_request') {
        /* 거래 종합분석 리포트(Claude, 참고용) 요청만 기록한다(관리자 전용).
         * trend_rescan 과 동일한 패턴 — trade_analysis_request 에 행 하나를 넣는 것이 전부이며,
         * 주문 · 설정 · 알고리즘 파라미터 등 다른 어떤 것도 바꾸지 않는다.
         * 실제 분석(Claude 호출)은 서버 모듈이 요청을 확인한 뒤 수행한다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '종합분석 요청은 관리자만 할 수 있습니다.']);
            exit;
        }
        $reqAccounts = repo_accounts();
        $reqAccount = repo_resolve_account($reqAccounts);
        $reqAccountId = $reqAccount !== null ? (int)$reqAccount['id'] : 0;
        $periodStart = clean_date($_POST['period_start'] ?? null);
        $periodEnd = clean_date($_POST['period_end'] ?? null);
        if ($reqAccountId <= 0 || $periodStart === null || $periodEnd === null) {
            header('Location: ' . url_page('trade.report', ['rq' => 'err']), true, 303);
            exit;
        }
        if ($periodStart > $periodEnd) {
            // 화면이 정상 사용 시엔 이미 정렬된 값을 보내지만, 직접 POST 등 방어적으로 교체해 적용한다
            // (trade_report.php 의 조회 기간 처리와 동일한 방식).
            [$periodStart, $periodEnd] = [$periodEnd, $periodStart];
        }
        $rangeDays = (int)((strtotime($periodEnd) - strtotime($periodStart)) / 86400);
        if ($rangeDays > TAREP_MAX_RANGE_DAYS) {
            header('Location: ' . url_page('trade.report', ['rq' => 'range']), true, 303);
            exit;
        }
        $lastReq = (int)($_SESSION['tarep_req_at'] ?? 0);
        $busy = ($lastReq > 0 && (time() - $lastReq) < TAREP_REQUEST_COOLDOWN_SEC)
            || repo_tarep_pending($reqAccountId) !== null;
        if ($busy) {
            header('Location: ' . url_page('trade.report', ['rq' => 'busy']), true, 303);
            exit;
        }
        $ok = repo_tarep_request_insert($reqAccountId, $periodStart, $periodEnd, $user['username']);
        if ($ok) {
            $_SESSION['tarep_req_at'] = time();
        }
        header('Location: ' . url_page('trade.report', ['rq' => $ok ? 'ok' : 'err']), true, 303);
        exit;
    } elseif ($action === 'algo_selection_save') {
        /* 알고리즘 사용여부 · 우선순위 저장(관리자 전용).
         * risk_guard(is_locked=1) 는 폼 값과 무관하게 repo_update_algorithm_selection() 안에서
         * 항상 enabled=1 로 강제된다(클라이언트 disabled 속성만 믿지 않음). */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '알고리즘 사용여부 · 우선순위 변경은 관리자만 할 수 있습니다.']);
            exit;
        }
        $rows = [];
        foreach ((array)($_POST['sel'] ?? []) as $id => $row) {
            if (!is_array($row)) {
                continue;
            }
            $rows[] = [
                'id' => (int)$id,
                'enabled' => !empty($row['enabled']),
                'priority' => clean_int($row['priority'] ?? 1, 1, 1, 999),
            ];
        }
        $res = repo_update_algorithm_selection($rows, $user['username']);
        flash_set(
            $res['ok']
                ? ('알고리즘 사용여부 · 우선순위를 저장했습니다(' . $res['changed'] . '건). '
                    . '서버가 다음 평가 주기부터 반영합니다.')
                : '일부 또는 전체 항목을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.',
            $res['ok'] ? 'ok' : 'err'
        );
        header('Location: ' . url_page('control.algorithms'), true, 303);
        exit;
    } elseif ($action === 'holding_sell_protect_save') {
        /* 보유종목 매도보호(매도 알고리즘 제외) 지정/해제(관리자 전용).
         * 보호 종목에는 -7% 손절(risk_guard)을 포함한 어떤 매도 알고리즘 신호도 나가지 않는다 —
         * 서버 runner.run_cycle 이 매 사이클 position_state.sell_protected 를 읽어 걸러낸다.
         * 이 동작은 position_state 의 매도보호 3컬럼만 쓰며 주문 · 설정 · 파라미터는 바꾸지 않는다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '매도보호 지정/해제는 관리자만 할 수 있습니다.']);
            exit;
        }
        $spAccounts = repo_accounts();
        $spAccount = repo_resolve_account($spAccounts);
        $spAccountId = $spAccount !== null ? (int)$spAccount['id'] : 0;
        // 폼을 띄운 계좌와 현재 세션 계좌가 다르면(다른 탭에서 계좌 전환 등) 엉뚱한 계좌에 쓰지 않는다
        $formAccountId = clean_int($_POST['account_id'] ?? 0, 0);
        $retQuery = array_filter([
            'sort' => clean_enum($_POST['sort'] ?? '', array_keys(HOLDING_SORTS), ''),
            'q' => clean_text($_POST['q'] ?? '', 30),
        ], static fn($v) => $v !== '');
        if ($spAccountId <= 0 || $formAccountId !== $spAccountId) {
            flash_set('계좌 정보가 맞지 않아 저장하지 않았습니다. 화면을 새로 고친 뒤 다시 시도해 주세요.', 'err');
            header('Location: ' . url_page('account.holdings', $retQuery), true, 303);
            exit;
        }
        $protect = [];
        foreach ((array)($_POST['protect'] ?? []) as $cd) {
            $cd = clean_text($cd, 12);
            if ($cd !== '') {
                $protect[] = $cd;
            }
        }
        $shown = [];
        foreach ((array)($_POST['shown'] ?? []) as $cd) {
            $cd = clean_text($cd, 12);
            if ($cd !== '') {
                $shown[] = $cd;
            }
        }
        $res = repo_update_sell_protect($spAccountId, $protect, $user['username'], $shown);
        flash_set(
            $res['ok']
                ? ('매도보호 설정을 저장했습니다(' . $res['changed'] . '건 변경, 현재 보호 ' . $res['protected']
                    . '종목). 서버가 다음 평가 주기부터 반영합니다 — 보호 종목은 손절을 포함한 모든 매도 '
                    . '알고리즘이 작동하지 않습니다.')
                : '일부 또는 전체 종목의 매도보호 설정을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.',
            $res['ok'] ? ($res['protected'] > 0 ? 'warn' : 'ok') : 'err'
        );
        header('Location: ' . url_page('account.holdings', $retQuery), true, 303);
        exit;
    } elseif ($action === 'sell_protect_clear') {
        /* 매도보호 단건 해제(관리자 전용). 보유종목 표(holding 기준)에 나오지 않는
         * 전량매도 종목의 남은 매도보호를 수동으로 해제하기 위한 것이다(자동 해제는 하지 않는다).
         * position_state 의 매도보호 3컬럼만 쓰며 주문 · 설정 · 파라미터는 바꾸지 않는다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '매도보호 해제는 관리자만 할 수 있습니다.']);
            exit;
        }
        $clrAccounts = repo_accounts();
        $clrAccount = repo_resolve_account($clrAccounts);
        $clrAccountId = $clrAccount !== null ? (int)$clrAccount['id'] : 0;
        // holding_sell_protect_save 와 같이, 폼을 띄운 계좌와 현재 세션 계좌가 다르면 쓰지 않는다
        $clrFormAccountId = clean_int($_POST['account_id'] ?? 0, 0);
        if ($clrAccountId <= 0 || $clrFormAccountId !== $clrAccountId) {
            flash_set('계좌 정보가 맞지 않아 해제하지 않았습니다. 화면을 새로 고친 뒤 다시 시도해 주세요.', 'err');
            header('Location: ' . url_page('account.holdings'), true, 303);
            exit;
        }
        $clrStkCd = clean_text($_POST['stk_cd'] ?? '', 12);
        $ok = $clrStkCd !== ''
            && repo_clear_sell_protect($clrAccountId, $clrStkCd, $user['username']);
        flash_set(
            $ok
                ? ('매도보호를 해제했습니다(' . $clrStkCd . '). 서버가 다음 평가 주기부터 매도 알고리즘을 다시 적용합니다.')
                : '매도보호를 해제하지 못했습니다(이미 해제됐거나 저장 오류). 화면을 새로 고친 뒤 다시 시도해 주세요.',
            $ok ? 'ok' : 'err'
        );
        header('Location: ' . url_page('account.holdings'), true, 303);
        exit;
    } elseif ($action === 'algo_params_save') {
        /* 알고리즘 파라미터 값 저장(관리자 전용). 타입 · 범위 · enum 검증은
         * repo_update_algorithm_params() 안에서 서버 Python validate() 와 같은 규칙으로 수행하며,
         * 검증에 하나라도 실패하면 아무것도 저장하지 않는다(all-or-nothing). */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '알고리즘 파라미터 변경은 관리자만 할 수 있습니다.']);
            exit;
        }
        $algoId = clean_int($_POST['algo_id'] ?? 0, 0, 0);
        $algoRow = $algoId > 0 ? repo_algorithm_by_id($algoId) : null;
        if ($algoRow === null) {
            $res = ['ok' => false, 'errors' => ['알고리즘을 찾을 수 없습니다.'], 'changed' => 0];
        } else {
            $res = repo_update_algorithm_params($algoId, (array)($_POST['params'] ?? []), $user['username']);
        }
        flash_set(
            $res['ok']
                ? ('파라미터를 저장했습니다(' . $res['changed'] . '건 변경). '
                    . '서버가 다음 평가 주기부터 반영합니다 — 값이 서로 맞지 않으면 서버가 이 알고리즘을 '
                    . '자동으로 비활성화하고 경보를 낼 수 있습니다.')
                : ('파라미터를 저장하지 못했습니다: ' . implode(' · ', $res['errors'])),
            $res['ok'] ? 'ok' : 'err'
        );
        header('Location: ' . url_page('control.algorithms', $algoRow !== null ? ['algo' => $algoRow['code']] : []),
            true, 303);
        exit;
    } elseif ($action === 'auto_trading_start') {
        /* 자동거래 시작(관리자 전용) — 로그인 아이디·비밀번호 재확인 필수(step-up 재인증, 로그인 절차 아님).
         * 클라이언트 JS 로 이미 두 칸을 채워야 버튼이 눌리게 막지만, curl 등으로 이 검사를 건너뛰고
         * 곧바로 POST 할 수 있으므로 서버에서도 반드시 다시 검사한다(방어의 마지막 줄).
         * 본인 계정으로만 재확인을 허용한다(다른 admin 자격증명으로 우회 방지) — 계정명 일치 여부와
         * 무관하게 항상 password_verify 를 한 번 수행해 타이밍으로 계정 존재/일치를 노출하지 않는다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '자동거래 시작은 관리자만 할 수 있습니다.']);
            exit;
        }
        $inUser = clean_text($_POST['username'] ?? '', 50);
        $inPw = is_string($_POST['password'] ?? null) ? $_POST['password'] : '';
        $freshHash = (string)(db_val('SELECT password_hash FROM app_user WHERE id = ? LIMIT 1', [$user['id']]) ?? AUTH_DUMMY_HASH);
        $pwOk = password_verify($inPw, $freshHash);
        $verified = ($inUser === $user['username']) && $pwOk;
        if (!$verified) {
            flash_set('아이디 또는 비밀번호가 올바르지 않아 시작 요청을 취소했습니다.', 'err');
            header('Location: ' . url_page('control.auto_trading'), true, 303);
            exit;
        }
        $ok = repo_insert_auto_trading_command('start', $user['username']);
        flash_set(
            $ok ? '자동거래 시작 요청을 접수했습니다. 서버가 곧 처리합니다(아래 명령 이력에서 확인하세요).'
                : '요청 접수에 실패했습니다. 잠시 후 다시 시도해 주세요.',
            $ok ? 'warn' : 'err'
        );
        header('Location: ' . url_page('control.auto_trading'), true, 303);
        exit;
    } elseif ($action === 'auto_trading_stop') {
        /* 자동거래 중지(관리자 전용) — 중지는 항상 안전한 방향이므로 REAL 재확인은 요구하지 않는다. */
        if ($user === null || !auth_is_admin()) {
            http_response_code(403);
            render_standalone(['title' => '접근 권한 없음',
                'message' => '자동거래 중지는 관리자만 할 수 있습니다.']);
            exit;
        }
        $ok = repo_insert_auto_trading_command('stop', $user['username']);
        flash_set(
            $ok ? '자동거래 중지 요청을 접수했습니다. 서버가 곧 처리합니다(아래 명령 이력에서 확인하세요).'
                : '요청 접수에 실패했습니다. 잠시 후 다시 시도해 주세요.',
            $ok ? 'ok' : 'err'
        );
        header('Location: ' . url_page('control.auto_trading'), true, 303);
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
    if ($expired !== null) {
        /* 이번 요청에서 auth_check_lifetime() 이 세션을 막 파기했다(쿠키 삭제 + session_destroy).
         * 이 상태에서 바로 폼을 그리면 CSRF 토큰이 파기된 세션에 써져 영속되지 않아
         * 첫 로그인 POST 가 항상 CSRF 실패한다. 다른 미인증 경로와 동일하게 깨끗한 로그인 URL 로
         * 303 리다이렉트해 새 요청에서 새 세션·새 토큰을 발급받게 한다(만료 안내·next 보존). */
        $next = isset($_GET['next']) && is_string($_GET['next']) && isset($routes[$_GET['next']])
            ? $_GET['next'] : 'dashboard';
        header('Location: ' . u('index.php?p=login&next=' . rawurlencode($next))
            . '&expired=' . rawurlencode($expired), true, 303);
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
