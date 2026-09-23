<?php
declare(strict_types=1);
/** 공통 유틸: 경로/출력 이스케이프/포맷. */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** 출력 이스케이프 (모든 화면 출력은 이 함수를 통과). */
function h(mixed $v): string
{
    if ($v === null) {
        return '';
    }
    if (is_bool($v)) {
        return $v ? '1' : '0';
    }
    return htmlspecialchars((string)$v, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/** HTTPS 여부 (리버스 프록시 헤더도 고려). */
function app_is_https(): bool
{
    if (!empty($_SERVER['HTTPS']) && strtolower((string)$_SERVER['HTTPS']) !== 'off') {
        return true;
    }
    if (($_SERVER['SERVER_PORT'] ?? '') === '443') {
        return true;
    }
    $proto = $_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '';
    return is_string($proto) && strtolower(explode(',', $proto)[0]) === 'https';
}

/**
 * 앱 기준경로. `/stock` 하위에서도, 다른 경로로 옮겨도 그대로 동작하도록 자동 계산.
 * 절대경로 '/' 를 코드에 쓰지 않는다.
 */
function base_path(): string
{
    static $base = null;
    if ($base !== null) {
        return $base;
    }
    $script = (string)($_SERVER['SCRIPT_NAME'] ?? '/index.php');
    $dir = str_replace('\\', '/', dirname($script));
    $dir = rtrim($dir, '/');
    $base = ($dir === '' || $dir === '.') ? '' : $dir;
    return $base;
}

/** 앱 내부 URL 생성. */
function u(string $rel = ''): string
{
    $rel = ltrim($rel, '/');
    $base = base_path();
    return ($base === '' ? '' : $base) . '/' . $rel;
}

/**
 * 정적 자산(CSS/JS) URL — 배포마다 캐시가 안 비워져 예전 스타일이 계속 보이던 문제 방지.
 *
 * `app.css`/`app.js`는 캐시 헤더(`max-age=300`)가 있는데 URL이 매번 같아서, 배포해도
 * 브라우저가 새 파일을 가져오기까지 최대 5분(또는 브라우저가 재검증을 안 하면 더 오래)
 * 예전 파일을 계속 쓴다. `?v=<웹 버전>` 쿼리스트링을 붙이면 배포(버전 올림)마다 URL 자체가
 * 바뀌어 항상 새 파일을 받는다.
 */
function asset_url(string $rel): string
{
    return u($rel) . '?v=' . STOCK_WEB_VERSION;
}

/** 현재 페이지 URL 에 쿼리 파라미터를 덮어쓴 URL. */
function url_with(array $params, string $script = 'index.php'): string
{
    $q = $_GET;
    foreach ($params as $k => $v) {
        if ($v === null) {
            unset($q[$k]);
        } else {
            $q[$k] = $v;
        }
    }
    $qs = http_build_query($q);
    return u($script) . ($qs === '' ? '' : '?' . $qs);
}

/** 천단위 구분 숫자. null/빈값은 '-' */
function nfmt(mixed $v, int $dec = 0): string
{
    if ($v === null || $v === '') {
        return '-';
    }
    if (!is_numeric($v)) {
        return h((string)$v);
    }
    return number_format((float)$v, $dec);
}

/** 금액(원). */
function money(mixed $v): string
{
    return nfmt($v, 0);
}

/** 비율(%) 표기. */
function pct(mixed $v, int $dec = 2): string
{
    if ($v === null || $v === '') {
        return '-';
    }
    if (!is_numeric($v)) {
        return h((string)$v);
    }
    $f = (float)$v;
    return ($f > 0 ? '+' : '') . number_format($f, $dec) . '%';
}

/** 부호 색 클래스 (한국 관례: 상승 빨강 / 하락 파랑). */
function sign_class(mixed $v): string
{
    if ($v === null || $v === '' || !is_numeric($v)) {
        return 'v-flat';
    }
    $f = (float)$v;
    if ($f > 0) {
        return 'v-up';
    }
    if ($f < 0) {
        return 'v-down';
    }
    return 'v-flat';
}

/** 부호 포함 금액. */
function money_signed(mixed $v): string
{
    if ($v === null || $v === '' || !is_numeric($v)) {
        return '-';
    }
    $f = (float)$v;
    return ($f > 0 ? '+' : '') . number_format($f, 0);
}

/** 계좌번호 마스킹: 끝 4자리만 노출. */
function mask_account(?string $no): string
{
    $no = (string)$no;
    if ($no === '') {
        return '-';
    }
    $len = strlen($no);
    if ($len <= 4) {
        return str_repeat('*', max(0, $len - 1)) . substr($no, -1);
    }
    return str_repeat('*', $len - 4) . substr($no, -4);
}

/** KST 표시 시각. */
function kst(mixed $dt, string $fmt = 'Y-m-d H:i:s'): string
{
    if ($dt === null || $dt === '' || $dt === '0000-00-00 00:00:00') {
        return '-';
    }
    $ts = strtotime((string)$dt);
    if ($ts === false) {
        return h((string)$dt);
    }
    return date($fmt, $ts);
}

/** 상대 경과시간(초). */
function seconds_since(mixed $dt): ?int
{
    if ($dt === null || $dt === '') {
        return null;
    }
    $ts = strtotime((string)$dt);
    if ($ts === false) {
        return null;
    }
    return time() - $ts;
}

/** 사람이 읽는 경과시간. */
function ago(mixed $dt): string
{
    $s = seconds_since($dt);
    if ($s === null) {
        return '-';
    }
    if ($s < 0) {
        $s = 0;
    }
    if ($s < 60) {
        return $s . '초 전';
    }
    if ($s < 3600) {
        return intdiv($s, 60) . '분 전';
    }
    if ($s < 86400) {
        return intdiv($s, 3600) . '시간 전';
    }
    return intdiv($s, 86400) . '일 전';
}

/** 날짜 입력 검증 (YYYY-MM-DD). 잘못되면 null. */
function clean_date(mixed $v): ?string
{
    if (!is_string($v) || !preg_match('/^\d{4}-\d{2}-\d{2}$/', $v)) {
        return null;
    }
    [$y, $m, $d] = array_map('intval', explode('-', $v));
    return checkdate($m, $d, $y) ? $v : null;
}

/** 자유 입력 문자열 정리(길이 제한). 값은 항상 prepared statement 바인딩으로만 사용. */
function clean_text(mixed $v, int $max = 60): string
{
    if (!is_string($v)) {
        return '';
    }
    $v = trim($v);
    if ($v === '') {
        return '';
    }
    $v = preg_replace('/[\x00-\x1F\x7F]/u', '', $v) ?? '';
    return mb_substr($v, 0, $max, 'UTF-8');
}

/** 정수 파라미터. */
function clean_int(mixed $v, int $default = 0, ?int $min = null, ?int $max = null): int
{
    if (!is_scalar($v) || !is_numeric((string)$v)) {
        $n = $default;
    } else {
        $n = (int)$v;
    }
    if ($min !== null && $n < $min) {
        $n = $min;
    }
    if ($max !== null && $n > $max) {
        $n = $max;
    }
    return $n;
}

/**
 * 실수 파라미터(범위 필터용). 비었거나 숫자가 아니면 null.
 * 반환값은 항상 prepared statement 바인딩으로만 쓰인다.
 */
function clean_num(mixed $v, ?float $min = null, ?float $max = null): ?float
{
    if (is_string($v)) {
        $v = trim($v);
    }
    if ($v === null || $v === '' || !is_scalar($v) || !is_numeric((string)$v)) {
        return null;
    }
    $f = (float)$v;
    if (!is_finite($f)) {
        return null;
    }
    if ($min !== null && $f < $min) {
        $f = $min;
    }
    if ($max !== null && $f > $max) {
        $f = $max;
    }
    return $f;
}

/** 화이트리스트 선택값. */
function clean_enum(mixed $v, array $allowed, string $default): string
{
    return (is_string($v) && in_array($v, $allowed, true)) ? $v : $default;
}

/** 치명적 오류: 상세는 로그로, 화면에는 일반 문구만. */
function app_fatal(string $code, string $userMessage, ?string $detail = null): never
{
    @error_log('[stock-web] ' . $code . ($detail !== null ? ' :: ' . $detail : ''));
    if (!headers_sent()) {
        http_response_code(500);
        header('Cache-Control: no-store');
    }
    if (defined('STOCK_JSON') && STOCK_JSON) {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['ok' => false, 'error' => $userMessage], JSON_UNESCAPED_UNICODE);
    } else {
        header('Content-Type: text/html; charset=utf-8');
        echo '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            . '<meta name="viewport" content="width=device-width, initial-scale=1">'
            . '<title>오류</title></head><body><h1>일시적인 오류가 발생했습니다.</h1><p>'
            . h($userMessage) . '</p></body></html>';
    }
    exit;
}

/** 서버 상태 색 클래스. */
function status_class(?string $s): string
{
    return match ($s) {
        'ok' => 'st-ok',
        'warn' => 'st-warn',
        'error' => 'st-error',
        default => 'st-unknown',
    };
}

function status_label(?string $s): string
{
    return match ($s) {
        'ok' => '정상',
        'warn' => '주의',
        'error' => '오류',
        default => '미확인',
    };
}

/**
 * 서버 사이드 SVG 라인 차트 (외부 라이브러리·인라인 스크립트 없음).
 * @param array<int,array{label:string,value:float}> $points
 * @param int $dec y축 라벨·값 표시 소수 자릿수(PER/PBR 처럼 소수가 의미 있는 지표에서 사용)
 */
function svg_line_chart(array $points, string $title = '', int $w = 720, int $h = 220, int $dec = 0): string
{
    $dec = max(0, min($dec, 4));
    $n = count($points);
    if ($n === 0) {
        return '<p class="empty">표시할 데이터가 없습니다.</p>';
    }
    $padL = 68;
    $padR = 16;
    $padT = 18;
    $padB = 34;
    $iw = max(1, $w - $padL - $padR);
    $ih = max(1, $h - $padT - $padB);

    $vals = array_map(static fn($p) => (float)$p['value'], $points);
    $min = min($vals);
    $max = max($vals);
    if ($max === $min) {
        $max = $min + max(1.0, abs($min) * 0.1);
        $min = $min - max(1.0, abs($min) * 0.1);
    }
    $span = $max - $min;

    $xOf = static function (int $i) use ($n, $padL, $iw): float {
        return $n === 1 ? $padL + $iw / 2 : $padL + $iw * $i / ($n - 1);
    };
    $yOf = static function (float $v) use ($min, $span, $padT, $ih): float {
        return $padT + $ih * (1 - ($v - $min) / $span);
    };

    $pts = [];
    foreach ($points as $i => $p) {
        $pts[] = round($xOf($i), 1) . ',' . round($yOf((float)$p['value']), 1);
    }

    $svg = '<svg class="chart" viewBox="0 0 ' . $w . ' ' . $h . '" role="img" preserveAspectRatio="none"'
        . ' aria-label="' . h($title !== '' ? $title : '추이 차트') . '">';
    // 가로 그리드 + y축 라벨
    for ($g = 0; $g <= 4; $g++) {
        $vy = $min + $span * $g / 4;
        $y = round($yOf($vy), 1);
        $svg .= '<line class="grid" x1="' . $padL . '" y1="' . $y . '" x2="' . ($w - $padR) . '" y2="' . $y . '"/>';
        $svg .= '<text class="axis" x="' . ($padL - 6) . '" y="' . ($y + 4) . '" text-anchor="end">'
            . h(number_format($vy, $dec)) . '</text>';
    }
    // x축 라벨(처음/중간/끝)
    $labelIdx = $n === 1 ? [0] : array_unique([0, intdiv($n - 1, 2), $n - 1]);
    foreach ($labelIdx as $i) {
        $x = round($xOf((int)$i), 1);
        $anchor = $i === 0 ? 'start' : ($i === $n - 1 ? 'end' : 'middle');
        $svg .= '<text class="axis" x="' . $x . '" y="' . ($h - 12) . '" text-anchor="' . $anchor . '">'
            . h((string)$points[$i]['label']) . '</text>';
    }
    $svg .= '<polyline class="series" points="' . implode(' ', $pts) . '"/>';
    foreach ($points as $i => $p) {
        $svg .= '<circle class="dot" cx="' . round($xOf($i), 1) . '" cy="' . round($yOf((float)$p['value']), 1)
            . '" r="2.5"><title>' . h($p['label'] . ' : ' . number_format((float)$p['value'], $dec)) . '</title></circle>';
    }
    $svg .= '</svg>';
    return $svg;
}
