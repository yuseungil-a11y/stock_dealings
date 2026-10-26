<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

/*
 * 기간별 수익률 조회.
 * 키움이 보여주는 수익률(holding.prft_rt)은 "현재 보유 종목"의 평단 대비 평가손익일 뿐이라
 * 이미 청산한 종목의 손익이 빠지고, 매매할 때마다 기준이 바뀌어 왜곡된다.
 * 진짜 수익률은 "기간 내 실현손익 + 현재 평가손익" ÷ "기간 내 매수금액 + 현재 보유 매수금액" 이며,
 * 이 계산은 매매 횟수와 무관하게 누적해도 정확하다 — 이 화면의 "총 수익률(실현+평가)" 카드가 그 값이다.
 */

// from 을 요청에서 아예 안 보냈을 때만(최초 진입) 기본값(90일 전)을 적용한다.
// 사용자가 필터를 비워서 보냈으면(초기화 등) clean_date('') 가 null 이 되어 "전체 기간"으로 동작한다.
$from = isset($_GET['from']) ? clean_date($_GET['from']) : date('Y-m-d', strtotime('-90 days'));
$to = isset($_GET['to']) ? clean_date($_GET['to']) : date('Y-m-d');
$groupBy = clean_enum($_GET['groupBy'] ?? '', PERIOD_BREAKDOWN_GROUPS, 'week');
$groupByLabel = ['day' => '일별', 'week' => '주별', 'month' => '월별'];

$periodTotals = $accountId !== null ? repo_daily_totals($accountId, $from, $to, '')
    : ['cnt' => 0, 'buy_amt' => 0, 'sell_amt' => 0, 'cmsn_tax' => 0, 'pl_amt' => 0];
$holdingTotals = $accountId !== null ? repo_holding_totals($accountId) : repo_holding_totals_empty();
$breakdown = $accountId !== null ? repo_period_breakdown($accountId, $from, $to, $groupBy) : [];

$periodBuy = (float)$periodTotals['buy_amt'];
$periodPl = (float)$periodTotals['pl_amt'];
$periodRate = $periodBuy > 0 ? ($periodPl / $periodBuy * 100) : null;

$combinedPl = $periodPl + (float)$holdingTotals['evltv_prft'];
$combinedBuy = $periodBuy + (float)$holdingTotals['pur_amt'];
$combinedRate = $combinedBuy > 0 ? ($combinedPl / $combinedBuy * 100) : null;

$points = [];
foreach ($breakdown as $b) {
    if ($b['rate'] === null) {
        continue;
    }
    $points[] = ['label' => (string)$b['period_label'], 'value' => (float)$b['rate']];
}

$toIsToday = $to === date('Y-m-d');
?>
<?php if ($accountId === null): ?>
  <?= empty_note('등록된 계좌가 없습니다.', '서버 모듈이 계좌를 등록하면 이 화면에 표시됩니다.') ?>
<?php else: ?>

<section class="panel">
  <div class="panel-h"><h2>기간별 수익률</h2></div>
  <?= filter_form_open('account.returns') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_select('groupBy', '그룹', ['day' => '일별', 'week' => '주별', 'month' => '월별'], $groupBy) ?>
  <?= filter_form_close() ?>
</section>

<section class="cards">
  <div class="card"><h2 class="card-t">기간 실현손익</h2>
    <p class="card-v <?= h(sign_class($periodPl)) ?>"><?= h(money_signed($periodPl)) ?></p>
    <p class="card-s <?= h(sign_class($periodRate)) ?>">수익률 <?= h(pct($periodRate)) ?></p></div>
  <div class="card"><h2 class="card-t">기간 매수금액</h2><p class="card-v"><?= h(money($periodBuy)) ?></p>
    <p class="card-s"><?= h(nfmt($periodTotals['cnt'])) ?>건</p></div>
  <div class="card"><h2 class="card-t">수수료 + 제세금</h2><p class="card-v"><?= h(money($periodTotals['cmsn_tax'])) ?></p></div>
  <div class="card"><h2 class="card-t">현재 평가손익</h2>
    <p class="card-v <?= h(sign_class($holdingTotals['evltv_prft'])) ?>"><?= h(money_signed($holdingTotals['evltv_prft'])) ?></p>
    <p class="card-s <?= h(sign_class($holdingTotals['prft_rt'])) ?>">수익률 <?= h(pct($holdingTotals['prft_rt'])) ?></p>
    <p class="card-s">※ 현재 시점(지금 보유 중인 종목) 기준 — 조회 기간과 무관합니다.</p></div>
  <div class="card"><h2 class="card-t">총 수익률(실현+평가)</h2>
    <p class="card-v <?= h(sign_class($combinedRate)) ?>"><?= h(pct($combinedRate)) ?></p>
    <p class="card-s <?= h(sign_class($combinedPl)) ?>">손익 <?= h(money_signed($combinedPl)) ?> / 매수금액 <?= h(money($combinedBuy)) ?></p>
    <p class="card-s">기간 실현손익 + 현재 평가손익을 기간 매수금액 + 현재 보유 매수금액으로 나눈, 매매 빈도와 무관한 실제 수익률입니다.
      평가손익은 항상 현재 시점 기준이므로 <strong>이 값은 선택한 종료일이 오늘(<?= $toIsToday ? '예' : '아니오 — 과거 종료일 선택 중' ?>)일 때 가장 의미가 있습니다.</strong></p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2><?= h($groupByLabel[$groupBy]) ?> 수익률 추이</h2></div>
  <?php if ($points === []): ?>
    <?= empty_note('표시할 데이터가 없습니다.', '해당 기간에 매매일지(daily_trade_summary) 기록이 없거나 매수금액이 0인 버킷만 있습니다.') ?>
  <?php else: ?>
    <div class="chartwrap"><?= svg_line_chart($points, $groupByLabel[$groupBy] . ' 수익률 추이', 720, 220, 2, '%') ?></div>
  <?php endif; ?>
</section>

<section class="panel">
  <div class="panel-h"><h2><?= h($groupByLabel[$groupBy]) ?> 손익 · 수익률 내역</h2></div>
  <?php if ($breakdown === []): ?>
    <?= empty_note('표시할 데이터가 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>기간</th><th class="num">손익</th><th class="num">매수금액</th><th class="num">수익률</th><th class="num">거래건수</th>
      </tr></thead>
      <tbody>
      <?php foreach (array_reverse($breakdown) as $b): ?>
        <tr>
          <td><?= h($b['period_label']) ?></td>
          <td class="num <?= h(sign_class($b['pl_amt'])) ?>"><?= h(money_signed($b['pl_amt'])) ?></td>
          <td class="num"><?= h(money($b['buy_amt'])) ?></td>
          <td class="num <?= h(sign_class($b['rate'])) ?>"><?= h(pct($b['rate'])) ?></td>
          <td class="num"><?= h(nfmt($b['trade_count'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
<?php endif; ?>
