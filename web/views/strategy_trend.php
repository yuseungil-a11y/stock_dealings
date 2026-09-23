<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 전략 → 산업 트렌드.
 * 서버의 산업 트렌드 스캔(국내 + 해외 조사 → 매수 후보 선별)이 남긴
 * trend_scan_run / trend_scan_candidate / trend_scan_attempt 기록을 조회한다.
 *
 * 이 화면의 유일한 쓰기는 관리자의 "지금 다시 조사" 버튼이며, 그것도 trend_scan_request 에
 * 요청 한 줄을 남기는 것이 전부다(주문 · 설정 · 알고리즘은 건드리지 않는다).
 */

$from = clean_date($_GET['from'] ?? null);
$to = clean_date($_GET['to'] ?? null);
$region = clean_enum($_GET['region'] ?? '', TREND_REGIONS, '');
$match = clean_enum($_GET['match'] ?? '', TREND_MATCHES, '');
$signalFilter = clean_enum($_GET['signal'] ?? '', TREND_SIGNAL_FILTERS, '');
$trigger = clean_enum($_GET['trigger'] ?? '', TREND_TRIGGERS, '');
$pageNo = clean_int($_GET['page'] ?? 1, 1, 1, 100000);
$attPageNo = clean_int($_GET['apage'] ?? 1, 1, 1, 100000);
// 재조사 요청 결과 배너(리다이렉트로 전달, 화이트리스트).
$rq = clean_enum($_GET['rq'] ?? '', ['ok', 'busy', 'err'], '');

$available = repo_trend_available();
$sum = repo_trend_summary();
$res = repo_trend_runs($from, $to, $region, $match, $signalFilter, $trigger, $pageNo);
// 화면에 보이는 실행들의 후보를 한 번에 조회한다(N+1 금지). 두 번째 인자는 뽑아낼 키 이름.
$cands = repo_trend_candidates(repo_collect_order_ids($res['rows'], 'id'), $region, $match, $signalFilter);
$hasCandFilter = ($region !== '' || $match !== '' || $signalFilter !== '');
$hasFilter = ($from !== null || $to !== null || $trigger !== '' || $hasCandFilter);
$today = $sum['today'];

/* 시도 이력(성공 · 실패 모두) — 오늘 실패한 시도가 화면에서 사라지지 않도록 별도 표로 보여준다. */
$attAvailable = repo_trend_attempt_available();
$attRes = repo_trend_attempts($from, $to, $trigger, $attPageNo);

/* 재조사 버튼 상태: 관리자에게만 보이고, 처리 중으로 보이면 잠근다.
 * 웹 계정은 trend_scan_request 를 조회할 수 없으므로
 *   (1) 같은 브라우저 세션의 마지막 요청 시각(쿨다운)
 *   (2) 최근 몇 분 안의 수동 시도 기록(trend_scan_attempt)
 * 두 가지로 간접 판단한다. 최종 방어선은 서버 모듈이다. */
$isAdmin = auth_is_admin();
$lastReqAt = (int)($_SESSION['trend_req_at'] ?? 0);
$cooldownLeft = $lastReqAt > 0 ? max(0, TREND_REQUEST_COOLDOWN_SEC - (time() - $lastReqAt)) : 0;
$manualRecent = $isAdmin ? repo_trend_manual_recent(TREND_MANUAL_RECENT_MIN) : null;
$rescanLocked = ($cooldownLeft > 0 || $manualRecent !== null);
?>
<?php if (!$available): ?>
  <?= empty_note('산업 트렌드 스캔 기록 표를 읽을 수 없습니다.',
      '데이터베이스에 trend_scan_run · trend_scan_candidate 표가 없거나 조회 권한이 없습니다. 서버 모듈이 스키마를 적용하면 표시됩니다.') ?>
<?php else: ?>

