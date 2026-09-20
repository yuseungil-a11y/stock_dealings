<?php
declare(strict_types=1);
/** 화면 공통 조각 (페이지네이션 / 필터 / 배지). */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** 파비콘 <link> 태그 묶음(서버 프로그램과 같은 주식 상승 아이콘). tools/make_icon.py 로 생성. */
function favicon_links(): string
{
    return '<link rel="icon" href="' . h(u('assets/img/favicon.ico')) . '" sizes="any">'
        . '<link rel="icon" href="' . h(u('assets/img/favicon-32.png')) . '" type="image/png" sizes="32x32">'
        . '<link rel="apple-touch-icon" href="' . h(u('assets/img/apple-touch-icon.png')) . '">';
}

/** 페이지네이션 바. */
function render_pager(array $result): string
{
    $page = (int)$result['page'];
    $pages = (int)$result['pages'];
    $total = (int)$result['total'];
    $out = '<nav class="pager" aria-label="페이지 이동"><span class="pager-total">전체 '
        . h(number_format($total)) . '건</span>';
    if ($pages > 1) {
        $out .= '<span class="pager-links">';
        if ($page > 1) {
            $out .= '<a class="btn btn-sm" href="' . h(url_with(['page' => 1])) . '">처음</a>'
                . '<a class="btn btn-sm" href="' . h(url_with(['page' => $page - 1])) . '">이전</a>';
        }
        $start = max(1, $page - 2);
        $end = min($pages, $start + 4);
        $start = max(1, $end - 4);
        for ($i = $start; $i <= $end; $i++) {
            $cls = $i === $page ? 'btn btn-sm is-cur' : 'btn btn-sm';
            $out .= '<a class="' . $cls . '" href="' . h(url_with(['page' => $i])) . '">' . $i . '</a>';
        }
        if ($page < $pages) {
            $out .= '<a class="btn btn-sm" href="' . h(url_with(['page' => $page + 1])) . '">다음</a>'
                . '<a class="btn btn-sm" href="' . h(url_with(['page' => $pages])) . '">끝</a>';
        }
        $out .= '</span>';
    }
    $out .= '<span class="pager-pos">' . $page . ' / ' . max(1, $pages) . ' 페이지</span></nav>';
    return $out;
}

/** 데이터 없음 안내. */
function empty_note(string $msg = '표시할 데이터가 없습니다.', string $hint = ''): string
{
    return '<div class="empty"><p>' . h($msg) . '</p>'
        . ($hint !== '' ? '<p class="empty-hint">' . h($hint) . '</p>' : '') . '</div>';
}

/**
 * 필터 폼 시작(GET). 현재 page key 를 유지한다.
 * @param array<string,string> $hidden 함께 유지할 추가 hidden 값(탭 등)
 */
function filter_form_open(string $page, array $hidden = []): string
{
    $out = '<form class="filters" method="get" action="' . h(u('index.php')) . '">'
        . '<input type="hidden" name="p" value="' . h($page) . '">'
        . (isset($_GET['account_id']) ? '<input type="hidden" name="account_id" value="'
            . h(clean_int($_GET['account_id'], 0)) . '">' : '');
    foreach ($hidden as $k => $v) {
        $out .= '<input type="hidden" name="' . h($k) . '" value="' . h($v) . '">';
    }
    return $out;
}

function filter_field_date(string $name, string $label, ?string $value): string
{
    return '<span class="fld"><label for="f_' . h($name) . '">' . h($label) . '</label>'
        . '<input type="date" id="f_' . h($name) . '" name="' . h($name) . '" value="' . h($value ?? '') . '"></span>';
}

function filter_field_text(string $name, string $label, string $value, string $placeholder = ''): string
{
    return '<span class="fld"><label for="f_' . h($name) . '">' . h($label) . '</label>'
        . '<input type="search" id="f_' . h($name) . '" name="' . h($name) . '" maxlength="60" value="'
        . h($value) . '" placeholder="' . h($placeholder) . '"></span>';
}

