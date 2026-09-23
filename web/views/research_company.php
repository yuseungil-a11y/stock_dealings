<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 리서치 → 기업 재무분석 (읽기 전용).
 *
 * 종목을 고르면 DART 재무제표(company_financial) 최근 5년, 밸류에이션
 * (company_valuation_daily) 최근값과 추이, Claude 재무분석 리포트
 * (company_analysis_report) 전문을 한 화면에서 보여준다.
 *
 * 이 화면은 순수 조회 · 연구용이며 계좌 · 주문 · 알고리즘과 전혀 연결되지 않는다.
 * 어떤 쓰기도 하지 않으며 계좌 선택도 필요 없다.
 */

$q = clean_text($_GET['q'] ?? '', 40);
$pageNo = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$rdate = clean_date($_GET['rdate'] ?? null);

$dayOptions = ['30' => '최근 30일', '60' => '최근 60일', '90' => '최근 90일',
    '180' => '최근 180일', '365' => '최근 1년'];
$days = (int)clean_enum(is_scalar($_GET['days'] ?? null) ? (string)$_GET['days'] : '',
    array_keys($dayOptions), (string)FIN_TREND_DAYS);

/* 종목코드는 영숫자만 허용한다(값은 이후 prepared statement 바인딩으로만 쓰인다). */
$stkRaw = clean_text($_GET['stk'] ?? '', 12);
$stkCd = preg_match('/^[A-Za-z0-9]{1,12}$/', $stkRaw) === 1 ? $stkRaw : '';

$finAvailable = repo_fin_available();
$valAvailable = repo_fin_valuation_available();
$rptAvailable = repo_fin_report_available();
$anyAvailable = $finAvailable || $rptAvailable;

/* ------------------------------------------------------- 선택한 종목 상세 */
$names = null;
$stmts = [];
$valLatest = null;
$valSeries = [];
$report = null;
$history = [];
$found = false;

if ($stkCd !== '' && $anyAvailable) {
    $names = repo_fin_stock_name($stkCd);
    $stmts = repo_fin_statements($stkCd, FIN_YEARS);
    $valLatest = repo_fin_valuation_latest($stkCd);
    $valSeries = repo_fin_valuation_series($stkCd, $days);
    $history = repo_fin_report_history($stkCd);
    $report = $rdate !== null ? repo_fin_report_by_date($stkCd, $rdate) : repo_fin_report_latest($stkCd);
    $found = ($stmts !== [] || $valLatest !== null || $report !== null || $history !== []);
}

/* PER / PBR 추이 차트 점 */
$perPoints = [];
$pbrPoints = [];
foreach ($valSeries as $v) {
    $label = kst($v['dt'], 'm-d');
    if ($v['per'] !== null && (float)$v['per'] > 0) {
        $perPoints[] = ['label' => $label, 'value' => (float)$v['per']];
    }
    if ($v['pbr'] !== null && (float)$v['pbr'] > 0) {
        $pbrPoints[] = ['label' => $label, 'value' => (float)$v['pbr']];
    }
}
?>
<?= research_disclaimer() ?>

<?php if (!$anyAvailable): ?>
  <?= empty_note('기업 재무 표(company_financial · company_analysis_report)를 읽을 수 없습니다.',
      '데이터베이스에 해당 표가 없거나 조회 권한이 없습니다. 서버 모듈이 데이터를 수집하면 표시됩니다.') ?>

