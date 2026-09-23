<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 알고리즘 관리 (관리자 전용 쓰기).
 *
 * 이 화면에서 바꿀 수 있는 것은 딱 세 가지뿐이다:
 *   1) algorithm_selection.is_enabled / priority (알고리즘 사용여부 · 우선순위)
 *   2) algorithm_param_value.value (기존에 정의된 파라미터의 "값"만)
 * algorithm(알고리즘 자체)과 algorithm_param_def(파라미터 정의 자체)는 이 웹에서 절대 쓰지 않는다
 * — DB 계정에도 그 두 표의 쓰기 권한이 없다(의도적).
 *
 * 실제 검증(타입 · 범위 · enum) 과 사용여부 강제(risk_guard 잠금)는 모두
 * lib/repo.php 의 repo_update_algorithm_selection() / repo_update_algorithm_params() 안에서
 * 서버사이드로 이뤄진다 — 이 뷰는 그 결과(성공/오류)를 보여주기만 한다.
 */

$algos = repo_algorithms_with_selection();
$codes = array_column($algos, 'code');
$code = clean_enum($_GET['algo'] ?? '', $codes, '');
$algo = $code !== '' ? repo_algorithm_by_code($code) : null;
$params = $algo !== null ? repo_algorithm_params((int)$algo['id']) : [];

$roleLabel = ['entry' => '진입', 'risk' => '리스크관리', 'filter' => '필터'];
$typeLabel = ['int' => '정수', 'decimal' => '소수', 'bool' => '예/아니오', 'string' => '문자열', 'enum' => '선택', 'time' => '시각'];
?>
<section class="panel">
  <div class="panel-h"><h2>알고리즘 관리</h2><span class="muted badge badge-admin">관리자 전용</span></div>
  <p class="note">이 화면에서 바꾼 값은 서버가 <strong>다음 평가 주기</strong>(수십 초~수 분)부터 반영합니다.
    <strong>risk_guard(전역 한도)</strong>는 잠금돼 있어 화면에서 끌 수 없습니다.
    파라미터끼리 서로 맞지 않으면(예: 단기 이동평균 ≥ 장기 이동평균) 서버가 해당 알고리즘을
    자동으로 비활성화하고 경보를 낼 수 있습니다(이 화면은 그 교차검증까지는 재현하지 않습니다).</p>

  <?php if ($algos === []): ?>
    <?= empty_note('등록된 알고리즘이 없습니다.') ?>
  <?php else: ?>
  <form method="post" action="<?= h(u('index.php')) ?>">
    <?= csrf_field() ?>
    <input type="hidden" name="action" value="algo_selection_save">
    <div class="tablewrap">
      <table class="tbl">
        <thead><tr>
          <th>사용</th><th class="num">우선순위</th><th>이름 [코드]</th><th>역할</th><th>설명</th>
          <th>변경자 / 시각</th>
        </tr></thead>
        <tbody>
        <?php foreach ($algos as $a): $locked = (int)$a['is_locked'] === 1; $aid = (int)$a['id']; ?>
          <tr>
            <td>
              <input type="checkbox" name="sel[<?= $aid ?>][enabled]" value="1"
                <?= ((int)$a['is_enabled'] === 1 || $locked) ? ' checked' : '' ?>
                <?= $locked ? ' disabled' : '' ?>>
              <?= $locked ? ' ' . badge('상시 잠금', 'info',
                  '전역 리스크 한도 알고리즘은 항상 사용됩니다 — 이 화면에서 끌 수 없습니다.') : '' ?>
            </td>
            <td class="num">
              <input class="numinput-sm" type="number" min="1" max="999"
                name="sel[<?= $aid ?>][priority]" value="<?= (int)$a['priority'] ?>">
            </td>
            <td>
              <a href="<?= h(url_page('control.algorithms', ['algo' => $a['code']])) ?>#paramform">
                <?= h($a['name']) ?></a>
              <span class="mono sub">[<?= h($a['code']) ?>]</span>
            </td>
            <td><?= h($roleLabel[(string)$a['role']] ?? $a['role']) ?></td>
            <td class="wrap-td"><?= h($a['description']) ?></td>
            <td><?= h($a['updated_by'] ?? '-') ?><br><span class="muted"><?= h(kst($a['updated_at'], 'Y-m-d H:i')) ?></span></td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
    <button class="btn btn-primary" type="submit">사용여부 · 우선순위 저장</button>
  </form>
  <?php endif; ?>
</section>

