<?php
declare(strict_types=1);
/**
 * 분석용 내보내기 (거래 분석 화면 전용, 읽기 전용).
 *
 *  * 세션 인증을 통과한 뒤 index.php 에서만 호출된다(단일 진입점 유지).
 *  * GET 전용 · 최대 ANALYSIS_EXPORT_MAX 행 · 계좌번호는 원문을 내보내지 않는다(뷰에 없음).
 *  * CSV 는 엑셀 호환을 위해 UTF-8 BOM 을 붙이고, 수식으로 해석될 수 있는 셀은 무력화한다.
 */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/** 내보내기 컬럼: 뷰 컬럼명 => CSV 헤더(한글). JSON 은 뷰 컬럼명을 그대로 쓴다. */
function export_analysis_fields(): array
{
    return [
        'signal_id' => '신호ID',
        'signal_time' => '신호시각',
        'algo_code' => '알고리즘',
        'signal_type' => '신호유형',
        'stk_cd' => '종목코드',
        'stk_nm' => '종목명',
        'score' => '점수',
        'signal_detail' => '신호사유',
        'order_id' => '주문ID',
        'ord_no' => '주문번호',
        'side' => '매매구분',
        'order_kind' => '주문종류',
        'order_status' => '주문상태',
        'is_dry_run' => '관찰만',
        'trde_tp' => '거래구분',
        'ord_qty' => '주문수량',
        'ord_uv' => '주문단가',
        'signal_price' => '신호가',
        'filled_qty' => '체결수량',
        'avg_fill_pric' => '평균체결가',
        'slippage_pct' => '슬리피지(%)',
        'return_code' => '응답코드',
        'return_msg' => '응답메시지',
        'reject_reason' => '거부사유',
        'order_reason' => '주문사유',
        'signal_context' => '신호맥락(JSON)',
        'params_snapshot' => '파라미터스냅샷(JSON)',
        'order_time' => '주문시각',
        'exec_cnt' => '체결건수',
        'exec_amount' => '체결금액',
        'exec_fee_tax' => '수수료·세금',
        'llm_model' => 'Claude모델',
        'llm_decision' => 'Claude판단',
        'llm_final' => 'Claude최종동작',
        'llm_confidence' => 'Claude확신도',
        'llm_reasons' => 'Claude근거',
    ];
}

/**
 * CSV 셀 정리.
 *  1) CSV 인젝션 방지: 셀이 = + - @ TAB CR 로 시작하면 앞에 작은따옴표를 붙여 수식 해석을 막는다.
 *     (순수 숫자 리터럴은 수식이 될 수 없으므로 분석 편의를 위해 예외로 둔다 — 음수 표기 보존)
 *  2) 줄바꿈·탭은 공백으로 정리해 "한 행 = 한 레코드" 를 유지한다.
 *  3) 항상 큰따옴표로 감싸고 내부 큰따옴표는 두 번 반복한다.
 */
function export_csv_cell(mixed $v): string
{
    if ($v === null) {
        return '""';
    }
    if (is_bool($v)) {
        $v = $v ? '1' : '0';
    }
    $s = (string)$v;
    if ($s !== '') {
        $first = substr($s, 0, 1);
        $isPlainNumber = (bool)preg_match('/^-?\d+(\.\d+)?$/', $s);
        if (!$isPlainNumber && strpos("=+-@\t\r", $first) !== false) {
            $s = "'" . $s;
        }
    }
    $s = str_replace(["\r\n", "\r", "\n", "\t"], ' ', $s);
    return '"' . str_replace('"', '""', $s) . '"';
}

/** 내려받기 공통 헤더(보안 헤더는 bootstrap 에서 이미 설정됨 — 여기서는 덮어쓰기만 한다). */
function export_send_headers(string $mime, string $filename): void
{
    if (headers_sent()) {
        return;
    }
    header('Content-Type: ' . $mime);
    // 파일명은 코드가 만든 ASCII 문자열만 사용한다(사용자 입력 없음).
    header('Content-Disposition: attachment; filename="' . $filename . '"');
    header('Cache-Control: no-store, no-cache, must-revalidate, private');
    header('Pragma: no-cache');
    header('Expires: 0');
    header('X-Content-Type-Options: nosniff');
}

function export_filename(string $ext): string
{
    return 'trade_analysis_' . date('Ymd_His') . '.' . $ext;
}

/**
 * 거래 분석 내보내기. 응답을 끝내고 종료한다.
 * @param string $format 'csv' | 'json'
 */
function export_trade_analysis(string $format, ?string $from, ?string $to, string $q, string $result): never
{
    $limit = ANALYSIS_EXPORT_MAX;
    $total = repo_trade_analysis_count($from, $to, $q, $result);
    $rows = repo_trade_analysis_export($from, $to, $q, $result, $limit);
    $truncated = $total > $limit;
    if ($truncated && !headers_sent()) {
        header('X-Export-Truncated: 1');
    }
    $fields = export_analysis_fields();

    if ($format === 'json') {
        // 주문 상태 타임라인도 함께 내보낸다(실패 원인 분석용). N+1 없이 한 번에 조회.
        $events = repo_order_events(repo_collect_order_ids($rows));
        $out = [];
        foreach ($rows as $r) {
            $row = [];
            foreach (array_keys($fields) as $k) {
                $row[$k] = $r[$k] ?? null;
            }
            $oid = ($r['order_id'] === null || $r['order_id'] === '') ? null : (int)$r['order_id'];
            $row['order_events'] = ($oid !== null && isset($events[$oid])) ? $events[$oid] : [];
            $out[] = $row;
        }
        export_send_headers('application/json; charset=utf-8', export_filename('json'));
        echo json_encode([
            'meta' => [
                'generated_at' => date('Y-m-d H:i:s'),
                'timezone' => 'Asia/Seoul',
                'source' => 'v_trade_analysis + order_event',
                'filter' => ['from' => $from, 'to' => $to, 'q' => $q, 'result' => ($result === '' ? 'all' : $result)],
                'total_matched' => $total,
                'exported' => count($out),
                'limit' => $limit,
                'truncated' => $truncated,
                'note' => $truncated
                    ? '조건에 맞는 행이 상한을 넘어 최신 ' . $limit . '건만 포함했습니다. 기간·종목 조건을 좁혀 다시 내려받으세요.'
                    : '계좌번호 등 민감정보는 포함하지 않습니다.',
            ],
            'rows' => $out,
        ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        exit;
    }

    export_send_headers('text/csv; charset=utf-8', export_filename('csv'));
    echo "\xEF\xBB\xBF"; // 엑셀용 UTF-8 BOM
    $head = [];
    foreach ($fields as $label) {
        $head[] = export_csv_cell($label);
    }
    echo implode(',', $head) . "\r\n";
    foreach ($rows as $r) {
        $line = [];
        foreach (array_keys($fields) as $k) {
            $line[] = export_csv_cell($r[$k] ?? null);
        }
        echo implode(',', $line) . "\r\n";
    }
    exit;
}