<?php elseif ($stkCd === '' || !$found): ?>
<?php
/* --------------------------------------------------- 종목 선택(검색) 화면 */
$list = repo_fin_stocks($q, $pageNo);
?>
<section class="panel">
  <div class="panel-h"><h2>종목 선택</h2>
    <span class="muted">재무데이터 또는 분석 리포트가 있는 종목</span></div>

  <?php if ($stkCd !== '' && !$found): ?>
    <p class="alert alert-warn">종목코드 <span class="mono"><?= h($stkCd) ?></span> 에 대한 재무데이터 ·
      분석 리포트가 없습니다. 아래 목록에서 종목을 선택해 주세요.</p>
  <?php endif; ?>

  <?= filter_form_open('research.company') ?>
    <?= filter_field_text('q', '종목', $q, '종목코드 또는 종목명') ?>
  <?= filter_form_close(['stk', 'rdate', 'days']) ?>

  <?php if ($list['rows'] === []): ?>
    <?= $q !== ''
        ? empty_note('검색 조건에 맞는 종목이 없습니다.', '종목코드 또는 종목명 일부로 다시 검색해 보세요.')
        : empty_note('아직 수집된 기업 재무데이터가 없습니다.',
            '서버 모듈이 DART 재무제표를 수집하고 Claude 재무분석을 수행하면 이곳에 종목이 나타납니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>종목</th><th>회사명(DART)</th><th class="num">재무 행수</th><th class="num">최근 사업연도</th>
        <th>최근 밸류에이션</th><th>최근 리포트</th><th></th>
      </tr></thead>
      <tbody>
      <?php foreach ($list['rows'] as $r): ?>
        <?php $cd = (string)$r['stk_cd']; ?>
        <tr>
          <td><a href="<?= h(url_page('research.company', ['stk' => $cd])) ?>">
              <span class="stk-nm"><?= h(fin_display_name($r)) ?></span></a>
            <span class="stk-cd"><?= h($cd) ?></span></td>
          <td><?= ($r['corp_name'] ?? '') !== '' ? h($r['corp_name']) : '<span class="muted">-</span>' ?></td>
          <td class="num"><?= h(nfmt($r['fin_cnt'])) ?></td>
          <td class="num"><?= $r['last_year'] === null ? '-' : h((string)(int)$r['last_year']) ?></td>
          <td><?= $r['last_val'] === null ? '<span class="muted">없음</span>' : h(kst($r['last_val'], 'Y-m-d')) ?></td>
          <td><?= $r['last_report'] === null ? '<span class="muted">없음</span>' : h(kst($r['last_report'], 'Y-m-d')) ?></td>
          <td><a class="btn btn-sm" href="<?= h(url_page('research.company', ['stk' => $cd])) ?>">보기</a></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($list) ?>
</section>

<?php else: ?>
<?php
/* ------------------------------------------------------------- 종목 상세 */
$title = fin_display_name([
    'stk_cd' => $stkCd,
    'stk_nm' => $names['stk_nm'] ?? null,
    'corp_name' => $names['corp_name'] ?? null,
]);
?>
<section class="panel">
  <div class="panel-h">
    <h2><?= h($title) ?> <span class="stk-cd"><?= h($stkCd) ?></span></h2>
    <span class="muted">
      <?php if (($names['corp_code'] ?? '') !== ''): ?>
        DART corp_code <span class="mono"><?= h($names['corp_code']) ?></span> ·
      <?php endif; ?>
      <a href="<?= h(url_page('research.company')) ?>">다른 종목 선택</a>
      · <a href="<?= h(url_page('research.reports', ['q' => $stkCd])) ?>">리포트 목록에서 보기</a>
    </span>
  </div>

  <section class="cards cards-wide">
    <div class="card"><h2 class="card-t">현재가</h2>
      <p class="card-v"><?= $valLatest === null ? '-' : h(money($valLatest['cur_prc'])) ?><span class="unit">원</span></p>
      <p class="card-s"><?= $valLatest === null ? '밸류에이션 없음' : h(kst($valLatest['dt'], 'Y-m-d')) . ' 기준' ?></p></div>
    <div class="card"><h2 class="card-t">PER</h2>
      <p class="card-v"><?= $valLatest === null ? '-' : h(fin_multiple($valLatest['per'])) ?></p>
      <p class="card-s">EPS(TTM) <?= $valLatest === null ? '-' : h(money($valLatest['eps_ttm'])) ?></p></div>
    <div class="card"><h2 class="card-t">PBR</h2>
      <p class="card-v"><?= $valLatest === null ? '-' : h(fin_multiple($valLatest['pbr'])) ?></p>
      <p class="card-s">BPS <?= $valLatest === null ? '-' : h(money($valLatest['bps'])) ?></p></div>
    <div class="card"><h2 class="card-t">ROE</h2>
      <p class="card-v <?= $valLatest === null ? '' : h(sign_class($valLatest['roe'])) ?>">
        <?= ($valLatest === null || $valLatest['roe'] === null) ? '-' : h(nfmt($valLatest['roe'], 2) . '%') ?></p>
      <p class="card-s">자기자본이익률</p></div>
    <div class="card"><h2 class="card-t">부채비율</h2>
      <p class="card-v"><?= ($valLatest === null || $valLatest['debt_ratio'] === null) ? '-'
          : h(nfmt($valLatest['debt_ratio'], 1) . '%') ?></p>
      <p class="card-s">부채총계 / 자본총계</p></div>
    <div class="card"><h2 class="card-t">분석 리포트</h2>
      <p class="card-v"><?= $report === null ? '없음' : h(kst($report['as_of_date'], 'Y-m-d')) ?></p>
      <p class="card-s"><?= $report === null ? 'Claude 재무분석 미생성'
          : fin_status_badge((string)$report['status']) . ' <span class="mono">' . h($report['model']) . '</span>' ?></p></div>
  </section>

  <?php if ($valLatest !== null): ?>
    <p class="note">밸류에이션은 <strong><?= h(kst($valLatest['dt'], 'Y-m-d')) ?></strong> 종가 기준으로 계산됐고,
      계산에 쓴 재무데이터 기준은
      <strong><?= ($valLatest['financial_asof'] ?? '') !== '' ? h($valLatest['financial_asof']) : '미기재' ?></strong>
      입니다. 주가는 매일, 재무데이터는 분기 공시 시점에 갱신되므로 두 기준일이 다를 수 있습니다.</p>
  <?php else: ?>
    <p class="alert alert-warn">이 종목의 밸류에이션 계산값(PER/PBR/ROE/부채비율)이 아직 없습니다.
      <?= $valAvailable ? '서버 모듈이 주가와 재무데이터를 결합하면 표시됩니다.' : '밸류에이션 표를 읽을 수 없습니다.' ?></p>
  <?php endif; ?>
</section>

<!-- ------------------------------------------------- 최근 5년 재무제표 -->
<section class="panel">
  <div class="panel-h"><h2>재무제표 (최근 <?= h((string)FIN_YEARS) ?>개 사업연도)</h2>
    <span class="muted">출처: DART 단일회사 주요계정 · 단위 <strong>억원</strong>(EPS는 원)</span></div>

  <?php if (!$finAvailable): ?>
    <?= empty_note('재무제표 표(company_financial)를 읽을 수 없습니다.') ?>
  <?php elseif ($stmts === []): ?>
    <?= empty_note('이 종목의 재무제표가 아직 수집되지 않았습니다.',
        '서버 모듈이 DART OpenAPI 에서 분기 · 사업보고서를 받아오면 이곳에 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>사업연도</th><th>보고서</th>
        <th class="num">매출액</th><th class="num">영업이익</th><th class="num">당기순이익</th>
        <th class="num">자산총계</th><th class="num">부채총계</th><th class="num">자본총계</th>
        <th class="num">EPS(원)</th><th class="num">영업활동현금흐름</th><th>수집시각</th>
      </tr></thead>
      <tbody>
      <?php foreach ($stmts as $s): ?>
        <tr class="<?= (string)$s['reprt_code'] === '11011' ? 'row-changed' : '' ?>">
          <td><strong><?= h((string)(int)$s['bsns_year']) ?></strong></td>
          <td><?= fin_reprt_badge($s['reprt_code'] === null ? null : (string)$s['reprt_code']) ?></td>
          <td class="num"><?= h(fin_eok($s['revenue'])) ?></td>
          <td class="num <?= h(sign_class($s['operating_profit'])) ?>"><?= h(fin_eok($s['operating_profit'])) ?></td>
          <td class="num <?= h(sign_class($s['net_profit'])) ?>"><?= h(fin_eok($s['net_profit'])) ?></td>
          <td class="num"><?= h(fin_eok($s['total_assets'])) ?></td>
          <td class="num"><?= h(fin_eok($s['total_liabilities'])) ?></td>
          <td class="num"><?= h(fin_eok($s['total_equity'])) ?></td>
          <td class="num"><?= h(money($s['eps'])) ?></td>
          <td class="num <?= h(sign_class($s['operating_cash_flow'])) ?>"><?= h(fin_eok($s['operating_cash_flow'])) ?></td>
          <td><span class="sub"><?= h(kst($s['fetched_at'], 'Y-m-d H:i')) ?></span></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <p class="hint">금액은 억원 단위로 반올림해 표시합니다(원 단위 원본값은 DART 공시 기준).
    공시되지 않았거나 수집하지 못한 항목은 <span class="mono">-</span> 로 둡니다.
    발행주식수 등 상세는 아래 원본 값에서 확인하세요.</p>
  <details class="jsum"><summary>원본 값(원 단위) 보기</summary>
    <div class="tablewrap">
      <table class="tbl">
        <thead><tr><th>사업연도</th><th>보고서</th><th class="num">매출액</th><th class="num">영업이익</th>
          <th class="num">당기순이익</th><th class="num">자산총계</th><th class="num">부채총계</th>
          <th class="num">자본총계</th><th class="num">발행주식수</th></tr></thead>
        <tbody>
        <?php foreach ($stmts as $s): ?>
          <tr>
            <td><?= h((string)(int)$s['bsns_year']) ?></td>
            <td><?= h(fin_reprt_label($s['reprt_code'] === null ? null : (string)$s['reprt_code'])) ?></td>
            <td class="num"><?= h(money($s['revenue'])) ?></td>
            <td class="num"><?= h(money($s['operating_profit'])) ?></td>
            <td class="num"><?= h(money($s['net_profit'])) ?></td>
            <td class="num"><?= h(money($s['total_assets'])) ?></td>
            <td class="num"><?= h(money($s['total_liabilities'])) ?></td>
            <td class="num"><?= h(money($s['total_equity'])) ?></td>
            <td class="num"><?= h(money($s['shares_outstanding'])) ?></td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
  </details>
  <?php endif; ?>
</section>

<!-- ------------------------------------------------- 밸류에이션 추이 -->
<section class="panel">
  <div class="panel-h"><h2>PER · PBR 추이</h2>
    <form class="filters filters-inline" method="get" action="<?= h(u('index.php')) ?>">
      <input type="hidden" name="p" value="research.company">
      <input type="hidden" name="stk" value="<?= h($stkCd) ?>">
      <?= filter_field_select('days', '기간', $dayOptions, (string)$days) ?>
      <span class="fld fld-btns"><button class="btn btn-sm" type="submit">적용</button></span>
    </form>
  </div>

  <?php if (!$valAvailable): ?>
    <?= empty_note('밸류에이션 표(company_valuation_daily)를 읽을 수 없습니다.') ?>
  <?php elseif ($valSeries === []): ?>
    <?= empty_note('선택한 기간에 계산된 밸류에이션이 없습니다.', '기간을 넓혀 보거나 서버 모듈의 수집을 기다려 주세요.') ?>
  <?php else: ?>
    <div class="grid2">
      <div>
        <h3 class="card-t">PER</h3>
        <?php if ($perPoints === []): ?>
          <p class="tl-empty">양수 PER 이 없어 그래프를 그리지 않았습니다(적자 또는 미산출).</p>
        <?php else: ?>
          <div class="chartwrap"><?= svg_line_chart($perPoints, 'PER 추이', 720, 220, 2) ?></div>
        <?php endif; ?>
      </div>
      <div>
        <h3 class="card-t">PBR</h3>
        <?php if ($pbrPoints === []): ?>
          <p class="tl-empty">양수 PBR 이 없어 그래프를 그리지 않았습니다.</p>
        <?php else: ?>
          <div class="chartwrap"><?= svg_line_chart($pbrPoints, 'PBR 추이', 720, 220, 2) ?></div>
        <?php endif; ?>
      </div>
    </div>
    <details class="jsum"><summary>일별 값 표로 보기 (<?= h(nfmt(count($valSeries))) ?>일)</summary>
      <div class="tablewrap">
        <table class="tbl">
          <thead><tr><th>일자</th><th class="num">종가</th><th class="num">PER</th><th class="num">PBR</th>
            <th class="num">ROE</th><th class="num">부채비율</th><th class="num">EPS(TTM)</th>
            <th class="num">BPS</th><th>재무기준</th></tr></thead>
          <tbody>
          <?php foreach (array_reverse($valSeries) as $v): ?>
            <tr>
              <td><?= h(kst($v['dt'], 'Y-m-d')) ?></td>
              <td class="num"><?= h(money($v['cur_prc'])) ?></td>
              <td class="num"><?= h(fin_multiple($v['per'])) ?></td>
              <td class="num"><?= h(fin_multiple($v['pbr'], 4)) ?></td>
              <td class="num <?= h(sign_class($v['roe'])) ?>"><?= $v['roe'] === null ? '-' : h(nfmt($v['roe'], 2) . '%') ?></td>
              <td class="num"><?= $v['debt_ratio'] === null ? '-' : h(nfmt($v['debt_ratio'], 1) . '%') ?></td>
              <td class="num"><?= h(money($v['eps_ttm'])) ?></td>
              <td class="num"><?= h(money($v['bps'])) ?></td>
              <td><?= ($v['financial_asof'] ?? '') !== '' ? h($v['financial_asof']) : '<span class="muted">-</span>' ?></td>
            </tr>
          <?php endforeach; ?>
          </tbody>
        </table>
      </div>
    </details>
  <?php endif; ?>
</section>

<!-- ------------------------------------------------- Claude 재무분석 리포트 -->
<section class="panel">
  <div class="panel-h"><h2>Claude 재무분석 리포트</h2>
    <span class="muted">참고용 — 매매를 실행하지 않습니다</span></div>

  <?php if (!$rptAvailable): ?>
    <?= empty_note('분석 리포트 표(company_analysis_report)를 읽을 수 없습니다.') ?>
  <?php elseif ($report === null): ?>
    <?= empty_note('아직 분석 리포트가 생성되지 않았습니다.',
        $rdate !== null
            ? '선택한 날짜에 이 종목의 리포트가 없습니다. 아래 이력에서 다른 날짜를 선택해 주세요.'
            : '서버 모듈이 이 종목의 재무데이터를 Claude 로 분석하면 이곳에 리포트 전문이 표시됩니다.') ?>
  <?php else: ?>
    <p class="llmline">
      <span class="llmline-t"><?= h(kst($report['as_of_date'], 'Y-m-d')) ?> 리포트</span>
      <?= fin_status_badge((string)$report['status']) ?>
      <span class="mono"><?= h($report['model']) ?></span>
      <span class="sub">토큰 입력 <?= h(nfmt($report['input_tokens'])) ?> · 출력 <?= h(nfmt($report['output_tokens'])) ?>
        · 지연 <?= h(nfmt($report['latency_ms'])) ?>ms · 생성 <?= h(kst($report['created_at'])) ?></span>
      <?php if ($rdate !== null): ?>
        <a class="btn btn-sm" href="<?= h(url_page('research.company', ['stk' => $stkCd])) ?>">최신 리포트 보기</a>
      <?php endif; ?>
    </p>

    <?php if ((string)$report['status'] === 'error'): ?>
      <p class="alert alert-err">이 날짜의 리포트 생성이 실패했습니다<?= ($report['error_msg'] ?? '') !== ''
          ? ' — ' . h($report['error_msg']) : '' ?>.</p>
    <?php endif; ?>

    <?php if (($report['summary'] ?? '') !== ''): ?>
      <p class="rd-k">한줄 요약</p>
      <p class="reportsum"><?= h($report['summary']) ?></p>
    <?php endif; ?>

    <p class="rd-k">리포트 전문</p>
    <?= fin_report_block($report['report_text']) ?>

    <p class="hint">위 내용은 공시 재무데이터를 바탕으로 생성한 <strong>참고 자료</strong>입니다.
      자동매매 알고리즘은 이 리포트를 읽지 않으며, 이 화면에서 주문이 나가는 일도 없습니다.</p>
  <?php endif; ?>

  <?php if ($rptAvailable && count($history) > 1): ?>
    <h3 class="card-t">리포트 이력</h3>
    <div class="tablewrap">
      <table class="tbl">
        <thead><tr><th>리포트일</th><th>상태</th><th>모델</th><th>요약 · 오류</th>
          <th class="num">토큰(입력/출력)</th><th class="num">지연</th><th></th></tr></thead>
        <tbody>
        <?php foreach ($history as $hrow): ?>
          <?php $cur = ($report !== null && (int)$hrow['id'] === (int)$report['id']); ?>
          <tr class="<?= $cur ? 'row-changed' : '' ?>">
            <td><?= h(kst($hrow['as_of_date'], 'Y-m-d')) ?><?= $cur ? ' <span class="sub">현재 보는 중</span>' : '' ?></td>
            <td><?= fin_status_badge((string)$hrow['status']) ?></td>
            <td class="mono"><?= h($hrow['model']) ?></td>
            <td class="wrap-td"><?php if ((string)$hrow['status'] === 'error'): ?>
                <span class="llm-err"><?= h($hrow['error_msg'] ?? '오류') ?></span>
              <?php else: ?><?= ($hrow['summary'] ?? '') !== '' ? h($hrow['summary']) : '<span class="muted">-</span>' ?><?php endif; ?></td>
            <td class="num"><?= h(nfmt($hrow['input_tokens'])) ?> / <?= h(nfmt($hrow['output_tokens'])) ?></td>
            <td class="num"><?= h(nfmt($hrow['latency_ms'])) ?>ms</td>
            <td><a class="btn btn-sm" href="<?= h(url_page('research.company',
                ['stk' => $stkCd, 'rdate' => (string)$hrow['as_of_date']])) ?>">보기</a></td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
  <?php endif; ?>
</section>
<?php endif; ?>