<section class="cards cards-wide">
  <div class="card"><h2 class="card-t">최근 스캔</h2>
    <p class="card-v"><?= $sum['last'] === null ? '-' : h(kst($sum['last']['scan_date'], 'Y-m-d')) ?></p>
    <p class="card-s"><?= $sum['last'] === null
        ? '기록 없음'
        : trend_status_badge((string)$sum['last']['status']) . ' <span class="mono">' . h($sum['last']['model']) . '</span>' ?></p></div>
  <div class="card"><h2 class="card-t">이번달 후보</h2>
    <p class="card-v"><?= h(nfmt($sum['candidates'])) ?><span class="unit">개</span></p>
    <p class="card-s">스캔 <?= h(nfmt($sum['runs'])) ?>회 (<?= h(kst($sum['month_from'], 'Y-m')) ?>)</p></div>
  <div class="card"><h2 class="card-t">매칭 성공률</h2>
    <p class="card-v"><?= $sum['match_rate'] === null ? '-' : h(nfmt($sum['match_rate'], 1) . '%') ?></p>
    <p class="card-s">종목 연결 <?= h(nfmt($sum['matched'])) ?> / <?= h(nfmt($sum['candidates'])) ?>개</p></div>
  <div class="card"><h2 class="card-t">신호 전환</h2>
    <p class="card-v"><?= h(nfmt($sum['signals'])) ?><span class="unit">건</span></p>
    <p class="card-s">후보 → 매수 신호</p></div>
  <div class="card"><h2 class="card-t">이번달 웹검색</h2>
    <p class="card-v"><?= h(nfmt($sum['web_search'])) ?><span class="unit">회</span></p>
    <p class="card-s">조사 단계 검색 횟수</p></div>
  <div class="card"><h2 class="card-t">이번달 토큰</h2>
    <p class="card-v"><?= h(nfmt($sum['input_tokens'])) ?><span class="unit">입력</span></p>
    <p class="card-s">출력 <?= h(nfmt($sum['output_tokens'])) ?></p></div>
</section>

<?php if ($isAdmin): ?>
<section class="panel">
  <div class="panel-h"><h2>수동 재조사</h2><span class="muted">관리자 전용</span></div>

  <?php if ($rq === 'ok'): ?>
    <p class="alert alert-ok">재조사를 요청했습니다. 서버가 확인 후 처리합니다(보통 1~2분,
      웹 검색 실패 시 더 걸릴 수 있음). 이 페이지를 새로고침하면 진행 상태를 볼 수 있습니다.</p>
  <?php elseif ($rq === 'busy'): ?>
    <p class="alert alert-warn">요청 처리 중입니다. 앞선 요청이 끝난 뒤에 다시 시도해 주세요.</p>
  <?php elseif ($rq === 'err'): ?>
    <p class="alert alert-err">재조사 요청을 접수하지 못했습니다. 잠시 후 다시 시도해 주세요.</p>
  <?php endif; ?>

  <div class="rescanbox">
    <div class="rescanbox-h">
      <h3>지금 다시 조사</h3>
      <form class="rescanform" method="post" action="<?= h(u('index.php')) ?>">
        <?= csrf_field() ?>
        <input type="hidden" name="action" value="trend_rescan">
        <button class="btn btn-primary" type="submit"<?= $rescanLocked ? ' disabled' : '' ?>>지금 다시 조사</button>
      </form>
    </div>
    <?php if ($rescanLocked): ?>
      <p class="rescanbox-s">요청 처리 중입니다 —
        <?php if ($manualRecent !== null): ?>
          최근 수동 시도 <?= h(kst($manualRecent['created_at'], 'H:i:s')) ?>
          (<?= trend_status_badge((string)$manualRecent['status']) ?>)
        <?php else: ?>
          이 브라우저에서 <?= h(nfmt($cooldownLeft)) ?>초 뒤에 다시 요청할 수 있습니다.
        <?php endif; ?>
      </p>
    <?php else: ?>
      <p class="rescanbox-s">오늘 스캔 결과가 비었거나 웹 검색이 실패했을 때, 같은 조사를 서버에 다시 요청합니다.</p>
    <?php endif; ?>
    <p class="note">이 버튼은 <strong>재조사 요청만 기록</strong>합니다(<span class="mono">trend_scan_request</span>).
      주문 · 알고리즘 · 설정은 전혀 바뀌지 않으며, 실제 조사는 서버 모듈이 요청을 확인한 뒤 수행합니다.
      요청 상태는 웹에서 직접 조회할 수 없으므로 아래 <strong>스캔 시도 이력</strong>에 새 행(구분 = 수동)이
      나타나는 것으로 확인하세요(보통 1~2분, 새로고침 필요).</p>
  </div>
