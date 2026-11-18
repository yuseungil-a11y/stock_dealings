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

/* 매도보호(매도 알고리즘 제외) — 쓰기는 관리자만. 비관리자는 상태만 읽기전용으로 본다. */
$isAdmin = auth_is_admin();
$protectedInList = 0;
foreach ($rows as $r) {
    if (!empty($r['sell_protected'])) {
        $protectedInList++;
    }
}
/* 매도보호 지정 종목 전체(보유 여부 무관) — 전량매도 종목의 남은 보호를 수동 해제하기 위한 별도 패널용 */
$protList = $accountId !== null ? repo_sell_protected_list($accountId) : [];
$protectTip = '매도보호 종목:-7% 손절을 포함한 모든 매도 알고리즘이 작동하지 않습니다(수동 매도만 가능).';
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
  <p class="alert alert-err"><strong>⚠ 매도제외 체크 주의:</strong> 체크한 종목에는
    <strong>-7% 손절을 포함한 모든 매도 알고리즘이 전혀 작동하지 않습니다.</strong>
    주가가 계속 하락해도 자동 매도되지 않으며, <strong>수동 매도(키움 앱 등)만 가능</strong>합니다.
    매수(물타기 등) 알고리즘은 그대로 동작합니다.
    <?php if ($protectedInList > 0): ?>
      현재 목록에서 <strong><?= h(nfmt($protectedInList)) ?>종목</strong>이 매도보호 중입니다.
    <?php endif; ?></p>
  <?php if ($isAdmin): ?>
  <form method="post" action="<?= h(u('index.php')) ?>">
    <?= csrf_field() ?>
    <input type="hidden" name="action" value="holding_sell_protect_save">
    <input type="hidden" name="account_id" value="<?= (int)$accountId ?>">
    <input type="hidden" name="sort" value="<?= h($sort) ?>">
    <input type="hidden" name="q" value="<?= h($q) ?>">
  <?php endif; ?>
  <div class="tablewrap">
    <table class="tbl tbl-sortable">
      <thead><tr>
        <?php /* data-sort 필수: app.js 정렬은 th[data-sort] 중 순번을 셀 인덱스로 쓰므로,
                 앞쪽 열에 data-sort 가 없으면 뒤 열 정렬이 한 칸씩 어긋난다. */ ?>
        <th class="shrink" data-sort="num" title="<?= h($protectTip) ?>">매도제외</th>
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
        $isProt = !empty($r['sell_protected']);
        $protBadge = $isProt ? ' ' . badge('매도보호', 'warn', $protectTip
            . ($r['sell_protected_by'] !== null && $r['sell_protected_by'] !== ''
                ? ' · 지정: ' . $r['sell_protected_by'] . ' ' . kst($r['sell_protected_at'], 'Y-m-d H:i') : '')) : '';
        ?>
        <tr<?= $isDel ? ' class="row-delisted"' : '' ?>>
          <td class="shrink" data-v="<?= $isProt ? 1 : 0 ?>">
            <?php if ($isAdmin): ?>
              <input type="hidden" name="shown[]" value="<?= h($r['stk_cd']) ?>">
              <input type="checkbox" name="protect[]" value="<?= h($r['stk_cd']) ?>"
                aria-label="<?= h($r['stk_nm'] . ' 매도제외') ?>"<?= $isProt ? ' checked' : '' ?>>
            <?php else: ?>
              <input type="checkbox" disabled aria-label="<?= h($r['stk_nm'] . ' 매도제외') ?>"<?= $isProt ? ' checked' : '' ?>>
            <?php endif; ?>
          </td>
          <td class="stk-nm"><?= h($r['stk_nm']) ?><?= $isDel ? ' ' . delisted_badge() : '' ?><?= $protBadge ?></td>
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
        <th colspan="7">합계 (<?= h(nfmt(count($rows))) ?>종목)</th>
        <th class="num"><?= h(money($sumPur)) ?></th>
        <th class="num"><?= h(money($sumEvlt)) ?></th>
        <th class="num <?= h(sign_class($sumPl)) ?>"><?= h(money_signed($sumPl)) ?></th>
        <th class="num <?= h(sign_class($sumRt)) ?>"><?= h(pct($sumRt)) ?></th>
        <th colspan="2"></th>
      </tr>
      <?php if ($delistedInList > 0 && $sumRtEx !== null): ?>
      <tr>
        <th colspan="10">상장폐지 종목 제외 시 총수익률 (<?= h(nfmt($delistedInList)) ?>종목 제외)</th>
        <th class="num <?= h(sign_class($sumRtEx)) ?>"><?= h(pct($sumRtEx)) ?></th>
        <th colspan="2"></th>
      </tr>
      <?php endif; ?>
      </tfoot>
    </table>
  </div>
  <?php if ($isAdmin): ?>
    <button class="btn btn-primary" type="submit">매도제외 설정 저장</button>
  </form>
  <?php else: ?>
    <p class="note">매도제외(매도보호) 지정/해제는 관리자만 할 수 있습니다.</p>
  <?php endif; ?>
  <?php if ($delistedInList > 0): ?>
    <p class="note"><strong>*</strong> 표시 종목은 현재가가 0 이거나 종목명이 <code>(폐)</code> 로 시작하는
      <strong>상장폐지·거래불가</strong> 종목입니다. 키움이 내려주는 수익률(0.00%)이 실제와 다르므로
      <strong>평가손익 ÷ 매입금액 × 100</strong> 으로 계산한 실제 손익 기준 수익률을 표시합니다.</p>
  <?php endif; ?>
  <?php endif; ?>
