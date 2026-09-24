<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/**
 * 운영 웹 버전 — 이 파일이 웹 버전의 유일한 원천이다.
 * 직접 고치지 말고 `python tools/bump_version.py web patch|minor|major` 로 올린다(CHANGELOG.md 동시 갱신).
 * 형식: MAJOR.MINOR.PATCH (SemVer). 서버 버전과는 독립적으로 관리한다.
 */
const STOCK_WEB_VERSION = '1.5.5';
const STOCK_WEB_RELEASED = '2026-09-25';

/** 서버 하트비트 메시지("heartbeat 09:55:34 · v0.1.0")에서 서버 버전을 뽑는다. 없으면 null. */
function app_server_version(array $statusRows): ?string
{
    foreach ($statusRows as $row) {
        if (($row['component'] ?? '') !== 'server') {
            continue;
        }
        if (preg_match('/\bv(\d+\.\d+\.\d+)\b/', (string)($row['message'] ?? ''), $m) === 1) {
            return $m[1];
        }
    }
    return null;
}

/** 상단 우측 표시 문구: "웹 v0.1.0 · 서버 v0.1.0" (서버 버전을 모르면 "서버 -"). */
function app_version_text(array $statusRows): string
{
    $sv = app_server_version($statusRows);
    return '웹 v' . STOCK_WEB_VERSION . ' · 서버 ' . ($sv !== null ? 'v' . $sv : '-');
}