<section class="panel" id="paramform">
  <div class="panel-h"><h2>파라미터 편집<?= $algo !== null ? ' — ' . h($algo['name']) . ' [' . h($algo['code']) . ']' : '' ?></h2></div>

  <?php if ($algos === []): ?>
    <?= empty_note('등록된 알고리즘이 없습니다.') ?>
  <?php else: ?>
  <form class="filters" method="get" action="<?= h(u('index.php')) ?>">
    <input type="hidden" name="p" value="control.algorithms">
    <span class="fld"><label for="f_algo">알고리즘</label>
      <select id="f_algo" name="algo" data-autosubmit="1">
        <option value="">선택하세요</option>
        <?php foreach ($algos as $a): ?>
          <option value="<?= h($a['code']) ?>"<?= $a['code'] === $code ? ' selected' : '' ?>>
            <?= h($a['name']) ?> (<?= h($a['code']) ?>)<?= (int)$a['is_enabled'] === 1 ? ' · 사용중' : '' ?>
          </option>
        <?php endforeach; ?>
      </select></span>
    <span class="fld fld-btns"><button class="btn btn-sm" type="submit">보기</button></span>
  </form>

  <?php if ($algo === null): ?>
    <?= empty_note('편집할 알고리즘을 선택해 주세요.') ?>
  <?php elseif ($params === []): ?>
    <?= empty_note('이 알고리즘에는 정의된 파라미터가 없습니다.') ?>
  <?php else: ?>
  <p class="note"><?= h($algo['description']) ?> · 파라미터 변경 이력 전체는
    <a href="<?= h(u('index.php?p=strategy.params&algo=' . rawurlencode((string)$algo['code']))) ?>">전략 → 파라미터 · 변경이력</a>
    에서 볼 수 있습니다.</p>
  <form method="post" action="<?= h(u('index.php')) ?>">
    <?= csrf_field() ?>
    <input type="hidden" name="action" value="algo_params_save">
    <input type="hidden" name="algo_id" value="<?= (int)$algo['id'] ?>">
    <div class="paramgrid">
    <?php foreach ($params as $p):
        $key = (string)$p['param_key'];
        $vtype = (string)$p['value_type'];
        $cur = $p['current_value'] ?? $p['default_value'];
        $name = 'params[' . $key . ']';
        $hint = [];
        if (($p['unit'] ?? '') !== '') { $hint[] = (string)$p['unit']; }
        if ($p['min_value'] !== null || $p['max_value'] !== null) {
            $hint[] = ($p['min_value'] ?? '') . ' ~ ' . ($p['max_value'] ?? '');
        }
        $hint[] = '기본값 ' . (string)$p['default_value'];
        ?>
      <div class="paramrow">
        <label for="pf_<?= h($key) ?>"><?= h($p['label']) ?> <span class="mono sub"><?= h($key) ?></span></label>
        <?php if ($vtype === 'bool'): ?>
          <input type="hidden" name="<?= h($name) ?>" value="0">
          <input type="checkbox" id="pf_<?= h($key) ?>" name="<?= h($name) ?>" value="1"
            <?= (string)$cur === '1' ? ' checked' : '' ?>>
        <?php elseif ($vtype === 'enum'): ?>
          <select id="pf_<?= h($key) ?>" name="<?= h($name) ?>">
            <?php foreach (param_enum_choices((string)($p['enum_options'] ?? '')) as [$v, $lab]): ?>
              <option value="<?= h($v) ?>"<?= $v === (string)$cur ? ' selected' : '' ?>><?= h($lab) ?></option>
            <?php endforeach; ?>
          </select>
        <?php elseif ($vtype === 'time'): ?>
          <input type="time" id="pf_<?= h($key) ?>" name="<?= h($name) ?>" value="<?= h((string)$cur) ?>">
        <?php elseif ($vtype === 'int' || $vtype === 'decimal'): ?>
          <input class="numinput-md" type="number"
            <?= $vtype === 'decimal' ? ' step="any"' : ' step="1"' ?>
            <?= $p['min_value'] !== null ? ' min="' . h((string)$p['min_value']) . '"' : '' ?>
            <?= $p['max_value'] !== null ? ' max="' . h((string)$p['max_value']) . '"' : '' ?>
            id="pf_<?= h($key) ?>" name="<?= h($name) ?>" value="<?= h((string)$cur) ?>">
        <?php else: ?>
          <input type="text" maxlength="100" id="pf_<?= h($key) ?>" name="<?= h($name) ?>" value="<?= h((string)$cur) ?>">
        <?php endif; ?>
        <p class="hint"><?= h($typeLabel[$vtype] ?? $vtype) ?> · <?= h(implode(' / ', $hint)) ?><?= ($p['description'] ?? '') !== '' ? ' — ' . h($p['description']) : '' ?></p>
      </div>
    <?php endforeach; ?>
    </div>
    <button class="btn btn-primary" type="submit">파라미터 저장</button>
  </form>
  <?php endif; ?>
  <?php endif; ?>
</section>
