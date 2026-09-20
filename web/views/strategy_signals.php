<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$type = clean_enum($_GET['type'] ?? '', ['BUY', 'SELL', 'HOLD', 'BLOCK'], '');
$codes = repo_algo_codes();
$algo = clean_enum($_GET['algo'] ?? '', $codes, '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = repo_signals($from, $to, $q, $type, $algo, $page);

$algoOptions = ['' => '전체'];
foreach ($codes as $c) {
    $algoOptions[$c] = $c;
}
$typeBadge = ['BUY' => 'buy', 'SELL' => 'sell', 'HOLD' => 'muted', 'BLOCK' => 'err'];
?>
<section class="panel">
  <div class="panel-h"><h2>신호 기록</h2></div>
  <p class="note">알고리즘이 생성한 모든 신호입니다. <strong>BLOCK</strong> 은 리스크 가드·필터에 의해 차단된 신호입니다.</p>
  <?= filter_form_open('strategy.signals') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '검색', $q, '종목 또는 상세') ?>
    <?= filter_field_select('type', '신호', ['' => '전체', 'BUY' => '매수', 'SELL' => '매도', 'HOLD' => '보유', 'BLOCK' => '차단'], $type) ?>
    <?= filter_field_select('algo', '알고리즘', $algoOptions, $algo) ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('신호 기록이 없습니다.', '서버 모듈이 알고리즘을 평가하면 이곳에 기록됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>시각</th><th>신호</th><th>알고리즘</th><th>종목</th><th class="num">점수</th>
        <th>상세</th><th class="num">연결 주문</th><th class="num">실행 ID</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr>
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= badge((string)$r['signal_type'], $typeBadge[(string)$r['signal_type']] ?? '') ?></td>
          <td class="mono"><?= h($r['algo_code']) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td class="num"><?= $r['score'] === null ? '-' : h(nfmt($r['score'], 2)) ?></td>
          <td class="wrap-td"><?= h($r['detail']) ?></td>
          <td class="num"><?= h($r['order_id'] ?? '-') ?></td>
          <td class="num"><?= h($r['run_id'] ?? '-') ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
