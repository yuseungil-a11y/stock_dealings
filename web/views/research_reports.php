<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 리서치 → 재무분석 리포트 (읽기 전용).
 *
 * Claude 재무분석 리포트(company_analysis_report)가 있는 종목을 종목별 최신 리포트 1건씩 모아
 * 최신 밸류에이션(company_valuation_daily)과 함께 나열한다.
 *
 * 이 화면은 순수 조회 · 연구용이며 계좌 · 주문 · 알고리즘과 전혀 연결되지 않는다.
 * 계좌 선택도 필요 없다(routes 에 'account' 를 지정하지 않음).
 */

$q = clean_text($_GET['q'] ?? '', 40);
$status = clean_enum($_GET['status'] ?? '', FIN_STATUSES, '');
$sort = clean_enum($_GET['sort'] ?? '', array_keys(FIN_SORTS), 'report_desc');
$pageNo = clean_int($_GET['page'] ?? 1, 1, 1, 100000);

/* 범위 필터: 숫자만 통과시키고 값은 전부 바인딩된다. */
$range = [
    'per_min' => clean_num($_GET['per_min'] ?? null, -100000, 100000),
    'per_max' => clean_num($_GET['per_max'] ?? null, -100000, 100000),
    'pbr_min' => clean_num($_GET['pbr_min'] ?? null, -100000, 100000),
    'pbr_max' => clean_num($_GET['pbr_max'] ?? null, -100000, 100000),
    'roe_min' => clean_num($_GET['roe_min'] ?? null, -100000, 100000),
    'roe_max' => clean_num($_GET['roe_max'] ?? null, -100000, 100000),
    'debt_min' => clean_num($_GET['debt_min'] ?? null, -100000, 1000000),
    'debt_max' => clean_num($_GET['debt_max'] ?? null, -100000, 1000000),
];

$available = repo_fin_report_available();
$valAvailable = repo_fin_valuation_available();
$res = $available ? repo_fin_reports($q, $status, $range, $sort, $pageNo)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => FIN_PAGE_SIZE];
$sum = $available ? repo_fin_report_summary($q, $status, $range)
    : ['stocks' => 0, 'ok' => 0, 'error' => 0, 'last_date' => null,
        'avg_per' => null, 'avg_pbr' => null, 'avg_roe' => null, 'avg_debt' => null];

$hasRange = false;
foreach ($range as $v) {
    if ($v !== null) { $hasRange = true; break; }
}
$hasFilter = ($q !== '' || $status !== '' || $hasRange);
$resetKeys = ['status', 'sort', 'per_min', 'per_max', 'pbr_min', 'pbr_max',
    'roe_min', 'roe_max', 'debt_min', 'debt_max'];
?>
<?= research_disclaimer() ?>

<?php if (!$available): ?>
  <?= empty_note('기업 재무분석 리포트 표(company_analysis_report)를 읽을 수 없습니다.',
      '데이터베이스에 해당 표가 없거나 조회 권한이 없습니다. 서버 모듈이 리포트를 생성하면 표시됩니다.') ?>
<?php else: ?>

<section class="cards cards-wide">
  <div class="card"><h2 class="card-t">리포트 종목</h2>
    <p class="card-v"><?= h(nfmt($sum['stocks'])) ?><span class="unit">종목</span></p>
    <p class="card-s">조회 조건 기준 · 종목별 최신 1건</p></div>
  <div class="card"><h2 class="card-t">정상 · 오류</h2>
    <p class="card-v"><?= h(nfmt($sum['ok'])) ?><span class="unit">정상</span></p>
    <p class="card-s">오류 <?= h(nfmt($sum['error'])) ?>건</p></div>
  <div class="card"><h2 class="card-t">최근 리포트일</h2>
    <p class="card-v"><?= $sum['last_date'] === null ? '-' : h(kst($sum['last_date'], 'Y-m-d')) ?></p>
    <p class="card-s">하루 1회 생성</p></div>
  <div class="card"><h2 class="card-t">평균 PER</h2>
    <p class="card-v"><?= $sum['avg_per'] === null ? '-' : h(nfmt($sum['avg_per'], 2)) ?></p>
    <p class="card-s">양수 PER 평균</p></div>
  <div class="card"><h2 class="card-t">평균 PBR</h2>
    <p class="card-v"><?= $sum['avg_pbr'] === null ? '-' : h(nfmt($sum['avg_pbr'], 2)) ?></p>
    <p class="card-s">양수 PBR 평균</p></div>
  <div class="card"><h2 class="card-t">평균 ROE</h2>
    <p class="card-v"><?= $sum['avg_roe'] === null ? '-' : h(nfmt($sum['avg_roe'], 2) . '%') ?></p>
    <p class="card-s">부채비율 평균 <?= $sum['avg_debt'] === null ? '-' : h(nfmt($sum['avg_debt'], 1) . '%') ?></p></div>
