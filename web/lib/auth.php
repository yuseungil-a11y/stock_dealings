<?php
declare(strict_types=1);
/** 인증: 로그인/잠금/IP 제한/세션 수명/비밀번호 변경. */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** 계정 존재 여부를 노출하지 않는 공통 오류 문구. */
const AUTH_GENERIC_ERROR = '아이디 또는 비밀번호가 올바르지 않습니다.';

/** 존재하지 않는 계정에도 동일한 연산비용을 들이기 위한 더미 해시(무작위 비밀번호). */
const AUTH_DUMMY_HASH = '$2y$10$usesomesillystringfore7hnbRJHxXVLeakoG8K30oukPsA.ztMG';

function auth_user(): ?array
{
    if (empty($_SESSION['uid'])) {
        return null;
    }
    return [
        'id' => (int)$_SESSION['uid'],
        'username' => (string)($_SESSION['uname'] ?? ''),
        'display_name' => (string)($_SESSION['udisp'] ?? ''),
        'role' => (string)($_SESSION['urole'] ?? 'viewer'),
    ];
}

function auth_is_admin(): bool
{
    $u = auth_user();
    return $u !== null && $u['role'] === 'admin';
}

/** 세션 유휴/절대 수명 검사. 만료 시 세션 파기 후 사유 반환. */
function auth_check_lifetime(): ?string
{
    if (empty($_SESSION['uid'])) {
        return null;
    }
    $now = time();
    $login = (int)($_SESSION['login_at'] ?? 0);
    $seen = (int)($_SESSION['last_seen'] ?? 0);
    if ($login > 0 && ($now - $login) > APP_ABSOLUTE_TIMEOUT) {
        auth_logout();
        return 'absolute';
    }
    if ($seen > 0 && ($now - $seen) > APP_IDLE_TIMEOUT) {
        auth_logout();
        return 'idle';
    }
    $_SESSION['last_seen'] = $now;
    return null;
}

function auth_logout(): void
{
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $p = session_get_cookie_params();
        setcookie(session_name(), '', [
            'expires' => time() - 42000,
            'path' => $p['path'],
            'domain' => $p['domain'],
            'secure' => $p['secure'],
            'httponly' => $p['httponly'],
            'samesite' => $p['samesite'] ?? 'Strict',
        ]);
    }
    session_destroy();
}

function auth_log_attempt(?int $userId, string $username, bool $success): void
{
    try {
        db_exec(
            'INSERT INTO app_login_log (user_id, username, success, ip_addr, user_agent) VALUES (?,?,?,?,?)',
            [$userId, mb_substr($username, 0, 50), $success ? 1 : 0, client_ip(), client_ua()]
        );
    } catch (Throwable $e) {
        @error_log('[stock-web] login_log_failed :: ' . $e->getMessage());
    }
}

/** IP 기준 10분 내 시도 횟수. */
function auth_ip_attempts(): int
{
    try {
        return (int)db_val(
            'SELECT COUNT(*) FROM app_login_log WHERE ip_addr = ? AND created_at > (NOW() - INTERVAL ? MINUTE)',
            [client_ip(), APP_IP_WINDOW_MIN],
            0
        );
    } catch (Throwable $e) {
        return 0;
    }
}

/**
 * 로그인 처리.
 * @return array{ok:bool, status:int, message:string}
 */