</section>

<?php if ($protList !== []): ?>
<section class="panel">
  <div class="panel-h"><h2>매도보호 지정 종목</h2></div>
  <p class="note">보유하지 않는(전량매도된) 종목도 포함됩니다 — 매도보호는 전량매도해도 자동으로 풀리지 않으므로,
    필요 없어진 종목은 아래에서 해제할 수 있습니다.</p>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>종목명</th><th>코드</th><th>보유</th><th>지정자</th><th>지정시각</th>
        <?php if ($isAdmin): ?><th class="shrink">해제</th><?php endif; ?>
      </tr></thead>
      <tbody>
      <?php foreach ($protList as $p): ?>
        <tr>
          <td class="stk-nm"><?= h($p['stk_nm'] ?? '-') ?></td>
          <td class="stk-cd"><?= h($p['stk_cd']) ?></td>
          <td><?= $p['currently_held']
              ? badge('보유중', 'info')
              : badge('미보유', 'warn', '현재 보유하지 않는 종목입니다. 다시 매수하면 매도보호가 그대로 적용됩니다.') ?></td>
          <td><?= h($p['sell_protected_by'] ?? '-') ?></td>
          <td><?= h(kst($p['sell_protected_at'], 'Y-m-d H:i')) ?></td>
          <?php if ($isAdmin): ?>
          <td class="shrink">
            <form method="post" action="<?= h(u('index.php')) ?>">
              <?= csrf_field() ?>
              <input type="hidden" name="action" value="sell_protect_clear">
              <input type="hidden" name="account_id" value="<?= (int)$accountId ?>">
              <input type="hidden" name="stk_cd" value="<?= h($p['stk_cd']) ?>">
              <button class="btn btn-sm" type="submit"
                aria-label="<?= h(($p['stk_nm'] ?? $p['stk_cd']) . ' 매도보호 해제') ?>">해제</button>
            </form>
          </td>
          <?php endif; ?>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php if (!$isAdmin): ?>
    <p class="note">매도보호 해제는 관리자만 할 수 있습니다.</p>
  <?php endif; ?>
</section>
<?php endif; ?>
<?php endif; ?>
