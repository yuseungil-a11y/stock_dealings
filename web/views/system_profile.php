<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var array $user */

$me = repo_user($user['id']);
$myLog = repo_my_login_log($user['username'], 20);
?>
<div class="grid2">
<section class="panel">
  <div class="panel-h"><h2>내 정보</h2></div>
  <ul class="kv">
    <li><span>아이디</span><strong><?= h($user['username']) ?></strong></li>
    <li><span>표시 이름</span><strong><?= h($me['display_name'] ?? '-') ?></strong></li>
    <li><span>권한</span><strong><?= h($user['role'] === 'admin' ? '관리자 (admin)' : '조회자 (viewer)') ?></strong></li>
    <li><span>마지막 로그인</span><strong><?= h(kst($me['last_login_at'] ?? null)) ?></strong></li>
    <li><span>계정 생성</span><strong><?= h(kst($me['created_at'] ?? null)) ?></strong></li>
    <li><span>세션 만료</span><strong>유휴 <?= (int)(APP_IDLE_TIMEOUT / 60) ?>분 / 최대 <?= (int)(APP_ABSOLUTE_TIMEOUT / 3600) ?>시간</strong></li>
  </ul>
</section>

<section class="panel">
  <div class="panel-h"><h2>비밀번호 변경</h2></div>
  <form class="form-narrow" method="post" action="<?= h(u('index.php')) ?>" autocomplete="off">
    <?= csrf_field() ?>
    <input type="hidden" name="action" value="change_password">
    <div class="field">
      <label for="current_password">현재 비밀번호</label>
      <input type="password" id="current_password" name="current_password" required maxlength="200" autocomplete="current-password">
    </div>
    <div class="field">
      <label for="new_password">새 비밀번호</label>
      <input type="password" id="new_password" name="new_password" required minlength="<?= (int)APP_PW_MIN_LEN ?>" maxlength="200" autocomplete="new-password">
      <p class="hint"><?= (int)APP_PW_MIN_LEN ?>자 이상, 영문 대문자·소문자·숫자·특수문자 중 3종류 이상. 아이디 포함 불가.</p>
    </div>
    <div class="field">
      <label for="confirm_password">새 비밀번호 확인</label>
      <input type="password" id="confirm_password" name="confirm_password" required minlength="<?= (int)APP_PW_MIN_LEN ?>" maxlength="200" autocomplete="new-password">
    </div>
    <button class="btn btn-primary" type="submit">비밀번호 변경</button>
  </form>
</section>
</div>

<section class="panel">
  <div class="panel-h"><h2>내 로그인 기록 (최근 20건)</h2></div>
  <?php if ($myLog === []): ?>
    <?= empty_note('로그인 기록이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th>시각</th><th>결과</th><th>IP</th></tr></thead>
      <tbody>
      <?php foreach ($myLog as $r): ?>
        <tr>
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= (int)$r['success'] === 1 ? badge('성공', 'ok') : badge('실패', 'err') ?></td>
          <td class="mono"><?= h($r['ip_addr']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