function auth_login(string $username, string $password): array
{
    $started = microtime(true);
    $finish = static function (array $r) use ($started): array {
        // 계정 존재 여부를 응답시간으로 구분할 수 없도록 최소 응답시간을 맞춘다.
        $elapsedUs = (int)((microtime(true) - $started) * 1000000);
        $minUs = APP_LOGIN_MIN_MS * 1000;
        if ($elapsedUs < $minUs) {
            usleep($minUs - $elapsedUs);
        }
        return $r;
    };

    if (auth_ip_attempts() >= APP_IP_MAX_ATTEMPTS) {
        auth_log_attempt(null, $username, false);
        return $finish(['ok' => false, 'status' => 429,
            'message' => '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.']);
    }

    $user = null;
    if ($username !== '') {
        $user = db_row(
            'SELECT id, username, password_hash, display_name, role, is_active, failed_count, locked_until
               FROM app_user WHERE username = ? LIMIT 1',
            [$username]
        );
    }

    $hash = ($user !== null) ? (string)$user['password_hash'] : AUTH_DUMMY_HASH;
    $verified = password_verify($password, $hash);

    $locked = false;
    if ($user !== null && !empty($user['locked_until'])) {
        $locked = strtotime((string)$user['locked_until']) > time();
    }
    $inactive = ($user !== null && (int)$user['is_active'] !== 1);

    if ($user === null || !$verified || $locked || $inactive) {
        if ($user !== null && !$locked && !$inactive) {
            $failed = (int)$user['failed_count'] + 1;
            if ($failed >= APP_MAX_FAILED) {
                db_exec(
                    'UPDATE app_user SET failed_count = ?, locked_until = (NOW() + INTERVAL ? MINUTE) WHERE id = ?',
                    [$failed, APP_LOCK_MINUTES, (int)$user['id']]
                );
            } else {
                db_exec('UPDATE app_user SET failed_count = ? WHERE id = ?', [$failed, (int)$user['id']]);
            }
        }
        auth_log_attempt($user !== null ? (int)$user['id'] : null, $username, false);
        return $finish(['ok' => false, 'status' => 401, 'message' => AUTH_GENERIC_ERROR]);
    }

    // 성공
    db_exec('UPDATE app_user SET failed_count = 0, locked_until = NULL, last_login_at = NOW() WHERE id = ?',
        [(int)$user['id']]);
    auth_log_attempt((int)$user['id'], $username, true);

    $csrf = bin2hex(random_bytes(32));
    session_regenerate_id(true);
    $_SESSION = [];
    $_SESSION['uid'] = (int)$user['id'];
    $_SESSION['uname'] = (string)$user['username'];
    $_SESSION['udisp'] = (string)($user['display_name'] ?? '');
    $_SESSION['urole'] = (string)$user['role'];
    $_SESSION['login_at'] = time();
    $_SESSION['last_seen'] = time();
    $_SESSION['csrf'] = $csrf;

    return $finish(['ok' => true, 'status' => 200, 'message' => '']);
}

/** 비밀번호 복잡도 검사. 문제가 없으면 null. */
function auth_password_problem(string $pw, string $username): ?string
{
    $len = mb_strlen($pw, 'UTF-8');
    if ($len < APP_PW_MIN_LEN) {
        return '새 비밀번호는 ' . APP_PW_MIN_LEN . '자 이상이어야 합니다.';
    }
    if ($len > 200) {
        return '새 비밀번호가 너무 깁니다.';
    }
    $classes = 0;
    $classes += preg_match('/[a-z]/', $pw) ? 1 : 0;
    $classes += preg_match('/[A-Z]/', $pw) ? 1 : 0;
    $classes += preg_match('/[0-9]/', $pw) ? 1 : 0;
    $classes += preg_match('/[^A-Za-z0-9]/', $pw) ? 1 : 0;
    if ($classes < 3) {
        return '영문 대문자·소문자·숫자·특수문자 중 3종류 이상을 포함해야 합니다.';
    }
    if ($username !== '' && stripos($pw, $username) !== false) {
        return '비밀번호에 아이디를 포함할 수 없습니다.';
    }
    return null;
}

/**
 * 비밀번호 변경 (현재 비밀번호 확인 필수).
 * @return array{ok:bool,message:string}
 */
function auth_change_password(int $userId, string $current, string $new, string $confirm): array
{
    $user = db_row('SELECT id, username, password_hash FROM app_user WHERE id = ? LIMIT 1', [$userId]);
    if ($user === null) {
        return ['ok' => false, 'message' => '사용자를 찾을 수 없습니다.'];
    }
    if (!password_verify($current, (string)$user['password_hash'])) {
        auth_log_attempt($userId, (string)$user['username'], false);
        return ['ok' => false, 'message' => '현재 비밀번호가 올바르지 않습니다.'];
    }
    if ($new !== $confirm) {
        return ['ok' => false, 'message' => '새 비밀번호 확인이 일치하지 않습니다.'];
    }
    if ($new === $current) {
        return ['ok' => false, 'message' => '현재 비밀번호와 다른 비밀번호를 사용해 주세요.'];
    }
    $problem = auth_password_problem($new, (string)$user['username']);
    if ($problem !== null) {
        return ['ok' => false, 'message' => $problem];
    }
    $hash = password_hash($new, PASSWORD_DEFAULT);
    if (!is_string($hash)) {
        return ['ok' => false, 'message' => '비밀번호를 변경할 수 없습니다.'];
    }
    try {
        db_exec('UPDATE app_user SET password_hash = ? WHERE id = ?', [$hash, $userId]);
    } catch (Throwable $e) {
        @error_log('[stock-web] pw_change_failed :: ' . $e->getMessage());
        return ['ok' => false, 'message' => '비밀번호를 변경할 수 없습니다. 관리자에게 문의해 주세요.'];
    }
    session_regenerate_id(true);
    return ['ok' => true, 'message' => '비밀번호가 변경되었습니다.'];
}
