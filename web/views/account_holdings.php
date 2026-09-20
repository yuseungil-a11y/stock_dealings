<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId @var ?array $account */

$sort = clean_enum($_GET['sort'] ?? '', array_keys(HOLDING_SORTS), 'evlt_amt_desc');
$q = clean_text($_GET['q'] ?? '', 30);
$rows = $accountId !== null ? repo_holdings($accountId, $sort, $q) : [];
$tot = $accountId !== null ? repo_holding_totals($accountId) : repo_holding_totals_empty();

/* 현재 목록(필터 적용) 기준 합계 — 상장폐지 종목 제외분도 함께 계산한다. */
$sumPur = $sumEvlt = $sumPl = 0;
$sumPurEx = $sumPlEx = 0;
$delistedInList = 0;
foreach ($rows as $r) {
    $sumPur += (int)$r['pur_amt'];
    $sumEvlt += (int)$r['evlt_amt'];
    $sumPl += (int)$r['evltv_prft'];
    if (!empty($r['delisted'])) {
        $delistedInList++;
    } else {
        $sumPurEx += (int)$r['pur_amt'];
        $sumPlEx += (int)$r['evltv_prft'];
    }
}
$sumRt = $sumPur > 0 ? $sumPl / $sumPur * 100 : null;
$sumRtEx = $sumPurEx > 0 ? $sumPlEx / $sumPurEx * 100 : null;
?>
<?php if ($accountId === null): ?>
  <?= empty_note('등록된 계좌가 없습니다.', '서버 모듈이 계좌를 등록하면 이 화면에 표시됩니다.') ?>
<?php else: ?>

<section class="cards">
  <div class="card"><h2 class="card-t">보유 종목수</h2><p class="card-v"><?= h(nfmt($tot['cnt'])) ?><span class="unit">종목</span></p></div>
  <div class="card"><h2 class="card-t">총 매입금액</h2><p class="card-v"><?= h(money($tot['pur_amt'])) ?></p></div>
  <div class="card"><h2 class="card-t">총 평가금액</h2><p class="card-v"><?= h(money($tot['evlt_amt'])) ?></p></div>
  <div class="card"><h2 class="card-t">평가손익</h2>
    <p class="card-v <?= h(sign_class($tot['evltv_prft'])) ?>"><?= h(money_signed($tot['evltv_prft'])) ?></p>
    <p class="card-s <?= h(sign_class($tot['prft_rt'])) ?>"><?= h(pct($tot['prft_rt'])) ?></p>
    <?php if ($tot['delisted_cnt'] > 0 && $tot['prft_rt_ex'] !== null): ?>
      <p class="card-s">상장폐지 종목 제외 시 <span class="<?= h(sign_class($tot['prft_rt_ex'])) ?>"><?= h(pct($tot['prft_rt_ex'])) ?></span>
        <span class="muted">(<?= h(nfmt($tot['delisted_cnt'])) ?>종목 제외)</span></p>
    <?php endif; ?>
  </div>
</section>

