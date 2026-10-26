<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

/*
 * 기간별 수익률 조회.
 * 키움이 보여주는 수익률(holding.prft_rt)은 "현재 보유 종목"의 평단 대비 평가손익일 뿐이라
 * 이미 청산한 종목의 손익이 빠지고, 매매할 때마다 기준이 바뀌어 왜곡된다.
 * 이 화면의 "총 수익률(실현+평가)" 은 repo_combined_return() 으로 계산한다(대시보드와 같은 공식):
 *   ("기간 내 실현손익" + "현재 평가손익") ÷ ("기간 내 매도분 매입원가" + "현재 보유 매입금액")
 * 매도분 매입원가 = 매도금액 − 손익 − 수수료·제세금(ka10170 행 기준). 매수금액(buy_amt)을 분모에 쓰면
 * 아직 보유 중인 매수분이 holding.pur_amt 와 겹쳐 이중계산되므로 쓰지 않는다.
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
$ret = $accountId !== null ? repo_combined_return($accountId, $from, $to) : null;

$periodBuy = (float)$periodTotals['buy_amt'];
$periodPl = (float)($ret['realized_pl'] ?? 0);
$periodCost = (float)($ret['realized_cost'] ?? 0);
$periodRate = $ret['realized_rate'] ?? null;

$combinedPl = (float)($ret['combined_pl'] ?? 0);
$combinedCost = (float)($ret['combined_cost'] ?? 0);
$combinedRate = $ret['combined_rate'] ?? null;

$points = [];
foreach ($breakdown as $b) {
    if ($b['rate'] === null) {
        continue;
    }
    $points[] = ['label' => (string)$b['period_label'], 'value' => (float)$b['rate']];
}

// 종료일 미입력(전체 기간) · 오늘 · 미래 날짜는 모두 "오늘 기준"으로 본다.
$toIsToday = $to === null || $to >= date('Y-m-d');
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
  <?= filter_form_close(['groupBy']) ?>
</section>

<section class="cards">
  <div class="card"><h2 class="card-t">기간 실현손익</h2>
    <p class="card-v <?= h(sign_class($periodPl)) ?>"><?= h(money_signed($periodPl)) ?></p>
    <p class="card-s <?= h(sign_class($periodRate)) ?>">수익률 <?= h(pct($periodRate)) ?></p>
    <p class="card-s">매도분 매입원가 <?= h(money($periodCost)) ?> 대비</p></div>
  <div class="card"><h2 class="card-t">기간 매수금액</h2><p class="card-v"><?= h(money($periodBuy)) ?></p>
    <p class="card-s"><?= h(nfmt($periodTotals['cnt'])) ?>건 · 참고용(수익률 분모 아님)</p></div>
  <div class="card"><h2 class="card-t">수수료 + 제세금</h2><p class="card-v"><?= h(money($periodTotals['cmsn_tax'])) ?></p></div>
  <div class="card"><h2 class="card-t">현재 평가손익</h2>
    <p class="card-v <?= h(sign_class($holdingTotals['evltv_prft'])) ?>"><?= h(money_signed($holdingTotals['evltv_prft'])) ?></p>
    <p class="card-s <?= h(sign_class($holdingTotals['prft_rt'])) ?>">수익률 <?= h(pct($holdingTotals['prft_rt'])) ?></p>
    <p class="card-s">※ 현재 시점(지금 보유 중인 종목) 기준 — 조회 기간과 무관합니다.</p></div>
  <div class="card"><h2 class="card-t">총 수익률(실현+평가)</h2>
    <p class="card-v <?= h(sign_class($combinedRate)) ?>"><?= h(pct($combinedRate)) ?></p>
    <p class="card-s <?= h(sign_class($combinedPl)) ?>">손익 <?= h(money_signed($combinedPl)) ?> / 투입 원가 <?= h(money($combinedCost)) ?></p>
    <p class="card-s">(기간 실현손익 + 현재 평가손익) ÷ (기간 내 매도분 매입원가 + 현재 보유 매입금액)입니다.
      평가손익은 항상 현재 시점 기준이므로 <strong>이 값은 선택한 종료일이 오늘(<?= $toIsToday ? '예' : '아니오 — 과거 종료일 선택 중' ?>)일 때 가장 의미가 있습니다.</strong></p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2><?= h($groupByLabel[$groupBy]) ?> 수익률 추이</h2></div>
  <?php if ($points === []): ?>
    <?= empty_note('표시할 데이터가 없습니다.', '해당 기간에 매매일지(daily_trade_summary) 기록이 없거나 매도가 없어 원가가 0인 버킷만 있습니다.') ?>
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
        <th>기간</th><th class="num">실현손익</th><th class="num">매도분 원가</th><th class="num">수익률</th><th class="num">매수금액</th><th class="num">거래건수</th>
      </tr></thead>
      <tbody>
      <?php foreach (array_reverse($breakdown) as $b): ?>
        <tr>
          <td><?= h($b['period_label']) ?></td>
          <td class="num <?= h(sign_class($b['pl_amt'])) ?>"><?= h(money_signed($b['pl_amt'])) ?></td>
          <td class="num"><?= h(money($b['cost_amt'])) ?></td>
          <td class="num <?= h(sign_class($b['rate'])) ?>"><?= h(pct($b['rate'])) ?></td>
          <td class="num"><?= h(money($b['buy_amt'])) ?></td>
          <td class="num"><?= h(nfmt($b['trade_count'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
<?php endif; ?>
