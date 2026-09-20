<?php
declare(strict_types=1);
/** 화면 공통 조각 (페이지네이션 / 필터 / 배지). */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

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

/** 필터 폼 시작(GET). 현재 page key 를 유지한다. */
function filter_form_open(string $page): string
{
    return '<form class="filters" method="get" action="' . h(u('index.php')) . '">'
        . '<input type="hidden" name="p" value="' . h($page) . '">'
        . (isset($_GET['account_id']) ? '<input type="hidden" name="account_id" value="'
            . h(clean_int($_GET['account_id'], 0)) . '">' : '');
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
            'result' => null]))
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
