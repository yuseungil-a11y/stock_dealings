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

/**
 * 페이지네이션 바.
 * @param string $param 페이지 번호를 담는 쿼리 파라미터 이름(한 화면에 표가 둘 이상일 때 구분)
 */
function render_pager(array $result, string $param = 'page'): string
{
    $page = (int)$result['page'];
    $pages = (int)$result['pages'];
    $total = (int)$result['total'];
    $out = '<nav class="pager" aria-label="페이지 이동"><span class="pager-total">전체 '
        . h(number_format($total)) . '건</span>';
    if ($pages > 1) {
        $out .= '<span class="pager-links">';
        if ($page > 1) {
            $out .= '<a class="btn btn-sm" href="' . h(url_with([$param => 1])) . '">처음</a>'
                . '<a class="btn btn-sm" href="' . h(url_with([$param => $page - 1])) . '">이전</a>';
        }
        $start = max(1, $page - 2);
        $end = min($pages, $start + 4);
        $start = max(1, $end - 4);
        for ($i = $start; $i <= $end; $i++) {
            $cls = $i === $page ? 'btn btn-sm is-cur' : 'btn btn-sm';
            $out .= '<a class="' . $cls . '" href="' . h(url_with([$param => $i])) . '">' . $i . '</a>';
        }
        if ($page < $pages) {
            $out .= '<a class="btn btn-sm" href="' . h(url_with([$param => $page + 1])) . '">다음</a>'
                . '<a class="btn btn-sm" href="' . h(url_with([$param => $pages])) . '">끝</a>';
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

/** 숫자 범위 입력 한 칸(PER/PBR/ROE 등). 값은 clean_num() 을 통과한 뒤에만 바인딩된다. */
function filter_field_num(string $name, string $label, ?float $value, string $placeholder = '', string $step = 'any'): string
{
    return '<span class="fld fld-num"><label for="f_' . h($name) . '">' . h($label) . '</label>'
        . '<input type="number" step="' . h($step) . '" id="f_' . h($name) . '" name="' . h($name) . '" value="'
        . ($value === null ? '' : h(rtrim(rtrim(number_format($value, 4, '.', ''), '0'), '.')))
        . '" placeholder="' . h($placeholder) . '"></span>';
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

/**
 * 필터 폼 닫기(조회 · 초기화 버튼).
 * @param array<int,string> $extraReset 이 화면에서만 쓰는 추가 파라미터(초기화 시 함께 지운다)
 */
function filter_form_close(array $extraReset = []): string
{
    $reset = ['from' => null, 'to' => null, 'q' => null, 'page' => null,
        'level' => null, 'category' => null, 'side' => null, 'kind' => null, 'type' => null, 'algo' => null,
        'result' => null, 'api_id' => null, 'rc' => null,
        'region' => null, 'match' => null, 'signal' => null, 'trigger' => null, 'apage' => null];
    foreach ($extraReset as $k) {
        $reset[(string)$k] = null;
    }
    return '<span class="fld fld-btns"><button class="btn btn-primary btn-sm" type="submit">조회</button>'
        . '<a class="btn btn-sm" href="' . h(url_with($reset))
        . '">초기화</a></span></form>';
}

/** 상태 배지. $title 을 주면 마우스 오버 설명(툴팁)이 붙는다. */
function badge(string $text, string $kind = '', string $title = ''): string
{
    return '<span class="badge' . ($kind !== '' ? ' badge-' . h($kind) : '') . '"'
        . ($title !== '' ? ' title="' . h($title) . '"' : '') . '>' . h($text) . '</span>';
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

/* ---------------------------------------------- 산업 트렌드 스캔 (읽기 전용) */

/** 스캔 실행 상태 배지 (ok / partial / error). 부분 성공에는 원인 안내 툴팁을 붙인다. */
function trend_status_badge(string $status): string
{
    return badge(
        match ($status) { 'ok' => '정상', 'partial' => '부분 성공', 'error' => '오류', default => $status },
        match ($status) { 'ok' => 'ok', 'partial' => 'warn', 'error' => 'err', default => 'muted' },
        match ($status) {
            'partial' => '일부 단계가 실패했습니다 — 웹 검색 실패 가능성이 큽니다. '
                . '오류 메시지와 아래 조사 원문을 확인하세요.',
            'error' => '실행이 실패했습니다. 오류 메시지를 확인하세요.',
            default => '',
        }
    );
}

/** 스캔 실행 · 시도의 구분 배지 (예약 / 수동). */
function trend_trigger_badge(string $trigger): string
{
    return badge(
        match ($trigger) { 'scheduled' => '예약', 'manual' => '수동', default => ($trigger === '' ? '-' : $trigger) },
        match ($trigger) { 'scheduled' => 'muted', 'manual' => 'info', default => 'muted' },
        match ($trigger) {
            'scheduled' => '설정된 시각에 서버가 자동 실행했습니다.',
            'manual' => '웹에서 관리자가 "지금 다시 조사"를 눌러 실행됐습니다.',
            default => '',
        }
    );
}

/** 조사 지역 배지 (국내 / 해외). */
function trend_region_badge(string $region): string
{
    return badge(
        match ($region) { 'domestic' => '국내', 'global' => '해외', default => $region },
        match ($region) { 'domestic' => 'info', 'global' => 'muted', default => 'muted' }
    );
}

/** 매칭 방식 한글 라벨. */
function trend_match_label(string $match): string
{
    return match ($match) {
        'kiwoom_theme_member' => '키움 테마 구성종목',
        'name_matched' => '종목명 일치',
        'unmatched' => '미매칭',
        default => $match,
    };
}

function trend_match_badge(string $match): string
{
    return badge(trend_match_label($match), match ($match) {
        'kiwoom_theme_member' => 'ok',
        'name_matched' => 'info',
        'unmatched' => 'muted',
        default => 'muted',
    });
}

/** 종목을 찾지 못한 이유 안내(추측으로 종목을 채우지 않는다는 설계를 설명). */
function trend_unmatched_note(array $c): string
{
    if ((string)$c['match_status'] !== 'unmatched') {
        return '';
    }
    return ((string)($c['region'] ?? '') === 'global')
        ? '해외 테마여서 국내 상장 종목으로 직접 연결되지 않았습니다. '
            . '키움 테마(ka90001)·종목마스터에서 이름이 일치하는 종목을 찾지 못해 추측으로 채우지 않았습니다.'
        : '키움 테마그룹(ka90001)에 해당 테마가 없고 종목마스터에서도 이름이 일치하는 종목을 찾지 못했습니다. '
            . '추측으로 종목을 채우지 않으므로 매수 신호로도 이어지지 않습니다.';
}

/** 접이식 원문 블록 (긴 텍스트, 개행 유지 · 반드시 이스케이프). */
function trend_text_details(string $label, mixed $raw): string
{
    $s = (string)($raw ?? '');
    if (trim($s) === '') {
        return '';
    }
    return '<details class="jsum"><summary>' . h($label) . '</summary><pre>' . h($s) . '</pre></details>';
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

/* ------------------------------------- 기업 재무분석 (리서치, 읽기 전용) */

/**
 * 매매와 무관함을 명시하는 공통 면책 안내.
 * 고정 문구만 포함하며 사용자 입력이 섞이지 않는다.
 */
function research_disclaimer(): string
{
    return '<p class="note"><strong>참고용 리서치 화면입니다.</strong> '
        . '여기의 재무데이터(DART 공시)와 Claude 재무분석 리포트는 <strong>조회 · 연구 목적</strong>이며 '
        . '자동매매 알고리즘 · 주문 · 계좌와 전혀 연결되어 있지 않습니다. '
        . '<strong>이 리포트는 참고용이며 매매를 자동으로 실행하지 않습니다. '
        . '투자 판단의 책임은 본인에게 있습니다.</strong></p>';
}

/** 분석 리포트 상태 배지 (정상 / 오류). */
function fin_status_badge(string $status): string
{
    return badge(
        match ($status) { 'ok' => '정상', 'error' => '오류', default => ($status === '' ? '-' : $status) },
        match ($status) { 'ok' => 'ok', 'error' => 'err', default => 'muted' },
        $status === 'error' ? '리포트 생성이 실패한 기록입니다. 오류 메시지를 확인하세요.' : ''
    );
}

/** DART 보고서 구분 배지 (1분기 / 반기 / 3분기 / 사업보고서). */
function fin_reprt_badge(?string $code): string
{
    $c = (string)$code;
    return badge(fin_reprt_label($c), $c === '11011' ? 'info' : 'muted',
        $c === '' ? '' : 'DART 보고서코드 ' . $c);
}

/** 억원 단위 요약 표기(원 단위 금액이 너무 길어 표가 깨지는 것을 막는다). 값이 없으면 '-'. */
function fin_eok(mixed $v, int $dec = 0): string
{
    if ($v === null || $v === '' || !is_numeric($v)) {
        return '-';
    }
    return number_format((float)$v / 100000000, $dec);
}

/** 배수 표기(PER/PBR). 0 이하는 산출 불가로 보고 안내 문구를 붙인다. */
function fin_multiple(mixed $v, int $dec = 2): string
{
    if ($v === null || $v === '' || !is_numeric($v)) {
        return '-';
    }
    $f = (float)$v;
    return $f <= 0 ? '산출불가' : number_format($f, $dec);
}

/** 리포트 본문(개행 유지, 반드시 이스케이프). */
function fin_report_block(mixed $raw): string
{
    $s = (string)($raw ?? '');
    if (trim($s) === '') {
        return '<p class="tl-empty">리포트 본문이 비어 있습니다.</p>';
    }
    return '<pre class="reporttext">' . h($s) . '</pre>';
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
