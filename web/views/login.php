<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?string $loginError @var string $nextParam @var ?string $expiredParam @var bool $bye */
?>
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>로그인 · <?= h(APP_NAME) ?></title>
<link rel="icon" href="<?= h(u('assets/img/favicon.svg')) ?>" type="image/svg+xml">
<link rel="stylesheet" href="<?= h(u('assets/css/app.css')) ?>">
</head>
<body class="loginpage" data-base="<?= h(u('')) ?>">
<main class="loginbox">
  <h1 class="login-title"><span class="brand-mark" aria-hidden="true"></span><?= h(APP_NAME) ?></h1>
  <p class="login-sub">조회 · 관제 전용 시스템</p>

  <?php if (!empty($loginError)): ?>
    <p class="alert alert-err" role="alert"><?= h($loginError) ?></p>
  <?php elseif (($expiredParam ?? null) === 'idle'): ?>
    <p class="alert alert-warn">일정 시간 활동이 없어 자동 로그아웃되었습니다. 다시 로그인해 주세요.</p>
  <?php elseif (($expiredParam ?? null) === 'absolute'): ?>
    <p class="alert alert-warn">세션 유효시간이 만료되었습니다. 다시 로그인해 주세요.</p>
  <?php elseif (!empty($bye)): ?>
    <p class="alert alert-ok">로그아웃되었습니다.</p>
  <?php endif; ?>

  <form method="post" action="<?= h(u('index.php')) ?>" autocomplete="off">
    <?= csrf_field() ?>
    <input type="hidden" name="action" value="login">
    <input type="hidden" name="next" value="<?= h($nextParam ?? 'dashboard') ?>">
    <div class="field">
      <label for="username">아이디</label>
      <input type="text" id="username" name="username" maxlength="50" required autocapitalize="off"
             autocorrect="off" spellcheck="false" autocomplete="username">
    </div>
    <div class="field">
      <label for="password">비밀번호</label>
      <input type="password" id="password" name="password" maxlength="200" required autocomplete="current-password">
    </div>
    <button class="btn btn-primary btn-block" type="submit">로그인</button>
  </form>
  <p class="login-note">로그인 시도는 기록되며, 연속 <?= (int)APP_MAX_FAILED ?>회 실패 시 <?= (int)APP_LOCK_MINUTES ?>분간 잠깁니다.</p>
</main>
<script src="<?= h(u('assets/js/app.js')) ?>"></script>
</body>
</html>
