<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 시스템 → 이벤트 · API 오류 보관 (읽기 전용).
 * event_log / api_call_log 는 7일 정리 대상이지만, 이 두 표는 장기 보관본이다.
 */

$tab = clean_enum($_GET['tab'] ?? '', ['events', 'api'], 'events');
$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);

$retention = repo_setting('archive_retention_days', '365');
$logRetention = repo_setting('log_retention_days', '7');

$tabs = ['events' => '이벤트 보관(event_archive)', 'api' => 'API 오류 보관(api_error_log)'];
?>
<section class="panel">
  <div class="panel-h"><h2>이벤트 · API 오류 보관</h2>
    <span class="muted">보관 기간: <?= h($retention) ?>일</span></div>
  <p class="note">최근 <strong><?= h($logRetention) ?>일</strong>만 유지되는
    <a href="<?= h(u('index.php?p=system.events')) ?>">이벤트 로그</a>와 달리, 이 기록은
    <code>archive_retention_days</code>(<?= h($retention) ?>일) 동안 보관됩니다.
    주요 이벤트(WARN · ERROR, 주문 · 알고리즘 · 엔진)와 키움 API <strong>오류 응답</strong>만 남으므로
    지난 거래의 원인을 나중에 되짚을 때 사용합니다. 이 화면은 조회 전용입니다.</p>

  <?= tab_links($tabs, $tab) ?>

<?php if ($tab === 'events'): ?>
  <?php
  $level = clean_enum($_GET['level'] ?? '', ['DEBUG', 'INFO', 'WARN', 'ERROR'], '');
  $cats = repo_event_archive_categories();
  $category = clean_enum($_GET['category'] ?? '', $cats, '');
  $q = clean_text($_GET['q'] ?? '', 60);
  $available = repo_can_read('event_archive');
  $res = repo_event_archive($from, $to, $level, $category, $q, $page);
  $catOptions = ['' => '전체'];
  foreach ($cats as $c) {
      $catOptions[$c] = $c;
  }
  ?>
  <?= filter_form_open('system.archive', ['tab' => 'events']) ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_select('level', '레벨', ['' => '전체', 'ERROR' => 'ERROR', 'WARN' => 'WARN', 'INFO' => 'INFO', 'DEBUG' => 'DEBUG'], $level) ?>
    <?= filter_field_select('category', '분류', $catOptions, $category) ?>
    <?= filter_field_text('q', '메시지', $q, '메시지 검색') ?>
  <?= filter_form_close() ?>

  <?php if (!$available): ?>
    <?= empty_note('이벤트 보관 표(event_archive)를 읽을 수 없습니다.',
        '데이터베이스에 해당 표가 없거나 조회 권한이 없습니다.') ?>
  <?php elseif ($res['rows'] === []): ?>
    <?= empty_note('보관된 이벤트가 없습니다.',
        '서버 모듈이 WARN · ERROR 또는 주문 · 알고리즘 이벤트를 남기면 이곳에 장기 보관됩니다.') ?>
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

<?php else: ?>
  <?php
  $apiIds = repo_api_error_ids();
  $apiId = clean_enum($_GET['api_id'] ?? '', $apiIds, '');
  $codes = array_map('strval', repo_api_error_codes());
  $rc = clean_enum($_GET['rc'] ?? '', $codes, '');
  $available = repo_can_read('api_error_log');
  $res = repo_api_errors($from, $to, $apiId, $rc, $page);
  $apiOptions = ['' => '전체'];
  foreach ($apiIds as $a) {
      $apiOptions[$a] = $a;
  }
  $codeOptions = ['' => '전체'];
  foreach ($codes as $c) {
      $codeOptions[$c] = $c;
  }
  ?>
  <?= filter_form_open('system.archive', ['tab' => 'api']) ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_select('api_id', 'API ID', $apiOptions, $apiId) ?>
    <?= filter_field_select('rc', '응답코드', $codeOptions, $rc) ?>
  <?= filter_form_close() ?>

  <?php if (!$available): ?>
    <?= empty_note('API 오류 보관 표(api_error_log)를 읽을 수 없습니다.',
        '데이터베이스에 해당 표가 없거나 조회 권한이 없습니다.') ?>
  <?php elseif ($res['rows'] === []): ?>
    <?= empty_note('보관된 API 오류가 없습니다.', '키움 API 가 오류 응답을 내려주면 이곳에 장기 보관됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th class="num">ID</th><th>시각</th><th>API ID</th><th class="num">HTTP</th>
        <th class="num">응답코드</th><th>응답메시지</th><th class="num">소요(ms)</th></tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <tr class="lv-error">
          <td class="num"><?= h($r['id']) ?></td>
          <td><?= h(kst($r['created_at'])) ?></td>
          <td class="mono"><?= h($r['api_id']) ?></td>
          <td class="num"><?= h($r['http_status'] ?? '-') ?></td>
          <td class="num"><?= h($r['return_code'] ?? '-') ?></td>
          <td class="wrap-td"><?= h($r['return_msg']) ?></td>
          <td class="num"><?= h(nfmt($r['elapsed_ms'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
<?php endif; ?>
</section>
