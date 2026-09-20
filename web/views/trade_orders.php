<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$kind = clean_enum($_GET['kind'] ?? '', ['signal', 'sent'], '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = $accountId !== null ? repo_orders($accountId, $from, $to, $q, $kind, $page)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];

// 주문 상태 타임라인: 이 페이지의 order_id 를 한 번의 질의로 모아 온다(N+1 금지).
$oeAvailable = repo_can_read('order_event');
$events = $oeAvailable ? repo_order_events(repo_collect_order_ids($res['rows'], 'id')) : [];

$tradeTp = ['0' => '보통(지정가)', '3' => '시장가', '5' => '조건부지정가', '6' => '최유리지정가', '7' => '최우선지정가'];
?>
<section class="panel">
  <div class="panel-h"><h2>주문내역</h2></div>
  <p class="note">주문 전송 게이트가 닫혀 있으면 주문은 전송되지 않고 <strong>신호만 기록</strong>됩니다(아래에서 배지로 구분).</p>
  <?= filter_form_open('trade.orders') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '검색', $q, '종목/주문번호/알고리즘') ?>
    <?= filter_field_select('kind', '유형', ['' => '전체', 'sent' => '실제 전송', 'signal' => '신호만 기록'], $kind) ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('주문 내역이 없습니다.', '알고리즘이 신호를 내면 이곳에 기록됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>등록시각</th><th>상태</th><th>구분</th><th>종목</th><th>알고리즘</th>
        <th class="num">주문수량</th><th class="num">주문단가</th><th>매매구분</th>
        <th class="num">체결수량</th><th class="num">신호가</th><th class="num">평균체결가</th>
        <th class="num">슬리피지</th><th>주문번호</th><th>거부사유</th><th>사유 / 응답</th><th>상세</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <?php
        $slip = slippage_pct($r['signal_price'] ?? null, $r['avg_fill_pric'] ?? null);
        $oid = (int)$r['id'];
        $rowEvents = $events[$oid] ?? [];
        ?>
        <tr class="<?= ((int)$r['is_dry_run'] === 1 || $r['status'] === 'SIGNAL_ONLY') ? 'row-dry' : (in_array((string)$r['status'], ['FAILED', 'REJECTED'], true) ? 'lv-error' : '') ?>">
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= order_status_badge($r) ?></td>
          <td><?= side_badge((string)$r['side']) ?><?= $r['order_kind'] !== 'NEW' ? ' ' . badge((string)$r['order_kind'], 'muted') : '' ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td><?= h($r['algo_code']) ?></td>
          <td class="num"><?= h(nfmt($r['ord_qty'])) ?></td>
          <td class="num"><?= $r['ord_uv'] === null ? '시장가' : h(money($r['ord_uv'])) ?></td>
          <td><?= h($tradeTp[(string)$r['trde_tp']] ?? $r['trde_tp']) ?></td>
          <td class="num"><?= h(nfmt($r['filled_qty'])) ?></td>
          <td class="num"><?= h(money($r['signal_price'] ?? null)) ?></td>
          <td class="num"><?= h(money($r['avg_fill_pric'])) ?></td>
          <td class="num <?= h(sign_class($slip)) ?>"><?= $slip === null ? '-' : h(pct($slip, 3)) ?></td>
          <td class="mono"><?= h($r['ord_no'] ?? '-') ?></td>
          <td class="wrap-td"><?= ($r['reject_reason'] ?? '') !== '' ? h($r['reject_reason']) : '<span class="muted">-</span>' ?></td>
          <td class="wrap-td"><?= h($r['reason']) ?><?php if ($r['return_msg'] !== null && $r['return_msg'] !== ''): ?>
            <span class="muted">· <?= h($r['return_code']) ?> <?= h($r['return_msg']) ?></span><?php endif; ?></td>
          <td class="wrap-td">
            <details class="rowdet"><summary>상세</summary>
              <div class="rowdet-body">
                <p class="rd-k">주문 상태 타임라인</p>
                <?= order_timeline_html($rowEvents, $oeAvailable) ?>
                <?= json_details('신호 맥락 (signal_context)', $r['signal_context'] ?? null) ?>
                <?= json_details('파라미터 스냅샷 (params_snapshot)', $r['params_snapshot'] ?? null) ?>
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
