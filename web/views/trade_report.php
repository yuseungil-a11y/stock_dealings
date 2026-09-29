<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 거래 → 종합분석 리포트 (Claude, 참고용).
 *
 * 기간(기본 최근 7일, 사용자가 선택 가능)의 매매 기록을 Claude 가 분석해 거래별 손익 원인과
 * 공통 패턴을 설명한 리포트(`trade_analysis_report`)를 조회한다.
 *
 * 이 화면의 유일한 쓰기는 관리자의 "종합분석 요청" 버튼이며, 그것도 `trade_analysis_request`
 * 에 요청 한 줄을 남기는 것이 전부다(주문 · 설정 · 알고리즘은 건드리지 않는다).
 * 조회는 로그인한 누구나 가능하다(trend_scan 과 동일한 조회/요청 권한 분리).
 *
 * @var ?int $accountId
 */

$defaultEnd = date('Y-m-d');
$defaultStart = date('Y-m-d', strtotime('-' . (TAREP_DEFAULT_DAYS - 1) . ' days'));
$from = clean_date($_GET['from'] ?? null) ?? $defaultStart;
$to = clean_date($_GET['to'] ?? null) ?? $defaultEnd;
$pageNo = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
// 종합분석 요청 결과 배너(리다이렉트로 전달, 화이트리스트).
$rq = clean_enum($_GET['rq'] ?? '', ['ok', 'busy', 'err', 'range'], '');

$reportAvailable = repo_tarep_report_available();
$res = ($accountId !== null && $reportAvailable)
    ? repo_tarep_reports($accountId, $from, $to, $pageNo)
    : ['rows' => [], 'total' => 0, 'page' => 1, 'pages' => 1, 'size' => TAREP_PAGE_SIZE];

$isAdmin = auth_is_admin();
$pending = $accountId !== null ? repo_tarep_pending($accountId) : null;
$lastReqAt = (int)($_SESSION['tarep_req_at'] ?? 0);
$cooldownLeft = $lastReqAt > 0 ? max(0, TAREP_REQUEST_COOLDOWN_SEC - (time() - $lastReqAt)) : 0;
$requestLocked = ($cooldownLeft > 0 || $pending !== null);
?>
<?= trade_report_disclaimer() ?>

<?php if ($accountId === null): ?>
  <?= empty_note('선택된 계좌가 없습니다.', '계좌를 먼저 등록해야 종합분석 리포트를 조회 · 요청할 수 있습니다.') ?>
<?php else: ?>

<?php if ($isAdmin): ?>
<section class="panel">
  <div class="panel-h"><h2>종합분석 요청</h2><span class="muted">관리자 전용</span></div>

  <?php if ($rq === 'ok'): ?>
    <p class="alert alert-ok">종합분석을 요청했습니다. 서버가 확인 후 처리합니다(보통 1~2분,
      거래가 많은 기간은 더 걸릴 수 있음). 이 페이지를 새로고침하면 아래 이력에서 결과를 볼 수 있습니다.</p>
  <?php elseif ($rq === 'busy'): ?>
    <p class="alert alert-warn">요청 처리 중입니다. 앞선 요청이 끝난 뒤에 다시 시도해 주세요.</p>
  <?php elseif ($rq === 'range'): ?>
    <p class="alert alert-err">조회 기간이 너무 넓습니다(최대 <?= h(nfmt(TAREP_MAX_RANGE_DAYS)) ?>일).
      기간을 좁혀 다시 시도해 주세요.</p>
  <?php elseif ($rq === 'err'): ?>
    <p class="alert alert-err">종합분석 요청을 접수하지 못했습니다. 기간을 확인하고 잠시 후 다시 시도해 주세요.</p>
  <?php endif; ?>

  <div class="rescanbox">
    <div class="rescanbox-h">
      <h3>선택한 기간(<?= h($from) ?> ~ <?= h($to) ?>)으로 종합분석 요청</h3>
      <form class="rescanform" method="post" action="<?= h(u('index.php')) ?>">
        <?= csrf_field() ?>
        <input type="hidden" name="action" value="trade_report_request">
        <input type="hidden" name="period_start" value="<?= h($from) ?>">
        <input type="hidden" name="period_end" value="<?= h($to) ?>">
        <button class="btn btn-primary" type="submit"<?= $requestLocked ? ' disabled' : '' ?>>종합분석 요청</button>
      </form>
    </div>
    <?php if ($requestLocked): ?>
      <p class="rescanbox-s">요청 처리 중입니다 —
        <?php if ($pending !== null): ?>
          <?= tarep_request_status_badge((string)$pending['status']) ?>
          <?= h(kst($pending['requested_at'], 'H:i:s')) ?> 요청
          (<?= h((string)$pending['period_start']) ?> ~ <?= h((string)$pending['period_end']) ?>)
        <?php else: ?>
          이 브라우저에서 <?= h(nfmt($cooldownLeft)) ?>초 뒤에 다시 요청할 수 있습니다.
        <?php endif; ?>
      </p>
    <?php else: ?>
      <p class="rescanbox-s">위 "조회 기간" 에서 기간을 바꾼 뒤 요청하면 그 기간으로 분석합니다
        (최대 <?= h(nfmt(TAREP_MAX_RANGE_DAYS)) ?>일).</p>
    <?php endif; ?>
    <p class="note">이 버튼은 <strong>종합분석 요청만 기록</strong>합니다(<span class="mono">trade_analysis_request</span>).
      주문 · 알고리즘 · 설정은 전혀 바뀌지 않으며, 실제 분석(Claude 호출)은 서버 모듈이 요청을 확인한 뒤
      수행합니다. 요청 상태는 아래 <strong>종합분석 리포트 이력</strong>에 새 행이 나타나는 것으로
      확인하세요(보통 1~2분, 새로고침 필요).</p>
  </div>
