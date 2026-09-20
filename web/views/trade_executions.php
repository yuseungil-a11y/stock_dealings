<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$side = clean_enum($_GET['side'] ?? '', ['BUY', 'SELL'], '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = $accountId !== null ? repo_executions($accountId, $from, $to, $q, $side, $page)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
?>
<section class="panel">
  <div class="panel-h"><h2>체결내역</h2></div>
  <?= filter_form_open('trade.executions') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '검색', $q, '종목/주문번호') ?>
    <?= filter_field_select('side', '구분', ['' => '전체', 'BUY' => '매수', 'SELL' => '매도'], $side) ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('체결 내역이 없습니다.', '서버 모듈이 체결을 수집하면 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>체결시각</th><th>구분</th><th>종목</th><th class="num">체결수량</th><th class="num">체결가</th>
        <th class="num">체결금액</th><th class="num">수수료</th><th class="num">세금</th>
        <th>주문번호</th><th>체결번호</th><th>수집</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr>
          <td><?= h(kst($r['executed_at'])) ?></td>
          <td><?= side_badge((string)$r['side']) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td class="num"><?= h(nfmt($r['cntr_qty'])) ?></td>
          <td class="num"><?= h(money($r['cntr_pric'])) ?></td>
          <td class="num"><?= h(money((int)$r['cntr_qty'] * (int)$r['cntr_pric'])) ?></td>
          <td class="num"><?= h(money($r['cmsn'])) ?></td>
          <td class="num"><?= h(money($r['tax'])) ?></td>
          <td class="mono"><?= h($r['ord_no']) ?></td>
          <td class="mono"><?= h($r['cntr_no']) ?></td>
          <td><?= badge((string)$r['source'], 'muted') ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
