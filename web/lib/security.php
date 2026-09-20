<?php
declare(strict_types=1);
/** 보안 공통: 오류표시 차단, 보안 헤더, 세션, CSRF. */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** display_errors OFF + 오류는 웹루트 밖 파일로. */
function app_init_errors(): void
{
    ini_set('display_errors', '0');
    ini_set('display_startup_errors', '0');
    ini_set('log_errors', '1');
    ini_set('error_log', app_log_file());
    ini_set('html_errors', '0');
    error_reporting(E_ALL);

    set_exception_handler(static function (Throwable $e): void {
        app_fatal('unhandled_exception', '요청을 처리할 수 없습니다.', get_class($e) . ': ' . $e->getMessage()
            . ' @' . $e->getFile() . ':' . $e->getLine());
    });
    register_shutdown_function(static function (): void {
        $err = error_get_last();
        if ($err !== null && in_array($err['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR], true)) {
            @error_log('[stock-web] fatal :: ' . $err['message'] . ' @' . $err['file'] . ':' . $err['line']);
        }
    });
}

/** 보안 헤더 (외부 공개 필수 항목). */
function app_send_security_headers(bool $json = false): void
{
    if (headers_sent()) {
        return;
    }
    header_remove('X-Powered-By');
    $csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        . "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        . "base-uri 'self'; object-src 'none'";
    header('Content-Security-Policy: ' . $csp);
    header('X-Frame-Options: DENY');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: no-referrer');
    header('X-Robots-Tag: noindex, nofollow, noarchive');
    header('Permissions-Policy: geolocation=(), microphone=(), camera=()');
    header('Cache-Control: no-store, no-cache, must-revalidate, private');
    header('Pragma: no-cache');
    header('Expires: 0');
    header('Content-Type: ' . ($json ? 'application/json; charset=utf-8' : 'text/html; charset=utf-8'));
}

/** 세션 시작: HttpOnly / Secure(HTTPS) / SameSite=Strict / path=앱 기준경로. */
function app_start_session(): void
{
    if (session_status() === PHP_SESSION_ACTIVE) {
        return;
    }
    $base = base_path();
    ini_set('session.use_strict_mode', '1');
    ini_set('session.use_only_cookies', '1');
    ini_set('session.use_trans_sid', '0');
    ini_set('session.cookie_httponly', '1');
    ini_set('session.gc_maxlifetime', (string)APP_IDLE_TIMEOUT);
    ini_set('session.sid_length', '48');
    ini_set('session.sid_bits_per_character', '5');
    session_name('STOCKWEBSID');
    session_set_cookie_params([
        'lifetime' => 0,
        'path' => ($base === '' ? '/' : $base),
        'domain' => '',
        'secure' => app_is_https(),
        'httponly' => true,
        'samesite' => 'Strict',
    ]);
    session_start();
}

/** CSRF 토큰 (세션 단위). */
function csrf_token(): string
{
    if (empty($_SESSION['csrf'])) {
        $_SESSION['csrf'] = bin2hex(random_bytes(32));
    }
    return (string)$_SESSION['csrf'];
}

/** POST 의 CSRF 토큰 검증. */
function csrf_valid(): bool
{
    $sent = $_POST['csrf_token'] ?? '';
    if (!is_string($sent) || $sent === '' || empty($_SESSION['csrf'])) {
        return false;
    }
    return hash_equals((string)$_SESSION['csrf'], $sent);
}

/** hidden input HTML. */
function csrf_field(): string
{
    return '<input type="hidden" name="csrf_token" value="' . h(csrf_token()) . '">';
}

/** 요청 IP (프록시 경유 고려하되 신뢰 범위는 REMOTE_ADDR 우선). */
function client_ip(): string
{
    $ip = (string)($_SERVER['REMOTE_ADDR'] ?? '');
    return mb_substr($ip, 0, 45);
}

function client_ua(): string
{
    return mb_substr((string)($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 255);
}
