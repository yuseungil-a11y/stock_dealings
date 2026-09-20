<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$algos = repo_algorithms();
$roleLabel = ['entry' => '진입', 'risk' => '리스크관리', 'filter' => '필터'];
?>
<section class="panel">
  <div class="panel-h"><h2>알고리즘 현황</h2></div>
  <p class="note">이 화면은 <strong>읽기 전용</strong>입니다. 알고리즘 선택·우선순위·파라미터는 서버 프로그램(stock_svr) UI 에서만 변경됩니다.</p>
  <?php if ($algos === []): ?>
    <?= empty_note('등록된 알고리즘이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>사용</th><th class="num">우선순위</th><th>코드</th><th>이름</th><th>역할</th>
        <th>설명</th><th>파라미터</th><th>변경자 / 시각</th>
      </tr></thead>
      <tbody>
      <?php foreach ($algos as $a): ?>
        <tr>
          <td><?= (int)$a['is_enabled'] === 1 ? badge('ON', 'ok') : badge('OFF', 'muted') ?>
              <?= (int)$a['is_locked'] === 1 ? ' ' . badge('상시', 'info') : '' ?></td>
          <td class="num"><?= h(nfmt($a['priority'])) ?></td>
          <td class="mono"><?= h($a['code']) ?></td>
          <td><?= h($a['name']) ?></td>
          <td><?= h($roleLabel[(string)$a['role']] ?? $a['role']) ?></td>
          <td class="wrap-td"><?= h($a['description']) ?></td>
          <td><a class="btn btn-sm" href="<?= h(u('index.php?p=strategy.params&algo=' . rawurlencode((string)$a['code']))) ?>">보기</a></td>
          <td><?= h($a['updated_by'] ?? '-') ?><br><span class="muted"><?= h(kst($a['updated_at'], 'Y-m-d H:i')) ?></span></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
