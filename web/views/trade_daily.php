<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = $accountId !== null ? repo_daily($accountId, $from, $to, $q, $page)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
$tot = $accountId !== null ? repo_daily_totals($accountId, $from, $to, $q)
    : ['cnt' => 0, 'buy_amt' => 0, 'sell_amt' => 0, 'cmsn_tax' => 0, 'pl_amt' => 0];

// 일자별 손익 추이
$points = [];
if ($accountId !== null) {
    $byDate = [];
    foreach (array_reverse($res['rows']) as $r) {
        $d = (string)$r['base_dt'];
        $byDate[$d] = ($byDate[$d] ?? 0) + (int)$r['pl_amt'];
    }
    foreach ($byDate as $d => $v) {
        $points[] = ['label' => date('m-d', strtotime($d)), 'value' => (float)$v];
    }
}
?>
<section class="cards">
  <div class="card"><h2 class="card-t">기간 매수금액</h2><p class="card-v"><?= h(money($tot['buy_amt'])) ?></p></div>
  <div class="card"><h2 class="card-t">기간 매도금액</h2><p class="card-v"><?= h(money($tot['sell_amt'])) ?></p></div>
  <div class="card"><h2 class="card-t">수수료 + 제세금</h2><p class="card-v"><?= h(money($tot['cmsn_tax'])) ?></p></div>
  <div class="card"><h2 class="card-t">기간 손익 합계</h2>
    <p class="card-v <?= h(sign_class($tot['pl_amt'])) ?>"><?= h(money_signed($tot['pl_amt'])) ?></p>
    <p class="card-s"><?= h(nfmt($tot['cnt'])) ?>건</p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2>매매일지 (일별 · 종목별 손익)</h2></div>
  <?= filter_form_open('trade.daily') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
  <?= filter_form_close() ?>

  <?php if ($points !== []): ?>
    <div class="chartwrap"><?= svg_line_chart($points, '일별 손익 추이') ?></div>
  <?php endif; ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('매매일지 데이터가 없습니다.', '서버 모듈이 장마감 후 당일매매일지를 수집하면 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>일자</th><th>종목</th>
        <th class="num">매수수량</th><th class="num">매수평단</th><th class="num">매수금액</th>
        <th class="num">매도수량</th><th class="num">매도평단</th><th class="num">매도금액</th>
        <th class="num">수수료+세금</th><th class="num">손익</th><th class="num">수익률</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr>
          <td><?= h(kst($r['base_dt'], 'Y-m-d')) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td class="num"><?= h(nfmt($r['buy_qty'])) ?></td>
          <td class="num"><?= h(money($r['buy_avg_pric'])) ?></td>
          <td class="num"><?= h(money($r['buy_amt'])) ?></td>
          <td class="num"><?= h(nfmt($r['sell_qty'])) ?></td>
          <td class="num"><?= h(money($r['sell_avg_pric'])) ?></td>
          <td class="num"><?= h(money($r['sell_amt'])) ?></td>
          <td class="num"><?= h(money($r['cmsn_tax'])) ?></td>
          <td class="num <?= h(sign_class($r['pl_amt'])) ?>"><?= h(money_signed($r['pl_amt'])) ?></td>
          <td class="num <?= h(sign_class($r['prft_rt'])) ?>"><?= h(pct($r['prft_rt'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
