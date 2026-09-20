<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 거래 → 거래 분석 (읽기 전용).
 * v_trade_analysis(신호 → 주문 → 체결 → Claude 판단)를 한 화면에서 보고,
 * 같은 조건 그대로 CSV/JSON 으로 내려받아 Claude 에 올려 분석할 수 있게 한다.
 */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$result = clean_enum($_GET['result'] ?? '', ANALYSIS_RESULTS, '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);

$available = repo_can_read('v_trade_analysis');
$oeAvailable = repo_can_read('order_event');
$res = repo_trade_analysis($from, $to, $q, $result, $page);
$sum = repo_trade_analysis_summary($from, $to, $q, $result);
$events = $oeAvailable ? repo_order_events(repo_collect_order_ids($res['rows'])) : [];
$hasFilter = ($from !== null || $to !== null || $q !== '' || $result !== '');
$overLimit = $sum['signals'] > ANALYSIS_EXPORT_MAX;

$tradeTp = ['0' => '보통(지정가)', '3' => '시장가', '5' => '조건부지정가', '6' => '최유리지정가', '7' => '최우선지정가'];

$prompts = [
    "첨부한 자동매매 거래 분석 파일(trade_analysis)을 읽고, 주문이 실패하거나 거부된 건을 모두 찾아 "
    . "거부사유·응답코드·주문 상태 타임라인을 근거로 원인을 유형별로 분류해 주세요. "
    . "유형별 건수와 재발을 막기 위한 점검 항목을 표로 정리해 주세요.",

    "첨부한 파일에서 신호가와 평균체결가의 차이(슬리피지)를 알고리즘별·종목별·매매구분별로 집계해 주세요. "
    . "슬리피지가 큰 상위 사례는 주문 시각·거래구분(시장가/지정가)·주문 상태 타임라인과 함께 원인을 추정하고, "
    . "지정가 전환이나 분할 주문이 유리한 구간이 있는지 알려 주세요.",

    "첨부한 파일에서 signal_type 이 BLOCK 인 건과 Claude 판단이 block 인 건을 모아, "
    . "차단 근거(신호사유·신호맥락·파라미터 스냅샷)가 타당했는지 평가해 주세요. "
    . "차단하지 않았다면 이익이었을 가능성이 있는 건과, 반대로 차단이 손실을 막은 건을 나누어 설명해 주세요.",
];
?>
<?php if (!$available): ?>
  <?= empty_note('거래 분석 뷰(v_trade_analysis)를 읽을 수 없습니다.',
      '데이터베이스에 해당 뷰가 없거나 조회 권한이 없습니다. 서버 모듈이 스키마를 적용하면 표시됩니다.') ?>
<?php else: ?>

<section class="cards cards-wide">
  <div class="card"><h2 class="card-t">신호 수</h2>
    <p class="card-v"><?= h(nfmt($sum['signals'])) ?><span class="unit">건</span></p>
    <p class="card-s">조회 조건 기준</p></div>
  <div class="card"><h2 class="card-t">주문 수</h2>
    <p class="card-v"><?= h(nfmt($sum['orders'])) ?><span class="unit">건</span></p>
    <p class="card-s">관찰만(기록) 제외</p></div>
  <div class="card"><h2 class="card-t">체결 수</h2>
    <p class="card-v"><?= h(nfmt($sum['filled'])) ?><span class="unit">건</span></p>
    <p class="card-s">주문상태 FILLED</p></div>
  <div class="card"><h2 class="card-t">실패 · 거부</h2>
    <p class="card-v <?= $sum['failed'] > 0 ? 'v-up' : '' ?>"><?= h(nfmt($sum['failed'])) ?><span class="unit">건</span></p>
    <p class="card-s">FAILED · REJECTED</p></div>
  <div class="card"><h2 class="card-t">차단</h2>
    <p class="card-v"><?= h(nfmt($sum['blocked'])) ?><span class="unit">건</span></p>
    <p class="card-s">신호유형 BLOCK</p></div>
  <div class="card"><h2 class="card-t">평균 슬리피지</h2>
    <p class="card-v <?= h(sign_class($sum['avg_slippage'])) ?>"><?= $sum['avg_slippage'] === null ? '-' : h(pct($sum['avg_slippage'], 3)) ?></p>
    <p class="card-s">신호가 대비 평균체결가</p></div>
  <div class="card"><h2 class="card-t">수수료 · 세금</h2>
    <p class="card-v"><?= h(money($sum['fee_tax'])) ?></p>
    <p class="card-s">체결분 합계</p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2>거래 분석</h2></div>
  <p class="note">알고리즘 <strong>신호</strong> → <strong>주문</strong> → <strong>체결</strong> → <strong>Claude 판단</strong>을
    한 줄로 모아 보여줍니다. 각 행의 <strong>상세</strong>를 펼치면 신호 사유 전문, 신호 맥락·파라미터 스냅샷(JSON),
    Claude 근거, 주문 상태 타임라인을 볼 수 있습니다. 이 화면은 조회 전용이며 계좌 구분 없이 전체 신호를 대상으로 합니다.</p>

  <?= filter_form_open('trade.analysis') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
    <?= filter_field_select('result', '결과 유형', analysis_result_options(), $result) ?>
  <?= filter_form_close() ?>

  <div class="exportbox">
    <div class="exportbox-h">
      <h3>Claude 분석 안내</h3>
      <span class="exportbox-btns">
        <a class="btn btn-sm btn-primary" href="<?= h(url_with(['export' => 'csv', 'page' => null])) ?>">CSV 내려받기</a>
        <a class="btn btn-sm" href="<?= h(url_with(['export' => 'json', 'page' => null])) ?>">JSON 내려받기</a>
      </span>
    </div>
    <p class="exportbox-s">지금 화면의 <strong>필터 조건 그대로</strong> 내려받습니다.
      CSV 는 엑셀용(UTF-8 BOM), JSON 은 주문 상태 타임라인까지 포함합니다.
      최대 <?= h(nfmt(ANALYSIS_EXPORT_MAX)) ?>행이며 계좌번호는 포함되지 않습니다.</p>
    <?php if ($overLimit): ?>
      <p class="alert alert-warn">현재 조건에 <?= h(nfmt($sum['signals'])) ?>건이 있어 상한(<?= h(nfmt(ANALYSIS_EXPORT_MAX)) ?>행)을 넘습니다.
        내려받으면 <strong>최신 <?= h(nfmt(ANALYSIS_EXPORT_MAX)) ?>건만</strong> 포함되므로 기간·종목 조건을 좁혀 나누어 받으세요.</p>
    <?php endif; ?>
    <p class="exportbox-s">내려받은 파일을 Claude 대화창에 올리고 아래 질문 중 하나를 붙여 넣으세요.</p>
    <?php foreach ($prompts as $pmt): ?>
      <pre class="prompt"><?= h($pmt) ?></pre>
    <?php endforeach; ?>
  </div>

  <?php if ($res['rows'] === []): ?>
    <?= $hasFilter
        ? empty_note('검색 조건에 맞는 기록이 없습니다.', '기간 · 종목 · 결과 유형 조건을 바꾸어 다시 조회해 보세요.')
        : empty_note('아직 분석할 거래 기록이 없습니다.',
            '서버 모듈이 신호를 내고 주문을 기록하면 이곳에 신호 → 주문 → 체결 흐름이 모입니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>신호시각</th><th>알고리즘</th><th>종목</th><th>구분</th><th>신호유형</th><th class="num">점수</th>
        <th>주문상태</th><th class="num">수량(주문/체결)</th><th class="num">신호가 → 평균체결가</th>
        <th class="num">슬리피지</th><th>응답코드 · 메시지</th><th>거부사유</th><th>Claude 판단</th><th>상세</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <?php
        $status = (string)($r['order_status'] ?? '');
        $dry = (int)($r['is_dry_run'] ?? 0) === 1 || $status === 'SIGNAL_ONLY';
        $oid = ($r['order_id'] === null || $r['order_id'] === '') ? null : (int)$r['order_id'];
        $rowEvents = ($oid !== null && isset($events[$oid])) ? $events[$oid] : [];
        $rowCls = in_array($status, ['FAILED', 'REJECTED'], true) ? 'lv-error'
            : ((string)$r['signal_type'] === 'BLOCK' ? 'lv-warn' : ($dry ? 'row-dry' : ''));
        ?>
        <tr class="<?= h($rowCls) ?>">
          <td><?= h(kst($r['signal_time'])) ?>
            <?php if ($r['order_time'] !== null): ?><span class="sub">주문 <?= h(kst($r['order_time'], 'H:i:s')) ?></span><?php endif; ?></td>
          <td class="mono"><?= h($r['algo_code']) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td><?= $r['side'] === null ? '<span class="muted">-</span>' : side_badge((string)$r['side']) ?></td>
          <td><?= signal_type_badge($r['signal_type'] === null ? null : (string)$r['signal_type']) ?></td>
          <td class="num"><?= $r['score'] === null ? '-' : h(nfmt($r['score'], 2)) ?></td>
          <td><?php if ($status === ''): ?><span class="muted">주문 없음</span>
              <?php else: ?><?= order_status_badge(['status' => $status, 'is_dry_run' => (int)($r['is_dry_run'] ?? 0)]) ?><?php endif; ?></td>
          <td class="num"><?= h(nfmt($r['ord_qty'])) ?> / <?= h(nfmt($r['filled_qty'])) ?></td>
          <td class="num"><?= h(money($r['signal_price'])) ?> → <?= h(money($r['avg_fill_pric'])) ?></td>
          <td class="num <?= h(sign_class($r['slippage_pct'])) ?>"><?= $r['slippage_pct'] === null ? '-' : h(pct($r['slippage_pct'], 3)) ?></td>
          <td class="wrap-td"><?php if ($r['return_code'] !== null || ($r['return_msg'] ?? '') !== ''): ?>
              <span class="mono"><?= h($r['return_code'] ?? '-') ?></span> <?= h($r['return_msg']) ?>
            <?php else: ?><span class="muted">-</span><?php endif; ?></td>
          <td class="wrap-td"><?= ($r['reject_reason'] ?? '') !== '' ? h($r['reject_reason']) : '<span class="muted">-</span>' ?></td>
          <td><?php if (($r['llm_decision'] ?? '') !== ''): ?>
              <?= llm_decision_badge((string)$r['llm_decision']) ?>
              <?= ($r['llm_final'] ?? '') !== '' ? llm_action_badge((string)$r['llm_final']) : '' ?>
              <span class="sub"><?= $r['llm_confidence'] === null ? '' : h(nfmt($r['llm_confidence']) . '%') ?></span>
            <?php else: ?><span class="muted">-</span><?php endif; ?></td>
          <td class="wrap-td">
            <details class="rowdet"><summary>상세</summary>
              <div class="rowdet-body">
                <?php if (($r['signal_detail'] ?? '') !== ''): ?>
                  <p class="rd-k">신호 사유</p><p class="rd-v"><?= h($r['signal_detail']) ?></p>
                <?php endif; ?>
                <?php if (($r['order_reason'] ?? '') !== ''): ?>
                  <p class="rd-k">주문 사유</p><p class="rd-v"><?= h($r['order_reason']) ?></p>
                <?php endif; ?>
                <?php if (($r['llm_reasons'] ?? '') !== ''): ?>
                  <p class="rd-k">Claude 근거<?= ($r['llm_model'] ?? '') !== '' ? ' (' . h($r['llm_model']) . ')' : '' ?></p>
                  <p class="rd-v"><?= h($r['llm_reasons']) ?></p>
                <?php endif; ?>
                <?php if ($status !== '' || $r['trde_tp'] !== null): ?>
                  <p class="rd-k">주문 정보</p>
                  <p class="rd-v">주문번호 <span class="mono"><?= h($r['ord_no'] ?? '-') ?></span>
                    · 거래구분 <?= h($tradeTp[(string)$r['trde_tp']] ?? ($r['trde_tp'] ?? '-')) ?>
                    · 주문단가 <?= $r['ord_uv'] === null ? '시장가' : h(money($r['ord_uv'])) ?>
                    · 체결 <?= h(nfmt($r['exec_cnt'])) ?>건 / <?= h(money($r['exec_amount'])) ?>원
                    · 수수료·세금 <?= h(money($r['exec_fee_tax'])) ?>원</p>
                <?php endif; ?>
                <?= json_details('신호 맥락 (signal_context)', $r['signal_context']) ?>
                <?= json_details('파라미터 스냅샷 (params_snapshot)', $r['params_snapshot']) ?>
                <p class="rd-k">주문 상태 타임라인</p>
                <?php if ($oid === null): ?>
                  <p class="tl-empty">연결된 주문이 없습니다(신호만 기록).</p>
                <?php else: ?>
                  <?= order_timeline_html($rowEvents, $oeAvailable) ?>
                <?php endif; ?>
              </div>
            </details>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
<?php endif; ?>
