<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 60);
$level = clean_enum($_GET['level'] ?? '', ['DEBUG', 'INFO', 'WARN', 'ERROR'], '');
$cats = repo_event_categories();
$category = clean_enum($_GET['category'] ?? '', $cats, '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$res = repo_events($from, $to, $level, $category, $q, $page);

$catOptions = ['' => '전체'];
foreach ($cats as $c) {
    $catOptions[$c] = $c;
}
?>
<section class="panel">
  <div class="panel-h"><h2>이벤트 로그</h2>
    <span class="muted">보관 기간: <?= h(repo_setting('log_retention_days', '7')) ?>일</span></div>
  <?= filter_form_open('system.events') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_select('level', '레벨', ['' => '전체', 'ERROR' => 'ERROR', 'WARN' => 'WARN', 'INFO' => 'INFO', 'DEBUG' => 'DEBUG'], $level) ?>
    <?= filter_field_select('category', '분류', $catOptions, $category) ?>
    <?= filter_field_text('q', '메시지', $q, '메시지 검색') ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= empty_note('이벤트 기록이 없습니다.', '서버 모듈이 동작하면 연결·신호·주문 등의 이벤트가 기록됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th class="num">ID</th><th>시각</th><th>레벨</th><th>분류</th><th>메시지</th></tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr class="lv-<?= h(strtolower((string)$r['level'])) ?>">
          <td class="num"><?= h($r['id']) ?></td>
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= level_badge((string)$r['level']) ?></td>
          <td class="mono"><?= h($r['category']) ?></td>
          <td class="wrap-td"><?= h($r['message']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
