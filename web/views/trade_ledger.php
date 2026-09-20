<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = $accountId !== null ? repo_ledger($accountId, $from, $to, $q, $page)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => PAGE_SIZE];
?>
<section class="panel">
  <div class="panel-h"><h2>거래내역 (증권사 정본)</h2></div>
  <p class="note">키움 위탁종합거래내역(kt00015)을 그대로 보관한 표입니다. 입출금·수수료를 포함합니다.</p>
  <?= filter_form_open('trade.ledger') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '검색', $q, '종목/거래종류/적요') ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('거래내역이 없습니다.', '서버 모듈이 장마감 후 거래내역을 수집하면 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>거래일</th><th>거래번호</th><th>거래종류</th><th>적요</th><th>종목</th>
        <th class="num">수량</th><th class="num">단가</th><th class="num">거래금액</th>
        <th class="num">수수료</th><th class="num">세금</th><th class="num">정산금액</th>
        <th class="num">거래후 예수금</th><th>처리시각</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr>
          <td><?= h(kst($r['trde_dt'], 'Y-m-d')) ?></td>
          <td class="mono"><?= h($r['trde_no']) ?></td>
          <td><?= h($r['trde_kind_nm']) ?></td>
          <td class="wrap-td"><?= h($r['rmrk_nm']) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td class="num"><?= h(nfmt($r['trde_qty'])) ?></td>
          <td class="num"><?= h(money($r['trde_unit'])) ?></td>
          <td class="num"><?= h(money($r['trde_amt'])) ?></td>
          <td class="num"><?= h(money($r['cmsn'])) ?></td>
          <td class="num"><?= h(money($r['tax'])) ?></td>
          <td class="num <?= h(sign_class($r['exct_amt'])) ?>"><?= h(money_signed($r['exct_amt'])) ?></td>
          <td class="num"><?= h(money($r['entra_remn'])) ?></td>
          <td><?= h($r['proc_tm']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