</section>
<?php endif; ?>

<section class="panel">
  <div class="panel-h"><h2>산업 트렌드 스캔 이력</h2></div>

  <?php if ($today === null): ?>
    <p class="alert alert-warn">오늘 아직 스캔 전입니다 — 조사 시각 이후 자동 실행됩니다.
      (실행 시각은 <strong>전략 → 파라미터 · 변경이력</strong> 화면에서 확인할 수 있습니다.)</p>
  <?php else: ?>
    <p class="llmline">
      <span class="llmline-t">오늘 스캔</span>
      <span><?= h(kst($today['scan_date'], 'Y-m-d')) ?> · 후보 <?= h(nfmt($today['candidates'])) ?>개 ·
        신호 <?= h(nfmt($today['signals'])) ?>건</span>
      <?= trend_status_badge((string)$today['status']) ?>
      <?= trend_trigger_badge((string)($today['trigger_type'] ?? '')) ?>
      <?php if (($today['requested_by'] ?? '') !== ''): ?>
        <span class="sub">요청자 <?= h($today['requested_by']) ?></span>
      <?php endif; ?>
      <?php if (($today['error_msg'] ?? '') !== ''): ?>
        <span class="llm-err"><?= h($today['error_msg']) ?></span>
      <?php endif; ?>
      <a class="btn btn-sm" href="<?= h('#attempts') ?>">시도 이력 보기</a>
    </p>
  <?php endif; ?>

  <p class="note">Claude 가 하루 1회 <strong>국내 · 해외 산업 트렌드</strong>를 조사해 매수 후보를 골라낸 기록입니다.
    후보는 키움 테마(ka90001) 구성종목이나 종목마스터의 <strong>이름 일치</strong>로만 종목과 연결하며,
    찾지 못하면 <strong>추측하지 않고 미매칭으로 남깁니다</strong>. 조회 전용 화면이며 설정을 변경하지 않습니다.</p>

  <?= filter_form_open('strategy.trend') ?>
    <?= filter_field_date('from', '시작일(스캔일)', $from) ?>
    <?= filter_field_date('to', '종료일(스캔일)', $to) ?>
    <?= filter_field_select('region', '지역', trend_region_options(), $region) ?>
    <?= filter_field_select('match', '매칭방식', trend_match_options(), $match) ?>
    <?= filter_field_select('signal', '신호 여부', trend_signal_options(), $signalFilter) ?>
    <?= filter_field_select('trigger', '구분', trend_trigger_options(), $trigger) ?>
  <?= filter_form_close() ?>

  <?php if ($hasCandFilter): ?>
    <p class="note">지역 · 매칭방식 · 신호 여부 조건은 <strong>후보</strong>에 적용됩니다.
      조건에 맞는 후보가 있는 스캔만 표시되고, 아래 후보 목록도 같은 조건으로 걸러집니다.</p>
  <?php endif; ?>

  <?php if ($res['rows'] === []): ?>
    <?= $hasFilter
        ? empty_note('검색 조건에 맞는 스캔 기록이 없습니다.', '기간 · 지역 · 매칭방식 · 신호 여부 조건을 바꾸어 다시 조회해 보세요.')
        : empty_note('아직 산업 트렌드 스캔 기록이 없습니다.',
            '서버 모듈의 트렌드 스캔이 하루 1회 실행되면 이곳에 조사 결과와 매수 후보가 쌓입니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>스캔일</th><th>상태</th><th>구분</th><th>조사범위</th><th>모델</th>
        <th class="num">후보</th><th class="num">신호</th><th class="num">웹검색</th>
        <th class="num">토큰(입력/출력)</th><th class="num">지연</th><th>실행시각</th><th>오류</th><th>상세</th>
      </tr></thead>
      <tbody>
      <?php foreach ($res['rows'] as $r): ?>
        <?php
        $rid = (int)$r['id'];
        $rows = $cands[$rid] ?? [];
        $shown = count($rows);
        $sigShown = 0;
        foreach ($rows as $c) {
            if ($c['signal_id'] !== null) { $sigShown++; }
        }
        $st = (string)$r['status'];
        ?>
        <tr class="<?= $st === 'error' ? 'lv-error' : ($st === 'partial' ? 'lv-warn' : '') ?>">
          <td><?= h(kst($r['scan_date'], 'Y-m-d')) ?></td>
          <td><?= trend_status_badge($st) ?></td>
          <td><?= trend_trigger_badge((string)($r['trigger_type'] ?? '')) ?>
            <?php if (($r['requested_by'] ?? '') !== ''): ?>
              <span class="sub"><?= h($r['requested_by']) ?></span>
            <?php endif; ?></td>
          <td class="mono"><?= h($r['region_scope']) ?></td>
          <td class="mono"><?= h($r['model']) ?></td>
          <td class="num"><?= h(nfmt($r['candidate_count'])) ?></td>
          <td class="num"><?= h(nfmt($r['signal_count'])) ?></td>
          <td class="num"><?= h(nfmt($r['web_search_count'])) ?></td>
          <td class="num"><?= h(nfmt($r['input_tokens'])) ?> / <?= h(nfmt($r['output_tokens'])) ?></td>
          <td class="num"><?= $r['latency_ms'] === null ? '-' : h(nfmt($r['latency_ms']) . 'ms') ?></td>
          <td><?= h(kst($r['started_at'], 'm-d H:i:s')) ?>
            <span class="sub">종료 <?= h(kst($r['finished_at'], 'H:i:s')) ?></span></td>
          <td class="wrap-td"><?= ($r['error_msg'] ?? '') !== '' ? h($r['error_msg']) : '<span class="muted">-</span>' ?></td>
          <td><a class="btn btn-sm" href="<?= h('#run-' . $rid) ?>">후보 <?= h(nfmt($shown)) ?>건<?= $sigShown > 0 ? ' · 신호 ' . h(nfmt($sigShown)) : '' ?></a></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <?= render_pager($res) ?>
</section>

<section class="panel" id="attempts">
  <div class="panel-h"><h2>스캔 시도 이력 (실패 포함)</h2>
    <span class="muted">모든 시도를 영구 보존 · 최신순</span></div>

  <p class="note">위의 <strong>스캔 이력</strong>은 하루에 한 행(그날의 최신 결과)만 남지만, 이 표는
    <strong>예약 · 수동을 포함한 모든 시도</strong>를 실패한 것까지 그대로 보존합니다.
    웹 검색이 모두 실패한 시도도 여기에 남으므로 "왜 결과가 비었는지"를 확인할 수 있습니다.
    각 행의 <strong>조사 원문</strong>을 펼치면 Claude 응답 전문을 볼 수 있습니다.
    기간 · 구분 필터는 위 필터를 그대로 따릅니다.</p>

  <?php if (!$attAvailable): ?>
    <?= empty_note('시도 이력 표를 읽을 수 없습니다.',
        '데이터베이스에 trend_scan_attempt 표가 없거나 조회 권한이 없습니다. 서버 모듈이 스키마를 적용하면 표시됩니다.') ?>
  <?php elseif ($attRes['rows'] === []): ?>
    <?= ($from !== null || $to !== null || $trigger !== '')
        ? empty_note('검색 조건에 맞는 시도 기록이 없습니다.', '기간 · 구분 조건을 바꾸어 다시 조회해 보세요.')
        : empty_note('아직 스캔 시도 기록이 없습니다.',
            '서버 모듈이 트렌드 스캔을 실행하면(예약 또는 수동) 성공 · 실패가 모두 이곳에 쌓입니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>시도 시각</th><th>스캔일</th><th>구분</th><th>요청자</th><th>상태</th>
        <th class="num">웹검색</th><th class="num">후보</th><th class="num">토큰(입력/출력)</th>
        <th class="num">지연</th><th>오류</th>
      </tr></thead>
      <tbody>
      <?php foreach ($attRes['rows'] as $a): ?>
        <?php $ast = (string)$a['status']; ?>
        <tr class="<?= $ast === 'error' ? 'lv-error' : ($ast === 'partial' ? 'lv-warn' : '') ?>">
          <td><?= h(kst($a['created_at'], 'm-d H:i:s')) ?>
            <span class="sub">시작 <?= h(kst($a['started_at'], 'H:i:s')) ?>
              · 종료 <?= h(kst($a['finished_at'], 'H:i:s')) ?></span></td>
          <td><?= h(kst($a['scan_date'], 'Y-m-d')) ?></td>
          <td><?= trend_trigger_badge((string)$a['trigger_type']) ?></td>
          <td><?= ($a['requested_by'] ?? '') !== '' ? h($a['requested_by']) : '<span class="muted">-</span>' ?></td>
          <td><?= trend_status_badge($ast) ?></td>
          <td class="num"><?= h(nfmt($a['web_search_count'])) ?></td>
          <td class="num"><?= h(nfmt($a['candidate_count'])) ?></td>
          <td class="num"><?= h(nfmt($a['input_tokens'])) ?> / <?= h(nfmt($a['output_tokens'])) ?></td>
          <td class="num"><?= $a['latency_ms'] === null ? '-' : h(nfmt($a['latency_ms']) . 'ms') ?></td>
          <td class="wrap-td"><?= ($a['error_msg'] ?? '') !== '' ? h($a['error_msg']) : '<span class="muted">-</span>' ?></td>
        </tr>
        <?php if (trim((string)($a['research_summary'] ?? '')) !== ''): ?>
          <tr class="attempt-raw"><td colspan="10">
            <?= trend_text_details('조사 원문 (research_summary) · ' . kst($a['created_at'], 'm-d H:i:s'),
                $a['research_summary']) ?>
          </td></tr>
        <?php endif; ?>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?= render_pager($attRes, 'apage') ?>
  <?php endif; ?>
</section>

<?php foreach ($res['rows'] as $i => $r): ?>
  <?php
  $rid = (int)$r['id'];
  $rows = $cands[$rid] ?? [];
  ?>
  <section class="panel">
    <details class="runbox" id="<?= h('run-' . $rid) ?>"<?= $i === 0 ? ' open' : '' ?>>
      <summary>
        <span class="runbox-d"><?= h(kst($r['scan_date'], 'Y-m-d')) ?></span>
        <?= trend_status_badge((string)$r['status']) ?>
        <?= trend_trigger_badge((string)($r['trigger_type'] ?? '')) ?>
        <span class="runbox-s">후보 <?= h(nfmt(count($rows))) ?>건 · 조사범위 <?= h($r['region_scope']) ?>
          · 모델 <?= h($r['model']) ?></span>
      </summary>
      <div class="runbox-body">
        <?php if (($r['error_msg'] ?? '') !== ''): ?>
          <p class="alert alert-warn">실행 메시지: <?= h($r['error_msg']) ?></p>
        <?php endif; ?>

        <?php if ($rows === []): ?>
          <?= $hasCandFilter
              ? empty_note('이 스캔에는 조건에 맞는 후보가 없습니다.', '지역 · 매칭방식 · 신호 여부 조건을 바꾸어 보세요.')
              : empty_note('이 스캔에서 선별된 후보가 없습니다.', '조사 결과가 매수 후보 기준에 못 미쳤거나 실행이 중단되었습니다.') ?>
        <?php else: ?>
        <div class="tablewrap">
          <table class="tbl">
            <thead><tr>
              <th>지역</th><th>테마</th><th>근거</th><th class="num">확신도</th>
              <th>매칭 종목</th><th>매칭방식</th><th>매수신호</th>
            </tr></thead>
            <tbody>
            <?php foreach ($rows as $c): ?>
              <?php
              $ms = (string)$c['match_status'];
              $sid = $c['signal_id'];
              $sigStk = (string)($c['signal_stk_cd'] ?? ($c['stk_cd'] ?? ''));
              $sigDay = $c['signal_time'] !== null ? substr((string)$c['signal_time'], 0, 10) : null;
              ?>
              <tr class="<?= $ms === 'unmatched' ? 'row-dry' : '' ?>">
                <td><?= trend_region_badge((string)$c['region']) ?></td>
                <td class="wrap-td"><span class="stk-nm"><?= h($c['theme']) ?></span>
                  <?php if (($c['kiwoom_theme_nm'] ?? '') !== ''): ?>
                    <span class="sub">키움테마 <?= h($c['kiwoom_theme_nm']) ?>
                      <?= ($c['kiwoom_theme_cd'] ?? '') !== '' ? '(' . h($c['kiwoom_theme_cd']) . ')' : '' ?></span>
                  <?php endif; ?></td>
                <td class="wrap-td"><?= ($c['rationale'] ?? '') !== '' ? h($c['rationale']) : '<span class="muted">-</span>' ?></td>
                <td class="num"><?= $c['confidence'] === null ? '-' : h(nfmt($c['confidence']) . '%') ?></td>
                <td class="wrap-td">
                  <?php if (($c['stk_cd'] ?? '') !== ''): ?>
                    <span class="stk-nm"><?= h($c['stk_nm']) ?></span> <span class="stk-cd"><?= h($c['stk_cd']) ?></span>
                  <?php else: ?>
                    <?= badge('미매칭', 'muted') ?>
                    <span class="sub"><?= h(trend_unmatched_note($c)) ?></span>
                  <?php endif; ?>
                </td>
                <td><?= trend_match_badge($ms) ?></td>
                <td class="wrap-td">
                  <?php if ($sid === null): ?>
                    <span class="muted">신호 없음</span>
                  <?php elseif ($c['signal_type'] === null): ?>
                    <?= badge('신호 #' . (int)$sid, 'muted') ?>
                    <span class="sub">연결된 신호 기록을 찾을 수 없습니다.</span>
                  <?php else: ?>
                    <?= signal_type_badge((string)$c['signal_type']) ?>
                    <span class="sub"><?= h(kst($c['signal_time'], 'm-d H:i')) ?>
                      <?= ($c['algo_code'] ?? '') !== '' ? h($c['algo_code']) : '' ?></span>
                    <span class="linkrow">
                      <a class="btn btn-sm" href="<?= h(u('index.php?p=strategy.signals&'
                          . http_build_query(array_filter(['q' => $sigStk, 'from' => $sigDay, 'to' => $sigDay])))) ?>">신호 보기</a>
                      <?php if ($c['order_id'] !== null): ?>
                        <?php // 주문은 계좌별 화면이라 선택 계좌에 따라 보이지 않을 수 있어, 계좌 구분이 없는 거래 분석으로 연결한다. ?>
                        <a class="btn btn-sm" href="<?= h(u('index.php?p=trade.analysis&'
                            . http_build_query(array_filter(['q' => $sigStk, 'from' => $sigDay, 'to' => $sigDay])))) ?>">주문 · 체결 보기</a>
                      <?php endif; ?>
                    </span>
                  <?php endif; ?>
                </td>
              </tr>
            <?php endforeach; ?>
            </tbody>
          </table>
        </div>
        <?php endif; ?>

        <?= trend_text_details('국내 테마 요약 원문 (domestic_theme_summary)', $r['domestic_theme_summary']) ?>
        <?= trend_text_details('조사 요약 원문 (research_summary)', $r['research_summary']) ?>
      </div>
    </details>
  </section>
<?php endforeach; ?>
<?php endif; ?>
