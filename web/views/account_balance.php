<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId @var ?array $account */

$days = clean_int($_GET['days'] ?? 30, 30, 1, 365);
$series = $accountId !== null ? repo_balance_series($accountId, $days) : [];
$latest = $accountId !== null ? repo_latest_balance($accountId) : null;
$history = $accountId !== null ? repo_balance_history($accountId, 100) : [];

$metric = clean_enum($_GET['metric'] ?? '', ['tot_evlt_amt', 'entr', 'prsm_dpst_aset_amt', 'tot_evlt_pl'], 'tot_evlt_amt');
$metricLabel = [
    'tot_evlt_amt' => '총 평가금액',
    'entr' => '예수금',
    'prsm_dpst_aset_amt' => '추정예탁자산',
    'tot_evlt_pl' => '평가손익',
][$metric];

$points = [];
foreach ($series as $s) {
    if ($s[$metric] === null) {
        continue;
    }
    $points[] = ['label' => date('m-d', strtotime((string)$s['snapshot_at'])), 'value' => (float)$s[$metric]];
}
?>
<?php if ($accountId === null): ?>
  <?= empty_note('등록된 계좌가 없습니다.', '서버 모듈이 계좌를 등록하면 이 화면에 표시됩니다.') ?>
<?php else: ?>

<section class="cards">
  <div class="card"><h2 class="card-t">예수금</h2><p class="card-v"><?= $latest ? h(money($latest['entr'])) : '-' ?></p>
    <p class="card-s">D+1 <?= $latest ? h(money($latest['d1_entra'])) : '-' ?> / D+2 <?= $latest ? h(money($latest['d2_entra'])) : '-' ?></p></div>
  <div class="card"><h2 class="card-t">주문가능금액</h2><p class="card-v"><?= $latest ? h(money($latest['ord_alow_amt'])) : '-' ?></p>
    <p class="card-s">출금가능 <?= $latest ? h(money($latest['pymn_alow_amt'])) : '-' ?></p></div>
  <div class="card"><h2 class="card-t">총 평가금액</h2><p class="card-v"><?= $latest ? h(money($latest['tot_evlt_amt'])) : '-' ?></p>
    <p class="card-s">매입 <?= $latest ? h(money($latest['tot_pur_amt'])) : '-' ?></p></div>
  <div class="card"><h2 class="card-t">평가손익</h2>
    <p class="card-v <?= $latest ? h(sign_class($latest['tot_evlt_pl'])) : '' ?>"><?= $latest ? h(money_signed($latest['tot_evlt_pl'])) : '-' ?></p>
    <p class="card-s <?= $latest ? h(sign_class($latest['tot_prft_rt'])) : '' ?>"><?= $latest ? h(pct($latest['tot_prft_rt'])) : '-' ?></p></div>
</section>

<section class="panel">
  <div class="panel-h">
    <h2><?= h($metricLabel) ?> 추이 (최근 <?= (int)$days ?>일)</h2>
    <form class="filters filters-inline" method="get" action="<?= h(u('index.php')) ?>">
      <input type="hidden" name="p" value="account.balance">
      <?php if ($account !== null): ?><input type="hidden" name="account_id" value="<?= h($account['id']) ?>"><?php endif; ?>
      <?= filter_field_select('metric', '지표', [
          'tot_evlt_amt' => '총 평가금액', 'entr' => '예수금',
          'prsm_dpst_aset_amt' => '추정예탁자산', 'tot_evlt_pl' => '평가손익'], $metric) ?>
      <?= filter_field_select('days', '기간', ['7' => '7일', '30' => '30일', '90' => '90일', '180' => '180일', '365' => '1년'], (string)$days) ?>
      <span class="fld fld-btns"><button class="btn btn-primary btn-sm" type="submit">적용</button></span>
    </form>
  </div>
  <?php if ($points === []): ?>
    <?= empty_note('추이를 그릴 스냅샷 데이터가 없습니다.', '서버 모듈이 잔고를 주기적으로 기록하면 표시됩니다.') ?>
  <?php else: ?>
    <div class="chartwrap"><?= svg_line_chart($points, $metricLabel . ' 추이') ?></div>
  <?php endif; ?>
</section>

<section class="panel">
  <div class="panel-h"><h2>잔고 스냅샷 이력 (최근 100건)</h2></div>
  <?php if ($history === []): ?>
    <?= empty_note('기록된 잔고 스냅샷이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>시각</th><th class="num">예수금</th><th class="num">D+2 추정</th><th class="num">주문가능</th>
        <th class="num">매입금액</th><th class="num">평가금액</th><th class="num">평가손익</th><th class="num">수익률</th>
        <th class="num">추정예탁자산</th>
      </tr></thead>
      <tbody>
      <?php foreach ($history as $row): ?>
        <tr>
          <td><?= h(kst($row['snapshot_at'])) ?></td>
          <td class="num"><?= h(money($row['entr'])) ?></td>
          <td class="num"><?= h(money($row['d2_entra'])) ?></td>
          <td class="num"><?= h(money($row['ord_alow_amt'])) ?></td>
          <td class="num"><?= h(money($row['tot_pur_amt'])) ?></td>
          <td class="num"><?= h(money($row['tot_evlt_amt'])) ?></td>
          <td class="num <?= h(sign_class($row['tot_evlt_pl'])) ?>"><?= h(money_signed($row['tot_evlt_pl'])) ?></td>
          <td class="num <?= h(sign_class($row['tot_prft_rt'])) ?>"><?= h(pct($row['tot_prft_rt'])) ?></td>
          <td class="num"><?= h(money($row['prsm_dpst_aset_amt'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
<?php endif; ?>