</section>

<section class="panel">
  <div class="panel-h"><h2>재무분석 리포트 목록</h2>
    <span class="muted">종목별 <strong>최신 리포트</strong> 1건</span></div>

  <p class="note">종목을 클릭하면 <strong>기업 재무분석</strong> 화면에서 최근 5년 재무제표 · 밸류에이션 추이 ·
    리포트 전문을 볼 수 있습니다. PER · PBR · ROE · 부채비율은
    <span class="mono">company_valuation_daily</span> 의 <strong>가장 최근 계산값</strong>입니다.
    <?php if (!$valAvailable): ?>
      (현재 밸류에이션 표를 읽을 수 없어 지표 칸은 비어 있습니다.)
    <?php endif; ?></p>

  <?= filter_form_open('research.reports') ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
    <?= filter_field_select('status', '리포트 상태', fin_status_options(), $status) ?>
    <?= filter_field_num('per_min', 'PER 이상', $range['per_min'], '예: 0') ?>
    <?= filter_field_num('per_max', 'PER 이하', $range['per_max'], '예: 15') ?>
    <?= filter_field_num('pbr_min', 'PBR 이상', $range['pbr_min'], '예: 0') ?>
    <?= filter_field_num('pbr_max', 'PBR 이하', $range['pbr_max'], '예: 1.5') ?>
    <?= filter_field_num('roe_min', 'ROE(%) 이상', $range['roe_min'], '예: 10') ?>
    <?= filter_field_num('debt_max', '부채비율(%) 이하', $range['debt_max'], '예: 100') ?>
    <?= filter_field_select('sort', '정렬', fin_sort_options(), $sort) ?>
  <?= filter_form_close($resetKeys) ?>

  <?php if ($res['rows'] === []): ?>
    <?= $hasFilter
        ? empty_note('검색 조건에 맞는 리포트가 없습니다.', '종목 · 리포트 상태 · 지표 범위 조건을 바꾸어 다시 조회해 보세요.')
        : empty_note('아직 생성된 재무분석 리포트가 없습니다.',
            '서버 모듈이 DART 재무데이터를 수집하고 Claude 재무분석을 수행하면 이곳에 종목별 리포트가 모입니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>종목</th><th>리포트일</th><th>상태</th><th class="num">현재가</th>
        <th class="num">PER</th><th class="num">PBR</th><th class="num">ROE</th><th class="num">부채비율</th>
        <th>요약 (재무기준)</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <?php
        $stkCd = (string)$r['stk_cd'];
        $nm = fin_display_name($r);
        $isErr = (string)$r['status'] === 'error';
        ?>
        <tr class="<?= $isErr ? 'lv-warn' : '' ?>">
          <td><a href="<?= h(url_page('research.company', ['stk' => $stkCd])) ?>">
              <span class="stk-nm"><?= h($nm) ?></span></a>
            <span class="stk-cd"><?= h($stkCd) ?></span></td>
          <td><?= h(kst($r['as_of_date'], 'Y-m-d')) ?>
            <span class="sub"><?= h((string)$r['model']) ?></span></td>
          <td><?= fin_status_badge((string)$r['status']) ?></td>
          <td class="num"><?= h(money($r['cur_prc'])) ?></td>
          <td class="num"><?= h(fin_multiple($r['per'])) ?></td>
          <td class="num"><?= h(fin_multiple($r['pbr'])) ?></td>
          <td class="num <?= h(sign_class($r['roe'])) ?>"><?= $r['roe'] === null ? '-' : h(nfmt($r['roe'], 2) . '%') ?></td>
          <td class="num"><?= $r['debt_ratio'] === null ? '-' : h(nfmt($r['debt_ratio'], 1) . '%') ?></td>
          <td class="wrap-td"><?php if ($isErr): ?>
              <span class="llm-err"><?= ($r['error_msg'] ?? '') !== '' ? h($r['error_msg']) : '리포트 생성 실패' ?></span>
            <?php elseif (($r['summary'] ?? '') !== ''): ?>
              <?= h($r['summary']) ?>
            <?php else: ?><span class="muted">요약 없음</span><?php endif; ?>
            <?php if (($r['financial_asof'] ?? '') !== ''): ?>
              <div class="algo-meta muted">재무기준 <?= h($r['financial_asof']) ?>
                <?php if ($r['val_dt'] !== null): ?>· <?= h(kst($r['val_dt'], 'Y-m-d')) ?> 계산<?php endif; ?></div>
            <?php endif; ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
<?php endif; ?>