/** @param array<string,string> $options value => label */
function filter_field_select(string $name, string $label, array $options, string $value): string
{
    $out = '<span class="fld"><label for="f_' . h($name) . '">' . h($label) . '</label>'
        . '<select id="f_' . h($name) . '" name="' . h($name) . '">';
    foreach ($options as $v => $l) {
        $out .= '<option value="' . h($v) . '"' . ((string)$v === $value ? ' selected' : '') . '>' . h($l) . '</option>';
    }
    return $out . '</select></span>';
}

function filter_form_close(): string
{
    return '<span class="fld fld-btns"><button class="btn btn-primary btn-sm" type="submit">조회</button>'
        . '<a class="btn btn-sm" href="' . h(url_with(['from' => null, 'to' => null, 'q' => null, 'page' => null,
            'level' => null, 'category' => null, 'side' => null, 'kind' => null, 'type' => null, 'algo' => null,
            'result' => null, 'api_id' => null, 'rc' => null]))
        . '">초기화</a></span></form>';
}

/** 상태 배지. */
function badge(string $text, string $kind = ''): string
{
    return '<span class="badge' . ($kind !== '' ? ' badge-' . h($kind) : '') . '">' . h($text) . '</span>';
}

function order_status_badge(array $o): string
{
    $status = (string)$o['status'];
    $dry = (int)$o['is_dry_run'] === 1 || $status === 'SIGNAL_ONLY';
    $kind = match ($status) {
        'FILLED' => 'ok',
        'PARTIAL', 'ACCEPTED', 'SENT' => 'info',
        'CANCELED' => 'muted',
        'REJECTED', 'FAILED' => 'err',
        'SIGNAL_ONLY' => 'warn',
        default => '',
    };
    $out = badge($status, $kind);
    if ($dry) {
        $out .= ' ' . badge('신호만 기록', 'warn');
    }
    return $out;
}

function level_badge(string $level): string
{
    $kind = match ($level) {
        'ERROR' => 'err',
        'WARN' => 'warn',
        'INFO' => 'info',
        default => 'muted',
    };
    return badge($level, $kind);
}

function side_badge(string $side): string
{
    return badge($side === 'BUY' ? '매수' : '매도', $side === 'BUY' ? 'buy' : 'sell');
}

/** Claude 판단 배지 (허용=녹 / 차단=적 / 오류=황). */
function llm_decision_badge(string $decision): string
{
    return badge(
        match ($decision) { 'allow' => '허용', 'block' => '차단', 'error' => '오류', default => $decision },
        match ($decision) { 'allow' => 'ok', 'block' => 'err', 'error' => 'warn', default => 'muted' }
    );
}

/** 파이프라인 최종 동작 배지 (통과=녹 / 차단=적). */
function llm_action_badge(string $action): string
{
    return badge($action === 'block' ? '차단' : '통과', $action === 'block' ? 'err' : 'ok');
}

/** 상장폐지·거래불가 종목 배지. */
function delisted_badge(): string
{
    return badge('상장폐지', 'delist');
}

/** 신호 유형 배지 (BUY/SELL/HOLD/BLOCK). */
function signal_type_badge(?string $type): string
{
    $t = (string)$type;
    return badge(
        match ($t) { 'BUY' => '매수신호', 'SELL' => '매도신호', 'HOLD' => '관망', 'BLOCK' => '차단', default => ($t === '' ? '-' : $t) },
        match ($t) { 'BUY' => 'buy', 'SELL' => 'sell', 'HOLD' => 'muted', 'BLOCK' => 'err', default => 'muted' }
    );
}

/**
 * JSON 문자열을 보기 좋게 들여쓴다. 파싱에 실패하면 원문을 그대로 돌려준다.
 * 반환값은 원시 텍스트이므로 출력할 때 반드시 h() 로 이스케이프한다.
 */
function json_pretty_text(mixed $raw): string
{
    if ($raw === null) {
        return '';
    }
    $s = (string)$raw;
    if (trim($s) === '') {
        return '';
    }
    $decoded = json_decode($s, true);
    if (json_last_error() === JSON_ERROR_NONE && (is_array($decoded) || is_object($decoded))) {
        $pretty = json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if (is_string($pretty)) {
            return $pretty;
        }
    }
    return $s;
}

