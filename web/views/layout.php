<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var string $content @var string $page @var array $route @var array $accounts @var ?array $account */
$menu = app_menu();
$isAdmin = auth_is_admin();
$statusRows = [];
try {
    $statusRows = repo_server_status();
} catch (Throwable $e) {
    $statusRows = [];
}
$worst = 'unknown';
$rank = ['ok' => 0, 'unknown' => 1, 'warn' => 2, 'error' => 3];
foreach ($statusRows as $srow) {
    $eff = repo_effective_status($srow)['status'];
    if (($rank[$eff] ?? 1) > ($rank[$worst] ?? 1)) {
        $worst = $eff;
    }
}
?>
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title><?= h($route['title']) ?> · <?= h(APP_NAME) ?></title>
<?= favicon_links() ?>
<link rel="stylesheet" href="<?= h(asset_url('assets/css/app.css')) ?>">
</head>
<body data-base="<?= h(u('')) ?>" data-page="<?= h($page) ?>">
<a class="skip" href="#main">본문 바로가기</a>
<header class="topbar">
  <div class="topbar-in">
    <a class="brand" href="<?= h(u('index.php?p=dashboard')) ?>">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-txt"><?= h(APP_NAME) ?></span>
    </a>
    <button class="navtoggle" type="button" id="navtoggle" aria-expanded="false" aria-controls="mainnav">
      <span aria-hidden="true">☰</span><span class="sr-only">메뉴 열기</span>
    </button>
    <nav class="mainnav" id="mainnav" aria-label="주메뉴">
      <ul class="menu">
      <?php foreach ($menu as $gkey => $g): ?>
        <?php
        $items = [];
        foreach ($g['items'] as $pk => $plabel) {
            if (!empty(app_routes()[$pk]['admin']) && !$isAdmin) {
                continue;
            }
            $items[$pk] = $plabel;
        }
        if ($items === []) { continue; }
        $active = ($route['group'] ?? '') === $gkey;
        ?>
        <li class="menu-item<?= $active ? ' is-active' : '' ?>">
          <details class="dd"<?= $active ? ' open' : '' ?>>
            <summary><?= h($g['label']) ?></summary>
            <ul class="dd-list">
            <?php foreach ($items as $pk => $plabel): ?>
              <li><a class="<?= $pk === $page ? 'cur' : '' ?>" href="<?= h(u('index.php?p=' . $pk)) ?>"><?= h($plabel) ?></a></li>
            <?php endforeach; ?>
            </ul>
          </details>
        </li>
      <?php endforeach; ?>
      </ul>
    </nav>
    <div class="topbar-right">
      <a class="sysdot <?= h(status_class($worst)) ?>" href="<?= h(u('index.php?p=system.status')) ?>"
         title="서버 상태: <?= h(status_label($worst)) ?>">
        <span class="dot" aria-hidden="true"></span><span class="sysdot-txt"><?= h(status_label($worst)) ?></span>
      </a>
      <span class="who"><?= h($user['display_name'] !== '' ? $user['display_name'] : $user['username']) ?><?php if ($isAdmin): ?><span class="badge badge-admin">admin</span><?php endif; ?></span>
      <form class="logout" method="post" action="<?= h(u('index.php')) ?>">
        <?= csrf_field() ?>
        <input type="hidden" name="action" value="logout">
        <button class="btn btn-ghost" type="submit">로그아웃</button>
      </form>
      <span class="ver" title="운영 웹 v<?= h(STOCK_WEB_VERSION) ?> (<?= h(STOCK_WEB_RELEASED) ?>)"><?= h(app_version_text($statusRows)) ?></span>
    </div>
  </div>
</header>

<main id="main" class="wrap">
  <div class="pagehead">
    <h1><?= h($route['title']) ?></h1>
    <?php if (!empty($route['account'])): ?>
      <form class="acctsel" method="get" action="<?= h(u('index.php')) ?>">
        <input type="hidden" name="p" value="<?= h($page) ?>">
        <label for="account_id">계좌</label>
        <select id="account_id" name="account_id" data-autosubmit="1">
          <?php if ($accounts === []): ?>
            <option value="0">등록된 계좌 없음</option>
          <?php endif; ?>
          <?php foreach ($accounts as $a): ?>
            <option value="<?= h($a['id']) ?>"<?= ($account !== null && (int)$a['id'] === (int)$account['id']) ? ' selected' : '' ?>>
              <?= h(repo_account_label($a)) ?>
            </option>
          <?php endforeach; ?>
        </select>
        <button class="btn btn-sm" type="submit">적용</button>
      </form>
    <?php endif; ?>
  </div>

  <?php if (!empty($notFound)): ?>
    <p class="alert alert-err">요청한 페이지를 찾을 수 없어 종합현황을 표시합니다.</p>
  <?php endif; ?>
  <?php if (!empty($flash)): ?>
    <?php $flashCls = in_array($flashType, ['ok', 'warn', 'err'], true) ? $flashType : 'err'; ?>
    <p class="alert alert-<?= h($flashCls) ?>"><?= h($flash) ?></p>
  <?php endif; ?>

  <?= $content ?>
</main>

<footer class="foot">
  <span>표시 시각 기준: KST (<?= h(date('Y-m-d H:i:s')) ?>)</span>
  <span>조회 전용 화면 — 트레이딩 설정은 서버 프로그램에서만 변경됩니다.</span>
</footer>
<script src="<?= h(asset_url('assets/js/app.js')) ?>"></script>
</body>
</html>
