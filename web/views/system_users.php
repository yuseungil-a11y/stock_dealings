<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$users = repo_users();
$log = repo_login_log(50);
?>
<section class="panel">
  <div class="panel-h"><h2>사용자 목록</h2><span class="muted">관리자 전용 · 읽기 전용</span></div>
  <?php if ($users === []): ?>
    <?= empty_note('등록된 사용자가 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th class="num">ID</th><th>아이디</th><th>표시 이름</th><th>권한</th><th>상태</th>
        <th class="num">실패</th><th>잠금해제</th><th>마지막 로그인</th><th>생성</th>
      </tr></thead>
      <tbody>
      <?php foreach ($users as $r):
          $locked = !empty($r['locked_until']) && strtotime((string)$r['locked_until']) > time(); ?>
        <tr>
          <td class="num"><?= h($r['id']) ?></td>
          <td><?= h($r['username']) ?></td>
          <td><?= h($r['display_name'] ?? '-') ?></td>
          <td><?= badge($r['role'] === 'admin' ? 'admin' : 'viewer', $r['role'] === 'admin' ? 'info' : 'muted') ?></td>
          <td><?= (int)$r['is_active'] === 1 ? badge('활성', 'ok') : badge('비활성', 'muted') ?>
              <?= $locked ? ' ' . badge('잠금', 'err') : '' ?></td>
          <td class="num"><?= h(nfmt($r['failed_count'])) ?></td>
          <td><?= $locked ? h(kst($r['locked_until'])) : '-' ?></td>
          <td><?= h(kst($r['last_login_at'])) ?></td>
          <td><?= h(kst($r['created_at'], 'Y-m-d')) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <p class="note">사용자 추가·권한 변경은 이 화면에서 제공하지 않습니다(조회 전용). 필요 시 DB 관리자가 처리합니다.</p>
</section>

<section class="panel">
  <div class="panel-h"><h2>최근 로그인 시도 (50건)</h2></div>
  <?php if ($log === []): ?>
    <?= empty_note('로그인 시도 기록이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th class="num">ID</th><th>시각</th><th>아이디</th><th>결과</th><th>IP</th></tr></thead>
      <tbody>
      <?php foreach ($log as $r): ?>
        <tr>
          <td class="num"><?= h($r['id']) ?></td>
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= h($r['username']) ?></td>
          <td><?= (int)$r['success'] === 1 ? badge('성공', 'ok') : badge('실패', 'err') ?></td>
          <td class="mono"><?= h($r['ip_addr']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
