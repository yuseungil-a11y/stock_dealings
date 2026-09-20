<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 전략 → Claude 판단 (읽기 전용).
 * 서버의 Claude 거부권 필터가 llm_decision_log 에 남긴 판단 기록을 조회한다.
 */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$q = clean_text($_GET['q'] ?? '', 40);
$result = clean_enum($_GET['result'] ?? '', LLM_RESULTS, '');
$page = clean_int($_GET['page'] ?? 1, 1, 1, 100000);

$available = repo_llm_available();
$sum = repo_llm_summary();
$res = repo_llm_decisions($from, $to, $q, $result, $page);
$hasFilter = ($from !== null || $to !== null || $q !== '' || $result !== '');
?>
<?php if (!$available): ?>
  <?= empty_note('Claude 판단 기록 표를 읽을 수 없습니다.', '데이터베이스에 llm_decision_log 표가 없거나 조회 권한이 없습니다.') ?>
<?php else: ?>

<section class="cards cards-wide">
  <div class="card"><h2 class="card-t">오늘 호출 수</h2>
    <p class="card-v"><?= h(nfmt($sum['calls'] ?? 0)) ?><span class="unit">건</span></p>
    <p class="card-s">전체 <?= h(nfmt($sum['total'] ?? 0)) ?>건</p></div>
  <div class="card"><h2 class="card-t">오늘 차단</h2>
    <p class="card-v <?= ((int)($sum['blocks'] ?? 0)) > 0 ? 'v-up' : '' ?>"><?= h(nfmt($sum['blocks'] ?? 0)) ?><span class="unit">건</span></p>
    <p class="card-s">최종동작 block</p></div>
  <div class="card"><h2 class="card-t">오늘 오류</h2>
    <p class="card-v"><?= h(nfmt($sum['errors'] ?? 0)) ?><span class="unit">건</span></p>
    <p class="card-s">호출 실패 · 무효 응답</p></div>
  <div class="card"><h2 class="card-t">캐시 적중</h2>
    <p class="card-v"><?= h(nfmt($sum['cache_hits'] ?? 0)) ?><span class="unit">건</span></p>
    <p class="card-s">오늘 호출 중 재사용</p></div>
  <div class="card"><h2 class="card-t">오늘 토큰 합</h2>
    <p class="card-v"><?= h(nfmt($sum['input_tokens'] ?? 0)) ?><span class="unit">입력</span></p>
    <p class="card-s">출력 <?= h(nfmt($sum['output_tokens'] ?? 0)) ?></p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2>Claude 판단 기록</h2></div>
  <p class="note">서버의 <strong>Claude 거부권 필터</strong>가 남긴 판단입니다.
    <strong>판단</strong>은 모델의 응답(허용/차단/오류), <strong>최종동작</strong>은 신뢰도·fail_mode 를 적용한
    파이프라인의 실제 결과입니다. 이 화면은 조회 전용이며 설정을 변경하지 않습니다.</p>

  <?= filter_form_open('strategy.claude') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
    <?= filter_field_select('result', '결과', ['' => '전체', 'pass' => '통과(pass)', 'block' => '차단(block)', 'error' => '오류(error)'], $result) ?>
  <?= filter_form_close() ?>

  <?php if ($res['rows'] === []): ?>
    <?= $hasFilter
        ? empty_note('검색 조건에 맞는 판단 기록이 없습니다.', '기간·종목·결과 조건을 바꾸어 다시 조회해 보세요.')
        : empty_note('Claude 검토가 꺼져 있거나 아직 판단 기록이 없습니다.',
            '서버 모듈에서 Claude 거부권 필터가 동작하면 이곳에 판단이 기록됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>시각</th><th>판단</th><th>최종동작</th><th>종목</th><th>구분</th>
        <th>알고리즘</th><th>모델</th><th class="num">신뢰도</th>
        <th>근거 · 리스크 · 입력요약</th>
        <th>캐시</th><th class="num">지연</th><th class="num">토큰(입력/출력)</th>
        <th class="num">주문</th><th class="num">실행 ID</th><th class="num">ID</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <?php $isErr = (string)$r['decision'] === 'error'; ?>
        <tr class="<?= $isErr ? 'lv-warn' : ((string)$r['final_action'] === 'block' ? 'lv-error' : '') ?>">
          <td><?= h(kst($r['created_at'])) ?></td>
          <td><?= llm_decision_badge((string)$r['decision']) ?></td>
          <td><?= llm_action_badge((string)$r['final_action']) ?></td>
          <td><span class="stk-nm"><?= h($r['stk_nm']) ?></span> <span class="stk-cd"><?= h($r['stk_cd']) ?></span></td>
          <td><?= side_badge((string)$r['side']) ?></td>
          <td class="mono"><?= h($r['source_algo'] ?? '-') ?></td>
          <td class="mono"><?= h($r['model']) ?></td>
          <td class="num"><?= $r['confidence'] === null ? '-' : h(nfmt($r['confidence']) . '%') ?></td>
          <td class="wrap-td">
            <?php if (($r['reasons'] ?? '') !== ''): ?>
              <div class="llm-reason"><?= h($r['reasons']) ?></div>
            <?php endif; ?>
            <?php if (($r['risk_flags'] ?? '') !== ''): ?>
              <div class="llm-flags">리스크: <?= h($r['risk_flags']) ?></div>
            <?php endif; ?>
            <?php if (($r['error_msg'] ?? '') !== ''): ?>
              <div class="llm-err">오류: <?= h($r['error_msg']) ?></div>
            <?php endif; ?>
            <?php if (($r['input_summary'] ?? '') !== ''): ?>
              <details class="jsum"><summary>입력 요약(JSON)</summary><pre><?= h($r['input_summary']) ?></pre></details>
            <?php endif; ?>
            <?php if (($r['reasons'] ?? '') === '' && ($r['risk_flags'] ?? '') === ''
                && ($r['error_msg'] ?? '') === '' && ($r['input_summary'] ?? '') === ''): ?>
              <span class="muted">-</span>
            <?php endif; ?>
          </td>
          <td><?= ((int)$r['from_cache'] === 1) ? badge('적중', 'info') : badge('호출', 'muted') ?></td>
          <td class="num"><?= $r['latency_ms'] === null ? '-' : h(nfmt($r['latency_ms']) . 'ms') ?></td>
          <td class="num"><?= h(nfmt($r['input_tokens'] ?? 0)) ?> / <?= h(nfmt($r['output_tokens'] ?? 0)) ?></td>
          <td class="num"><?= h($r['order_id'] ?? '-') ?></td>
          <td class="num"><?= h($r['run_id'] ?? '-') ?></td>
          <td class="num"><?= h($r['id']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
<?php endif; ?>
