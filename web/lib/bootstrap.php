<?php
declare(strict_types=1);
/**
 * 부트스트랩 — index.php / api.php 만 이 파일을 포함한다.
 * (두 파일이 STOCK_APP 상수를 정의하므로, lib 내 파일을 직접 호출하면 404 로 차단된다.)
 */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

define('APP_DIR', dirname(__DIR__));
define('APP_NAME', '주식 자동매매 관제');

/** 보안 정책 상수 */
const APP_IDLE_TIMEOUT = 1800;          // 유휴 30분
const APP_ABSOLUTE_TIMEOUT = 28800;     // 절대 8시간
const APP_MAX_FAILED = 5;               // 연속 실패 5회 -> 잠금
const APP_LOCK_MINUTES = 15;            // 잠금 15분
const APP_IP_WINDOW_MIN = 10;           // IP 제한 관찰 구간(분)
const APP_IP_MAX_ATTEMPTS = 20;         // 구간 내 최대 시도 횟수
const APP_LOGIN_MIN_MS = 350;           // 로그인 응답 최소 소요시간(ms) - 타이밍 차이 은닉
const APP_PW_MIN_LEN = 10;              // 비밀번호 최소 길이

/** 관제 하트비트 임계값(초) */
const APP_HEARTBEAT_WARN_SEC = 60;
const APP_HEARTBEAT_ERROR_SEC = 300;

date_default_timezone_set('Asia/Seoul');
mb_internal_encoding('UTF-8');

require __DIR__ . '/util.php';
require __DIR__ . '/version.php';
require __DIR__ . '/config.php';
require __DIR__ . '/security.php';
require __DIR__ . '/db.php';
require __DIR__ . '/auth.php';
require __DIR__ . '/repo.php';
require __DIR__ . '/view.php';

app_init_errors();
app_send_security_headers(defined('STOCK_JSON') && STOCK_JSON);
app_start_session();