<section class="panel">
  <div class="panel-h"><h2>보유종목</h2></div>
  <?= filter_form_open('account.holdings') ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
    <?= filter_field_select('sort', '정렬', [
        'evlt_amt_desc' => '평가금액 많은순', 'evlt_amt_asc' => '평가금액 적은순',
        'prft_rt_desc' => '수익률 높은순', 'prft_rt_asc' => '수익률 낮은순',
        'evltv_prft_desc' => '손익 큰순', 'evltv_prft_asc' => '손익 작은순',
        'stk_nm_asc' => '종목명순'], $sort) ?>
  <?= filter_form_close() ?>

  <?php if ($rows === []): ?>
    <?= empty_note($q !== '' ? '검색 조건에 맞는 보유종목이 없습니다.' : '보유 중인 종목이 없습니다.',
        '서버 모듈이 계좌 잔고를 동기화하면 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl tbl-sortable">
      <thead><tr>
        <th data-sort="str">종목명</th><th data-sort="str">코드</th>
        <th class="num" data-sort="num">보유수량</th><th class="num" data-sort="num">매매가능</th>
        <th class="num" data-sort="num">매입가</th><th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">매입금액</th><th class="num" data-sort="num">평가금액</th>
        <th class="num" data-sort="num">평가손익</th><th class="num" data-sort="num">수익률</th>
        <th class="num" data-sort="num">비중</th><th>갱신</th>
      </tr></thead>
      <tbody>
      <?php foreach ($rows as $r): ?>
        <?php
        $isDel = !empty($r['delisted']);
        $dispRt = holding_display_rate($r);
        $delNote = $isDel ? holding_delisted_note($r) : '';
        ?>
        <tr<?= $isDel ? ' class="row-delisted"' : '' ?>>
          <td class="stk-nm"><?= h($r['stk_nm']) ?><?= $isDel ? ' ' . delisted_badge() : '' ?></td>
          <td class="stk-cd"><?= h($r['stk_cd']) ?></td>
          <td class="num" data-v="<?= h($r['rmnd_qty']) ?>"><?= h(nfmt($r['rmnd_qty'])) ?></td>
          <td class="num" data-v="<?= h($r['trde_able_qty']) ?>"><?= h(nfmt($r['trde_able_qty'])) ?></td>
          <td class="num" data-v="<?= h($r['pur_pric']) ?>"><?= h(money($r['pur_pric'])) ?></td>
          <td class="num" data-v="<?= h($r['cur_prc']) ?>"><?= h(money($r['cur_prc'])) ?></td>
          <td class="num" data-v="<?= h($r['pur_amt']) ?>"><?= h(money($r['pur_amt'])) ?></td>
          <td class="num" data-v="<?= h($r['evlt_amt']) ?>"><?= h(money($r['evlt_amt'])) ?></td>
          <td class="num <?= h(sign_class($r['evltv_prft'])) ?>" data-v="<?= h($r['evltv_prft']) ?>"><?= h(money_signed($r['evltv_prft'])) ?></td>
          <?php if ($isDel): ?>
            <td class="num <?= h(sign_class($dispRt)) ?>" data-v="<?= h($dispRt) ?>" title="<?= h($delNote) ?>">
              <?= h(pct($dispRt, 1)) ?><span class="delisted-mark" aria-hidden="true">*</span>
              <span class="sr-only"><?= h($delNote) ?></span>
            </td>
          <?php else: ?>
            <td class="num <?= h(sign_class($r['prft_rt'])) ?>" data-v="<?= h($r['prft_rt']) ?>"><?= h(pct($r['prft_rt'])) ?></td>
          <?php endif; ?>
          <td class="num" data-v="<?= h($r['poss_rt']) ?>"><?= h(pct($r['poss_rt'])) ?></td>
          <td><?= h(kst($r['updated_at'], 'm-d H:i')) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
      <tfoot><tr>
        <th colspan="6">합계 (<?= h(nfmt(count($rows))) ?>종목)</th>
        <th class="num"><?= h(money($sumPur)) ?></th>
        <th class="num"><?= h(money($sumEvlt)) ?></th>
        <th class="num <?= h(sign_class($sumPl)) ?>"><?= h(money_signed($sumPl)) ?></th>
        <th class="num <?= h(sign_class($sumRt)) ?>"><?= h(pct($sumRt)) ?></th>
        <th colspan="2"></th>
      </tr>
      <?php if ($delistedInList > 0 && $sumRtEx !== null): ?>
      <tr>
        <th colspan="9">상장폐지 종목 제외 시 총수익률 (<?= h(nfmt($delistedInList)) ?>종목 제외)</th>
        <th class="num <?= h(sign_class($sumRtEx)) ?>"><?= h(pct($sumRtEx)) ?></th>
        <th colspan="2"></th>
      </tr>
      <?php endif; ?>
      </tfoot>
    </table>
  </div>
  <?php if ($delistedInList > 0): ?>
    <p class="note"><strong>*</strong> 표시 종목은 현재가가 0 이거나 종목명이 <code>(폐)</code> 로 시작하는
      <strong>상장폐지·거래불가</strong> 종목입니다. 키움이 내려주는 수익률(0.00%)이 실제와 다르므로
      <strong>평가손익 ÷ 매입금액 × 100</strong> 으로 계산한 실제 손익 기준 수익률을 표시합니다.</p>
  <?php endif; ?>
  <?php endif; ?>
</section>
<?php endif; ?>
