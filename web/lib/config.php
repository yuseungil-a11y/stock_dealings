<?php
declare(strict_types=1);
/**
 * 설정 로딩 · 경로 해석.
 * 비밀값(DB 비밀번호)은 이 파일이 읽어 PDO 에만 전달하고, 출력/로그로 내보내지 않는다.
 */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** 웹루트 밖 비공개 디렉터리(설정·로그). 없으면 개발용 소스 상대경로 fallback. */
function app_private_dir(): ?string
{
    static $dir = null;
    static $done = false;
    if ($done) {
        return $dir;
    }
    $done = true;

    $candidates = [];
    $env = getenv('STOCK_WEB_PRIVATE_DIR');
    if (is_string($env) && $env !== '') {
        $candidates[] = $env;
    }
    $candidates[] = 'D:/xampp/stock_private';
    $candidates[] = APP_DIR . '/config';

    foreach ($candidates as $cand) {
        $cand = rtrim(str_replace('\\', '/', $cand), '/');
        if ($cand !== '' && is_file($cand . '/config.local.php')) {
            $dir = $cand;
            break;
        }
    }
    return $dir;
}

/** 오류 로그 파일 경로 (항상 웹루트 밖 우선). */
function app_log_file(): string
{
    static $path = null;
    if ($path !== null) {
        return $path;
    }
    $base = app_private_dir();
    if ($base === null) {
        $base = sys_get_temp_dir();
    }
    $logDir = $base . '/logs';
    if (!is_dir($logDir)) {
        @mkdir($logDir, 0770, true);
    }
    $path = is_dir($logDir) ? $logDir . '/web_error.log' : $base . '/web_error.log';
    return $path;
}

/** 설정 배열 (db 접속정보 포함). 호출부는 필요한 키만 사용하고 값을 출력하지 않는다. */
function app_config(): array
{
    static $cfg = null;
    if ($cfg !== null) {
        return $cfg;
    }
    $dir = app_private_dir();
    if ($dir === null) {
        app_fatal('config_missing', '설정 파일을 찾을 수 없습니다.');
    }
    $loaded = require $dir . '/config.local.php';
    if (!is_array($loaded) || !isset($loaded['db']) || !is_array($loaded['db'])) {
        app_fatal('config_invalid', '설정 파일 형식이 올바르지 않습니다.');
    }
    $cfg = $loaded;
    return $cfg;
}

/** 애플리케이션 설정값(비밀 아님) 기본치. */
function app_option(string $key, mixed $default = null): mixed
{
    $cfg = app_config();
    return $cfg['app'][$key] ?? $default;
}