</section>
<?php elseif ($pending !== null): ?>
<section class="panel">
  <div class="panel-h"><h2>종합분석 요청 처리 중</h2></div>
  <p class="alert alert-warn"><?= tarep_request_status_badge((string)$pending['status']) ?>
    <?= h(kst($pending['requested_at'], 'm-d H:i:s')) ?> 요청
    (<?= h((string)$pending['period_start']) ?> ~ <?= h((string)$pending['period_end']) ?>) ·
    요청자 <?= h((string)$pending['requested_by']) ?></p>
</section>
<?php endif; ?>

<section class="panel">
  <div class="panel-h"><h2>조회 기간</h2></div>
  <p class="note">기본값은 최근 <?= h(nfmt(TAREP_DEFAULT_DAYS)) ?>일입니다. 기간을 바꾸면 아래 리포트
    이력도 그 기간과 겹치는 것만 보이고, 관리자의 종합분석 요청도 이 기간으로 접수됩니다.</p>
  <?= filter_form_open('trade.report') ?>
    <?= filter_field_date('from', '시작일', $from) ?>
    <?= filter_field_date('to', '종료일', $to) ?>
  <?= filter_form_close() ?>
</section>

<section class="panel">
  <div class="panel-h"><h2>종합분석 리포트 이력</h2><span class="muted">최신순</span></div>

  <?php if (!$reportAvailable): ?>
    <?= empty_note('거래 종합분석 리포트 표(trade_analysis_report)를 읽을 수 없습니다.',
        '데이터베이스에 해당 표가 없거나 조회 권한이 없습니다. 서버 모듈이 스키마를 적용하면 표시됩니다.') ?>
  <?php elseif ($res['rows'] === []): ?>
    <?= empty_note('이 기간에 생성된 종합분석 리포트가 없습니다.',
        $isAdmin ? '위의 "종합분석 요청" 버튼으로 지금 만들 수 있습니다.'
                 : '관리자가 "종합분석 요청"을 누르면 서버가 처리한 뒤 이곳에 표시됩니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>기간</th><th>상태</th><th class="num">거래</th><th class="num">승/패</th>
        <th class="num">승률</th><th class="num">손익합계</th><th>모델</th><th>요청자</th>
        <th>요약</th><th>상세</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $ri => $r): ?>
        <?php
        $st = (string)$r['status'];
        $detId = 'tarep-det-' . (int)$ri;
        ?>
        <tr class="<?= $st === 'error' ? 'lv-error' : '' ?>">
          <td><?= h(kst($r['period_start'], 'Y-m-d')) ?> ~ <?= h(kst($r['period_end'], 'Y-m-d')) ?>
            <span class="sub"><?= h(kst($r['created_at'], 'm-d H:i')) ?> 생성</span></td>
          <td><?= tarep_status_badge($st) ?></td>
          <td class="num"><?= h(nfmt($r['trade_count'])) ?></td>
          <td class="num"><?= h(nfmt($r['win_count'])) ?> / <?= h(nfmt($r['loss_count'])) ?></td>
          <td class="num"><?= $r['win_rate'] === null ? '-' : h(nfmt($r['win_rate'], 1) . '%') ?></td>
          <td class="num <?= h(sign_class($r['total_pl_amt'])) ?>"><?= h(money_signed($r['total_pl_amt'])) ?></td>
          <td class="mono"><?= h((string)$r['model']) ?></td>
          <td><?= h((string)$r['requested_by']) ?></td>
          <td class="wrap-td"><?php if ($st === 'error'): ?>
              <span class="llm-err"><?= ($r['error_msg'] ?? '') !== '' ? h($r['error_msg']) : '리포트 생성 실패' ?></span>
            <?php elseif (($r['summary'] ?? '') !== ''): ?>
              <?= h($r['summary']) ?>
            <?php else: ?><span class="muted">요약 없음</span><?php endif; ?></td>
          <td><details class="rowdet" data-target="<?= h($detId) ?>"><summary>상세</summary></details></td>
        </tr>
        <tr class="rowdet-line" id="<?= h($detId) ?>" hidden>
          <td colspan="10">
            <div class="rowdet-body">
              <?php if (($r['report_text'] ?? '') !== ''): ?>
                <p class="rd-k">종합분석 전문(참고용)</p>
                <p class="rd-v" style="white-space:pre-wrap;"><?= h($r['report_text']) ?></p>
              <?php else: ?>
                <p class="tl-empty">리포트 본문이 없습니다.</p>
              <?php endif; ?>
              <p class="rd-k">토큰 사용량</p>
              <p class="rd-v">입력 <?= h(nfmt($r['input_tokens'])) ?> / 출력 <?= h(nfmt($r['output_tokens'])) ?></p>
            </div>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>
<?php endif; ?>