/** 접이식 JSON 블록 (JS 없이 <details> 만 사용, 값은 모두 이스케이프). */
function json_details(string $label, mixed $raw): string
{
    $text = json_pretty_text($raw);
    if ($text === '') {
        return '';
    }
    return '<details class="jsum"><summary>' . h($label) . '</summary><pre>' . h($text) . '</pre></details>';
}

/** 주문 이벤트 유형 배지. */
function order_event_badge(string $type): string
{
    $kind = match ($type) {
        'FILLED' => 'ok',
        'PARTIAL', 'ACCEPTED', 'SENT', 'CREATED', 'MODIFIED' => 'info',
        'CANCELED', 'NOTE', 'UNKNOWN' => 'muted',
        'REJECTED', 'FAILED' => 'err',
        default => 'muted',
    };
    return badge($type, $kind);
}

/**
 * 주문 상태 타임라인(order_event) HTML.
 * @param array $events 시간순 이벤트 목록 (repo_order_events 결과)
 */
function order_timeline_html(array $events, bool $available = true): string
{
    if (!$available) {
        return '<p class="tl-empty">주문 이벤트 표(order_event)를 읽을 수 없습니다.</p>';
    }
    if ($events === []) {
        return '<p class="tl-empty">기록된 주문 상태 변화가 없습니다.</p>';
    }
    $out = '<ol class="timeline">';
    foreach ($events as $e) {
        $bits = [];
        if ($e['status'] !== null && $e['status'] !== '') {
            $bits[] = '상태 ' . (string)$e['status'];
        }
        if ($e['filled_qty'] !== null) {
            $bits[] = '체결 ' . nfmt($e['filled_qty']);
        }
        if ($e['remain_qty'] !== null) {
            $bits[] = '잔량 ' . nfmt($e['remain_qty']);
        }
        if ($e['price'] !== null) {
            $bits[] = '가격 ' . money($e['price']);
        }
        if ($e['return_code'] !== null) {
            $bits[] = '응답 ' . (string)$e['return_code'];
        }
        $out .= '<li><span class="tl-time">' . h(kst($e['event_time'])) . '</span>'
            . order_event_badge((string)$e['event_type'])
            . '<span class="tl-src">' . h($e['source']) . '</span>'
            . '<span class="tl-meta">' . h(implode(' · ', $bits)) . '</span>';
        if (($e['reject_reason'] ?? '') !== '') {
            $out .= '<span class="tl-reject">거부사유: ' . h($e['reject_reason']) . '</span>';
        }
        if (($e['message'] ?? '') !== '') {
            $out .= '<span class="tl-msg">' . h($e['message']) . '</span>';
        }
        $out .= '</li>';
    }
    return $out . '</ol>';
}

/**
 * 슬리피지(%) 계산: (평균체결가 - 신호가) / 신호가 × 100.
 * 두 값이 모두 양수일 때만 계산하고 그 외에는 null.
 */
function slippage_pct(mixed $signalPrice, mixed $avgFill): ?float
{
    if (!is_numeric($signalPrice) || !is_numeric($avgFill)) {
        return null;
    }
    $sp = (float)$signalPrice;
    $af = (float)$avgFill;
    if ($sp <= 0 || $af <= 0) {
        return null;
    }
    return ($af - $sp) / $sp * 100;
}

/** 탭 링크 묶음 (JS 없이 GET 링크). @param array<string,string> $tabs key => label */
function tab_links(array $tabs, string $current, string $param = 'tab'): string
{
    $out = '<nav class="tabs" aria-label="보기 선택">';
    foreach ($tabs as $k => $label) {
        $cls = $k === $current ? 'tab is-cur' : 'tab';
        $out .= '<a class="' . $cls . '" href="' . h(url_with([$param => $k, 'page' => null])) . '">'
            . h($label) . '</a>';
    }
    return $out . '</nav>';
}
