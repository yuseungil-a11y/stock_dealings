<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$algos = repo_algorithms();
$codes = array_column($algos, 'code');
$code = clean_enum($_GET['algo'] ?? '', $codes, $codes[0] ?? '');
$algo = $code !== '' ? repo_algorithm_by_code($code) : null;
$params = $algo !== null ? repo_params((int)$algo['id']) : [];
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$hist = $algo !== null ? repo_param_history((int)$algo['id'], $page)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => 20];

$typeLabel = ['int' => '정수', 'decimal' => '소수', 'bool' => '예/아니오', 'string' => '문자열', 'enum' => '선택', 'time' => '시각'];

/** enum 옵션 'v:라벨,v:라벨' 에서 현재값의 라벨을 찾는다. */
function param_display(array $p): string
{
    $v = $p['current_value'] ?? $p['default_value'];
    $v = (string)$v;
    if ($p['value_type'] === 'bool') {
        return $v === '1' ? '예' : '아니오';
    }
    if ($p['value_type'] === 'enum' && !empty($p['enum_options'])) {
        foreach (explode(',', (string)$p['enum_options']) as $opt) {
            $parts = explode(':', $opt, 2);
            if (trim($parts[0]) === $v) {
                return trim($parts[1] ?? $v);
            }
        }
    }
    return $v === '' ? '(빈값)' : $v;
}
?>
<section class="panel">
  <div class="panel-h"><h2>알고리즘 파라미터</h2></div>
  <?php if ($algos === []): ?>
    <?= empty_note('등록된 알고리즘이 없습니다.') ?>
  <?php else: ?>
  <form class="filters" method="get" action="<?= h(u('index.php')) ?>">
    <input type="hidden" name="p" value="strategy.params">
    <span class="fld"><label for="f_algo">알고리즘</label>
      <select id="f_algo" name="algo" data-autosubmit="1">
        <?php foreach ($algos as $a): ?>
          <option value="<?= h($a['code']) ?>"<?= $a['code'] === $code ? ' selected' : '' ?>>
            <?= h($a['name']) ?> (<?= h($a['code']) ?>)<?= (int)$a['is_enabled'] === 1 ? ' · 사용중' : '' ?>
          </option>
        <?php endforeach; ?>
      </select></span>
    <span class="fld fld-btns"><button class="btn btn-primary btn-sm" type="submit">조회</button></span>
  </form>

  <?php if ($algo === null): ?>
    <?= empty_note('알고리즘을 선택해 주세요.') ?>
  <?php else: ?>
  <p class="note"><?= h($algo['description']) ?></p>
  <?php if ($params === []): ?>
    <?= empty_note('정의된 파라미터가 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>항목</th><th>키</th><th>타입</th><th class="num">현재값</th><th class="num">기본값</th>
        <th>범위</th><th>단위</th><th>설명</th><th>변경자 / 시각</th>
      </tr></thead>
      <tbody>
      <?php foreach ($params as $p):
          $changed = ((string)($p['current_value'] ?? '')) !== ((string)$p['default_value']); ?>
        <tr class="<?= $changed ? 'row-changed' : '' ?>">
          <td><?= h($p['label']) ?></td>
          <td class="mono"><?= h($p['param_key']) ?></td>
          <td><?= h($typeLabel[(string)$p['value_type']] ?? $p['value_type']) ?></td>
          <td class="num"><strong><?= h(param_display($p)) ?></strong><?= $changed ? ' ' . badge('변경됨', 'warn') : '' ?></td>
          <td class="num muted"><?= h($p['default_value']) ?></td>
          <td class="num"><?= ($p['min_value'] === null && $p['max_value'] === null) ? '-'
              : h(($p['min_value'] ?? '') . ' ~ ' . ($p['max_value'] ?? '')) ?></td>
          <td><?= h($p['unit']) ?></td>
          <td class="wrap-td"><?= h($p['description']) ?></td>
          <td><?= h($p['updated_by'] ?? '-') ?><br><span class="muted"><?= h(kst($p['updated_at'], 'Y-m-d H:i')) ?></span></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?php endif; ?>
  <?php endif; ?>
</section>

<section class="panel">
  <div class="panel-h"><h2>파라미터 변경 이력<?= $algo !== null ? ' — ' . h($algo['name']) : '' ?></h2></div>
  <?php if ($hist['rows'] === []): ?>
    <?= empty_note('변경 이력이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th>변경시각</th><th>파라미터</th><th class="num">이전값</th><th class="num">변경값</th><th>변경자</th></tr></thead>
      <tbody>
      <?php foreach ($hist['rows'] as $r): ?>
        <tr>
          <td><?= h(kst($r['changed_at'])) ?></td>
          <td class="mono"><?= h($r['param_key']) ?></td>
          <td class="num muted"><?= h($r['old_value'] ?? '-') ?></td>
          <td class="num"><strong><?= h($r['new_value']) ?></strong></td>
          <td><?= h($r['changed_by'] ?? '-') ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($hist) ?>
</section>
