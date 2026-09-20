<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

$rows = repo_server_status();
$settings = repo_system_settings();
$runs = db_all('SELECT id, started_at, ended_at, env, order_enabled, note FROM algo_run ORDER BY id DESC LIMIT 10');
$apiStats = db_all(
    'SELECT api_id, COUNT(*) AS cnt, SUM(CASE WHEN return_code <> 0 THEN 1 ELSE 0 END) AS err_cnt,
            ROUND(AVG(elapsed_ms)) AS avg_ms, MAX(created_at) AS last_at
       FROM api_call_log WHERE created_at >= (NOW() - INTERVAL 1 DAY)
      GROUP BY api_id ORDER BY cnt DESC LIMIT 20'
);
$componentLabel = [
    'server' => '서버 프로세스', 'db' => '데이터베이스', 'kiwoom_rest' => '키움 REST',
    'kiwoom_ws' => '키움 WebSocket', 'market' => '시장 상태',
];
$settingLabel = [
    'trading_mode' => '거래 환경', 'order_enabled' => '주문 전송 허용',
    'real_trading_confirm' => '실계좌 이중확인', 'poll_interval_sec' => '평가 주기(초)',
    'log_retention_days' => '로그 보관(일)',
];
?>
<section class="panel" id="statuspanel">
  <div class="panel-h">
    <h2>서버 상태 (하트비트)</h2>
    <span class="muted">경고 <?= (int)APP_HEARTBEAT_WARN_SEC ?>초 / 오류 <?= (int)APP_HEARTBEAT_ERROR_SEC ?>초 초과 · 30초 자동 갱신</span>
  </div>
  <?php if ($rows === []): ?>
    <?= empty_note('서버 상태 정보가 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th>구성요소</th><th>상태</th><th>메시지</th><th>마지막 갱신</th><th class="num">경과</th></tr></thead>
      <tbody data-k="status-rows">
      <?php foreach ($rows as $r): $eff = repo_effective_status($r); ?>
        <tr data-c="<?= h($r['component']) ?>">
          <td><?= h($componentLabel[(string)$r['component']] ?? $r['component']) ?>
              <span class="muted mono"><?= h($r['component']) ?></span></td>
          <td><span class="dot <?= h(status_class($eff['status'])) ?>" aria-hidden="true"></span>
              <span class="st-label <?= h(status_class($eff['status'])) ?>"><?= h(status_label($eff['status'])) ?></span></td>
          <td class="wrap-td cell-msg"><?= h(($r['message'] ?? '') !== '' ? $r['message'] : '-') ?><?= $eff['note'] !== '' ? ' · ' . h($eff['note']) : '' ?></td>
          <td class="cell-upd"><?= h(kst($r['updated_at'])) ?></td>
          <td class="num cell-age"><?= $eff['age'] === null ? '-' : h(nfmt($eff['age'])) . '초' ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>

<div class="grid2">
<section class="panel">
  <div class="panel-h"><h2>런타임 설정 (읽기 전용)</h2></div>
  <?php if ($settings === []): ?>
    <?= empty_note('설정 값이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th>항목</th><th>값</th><th>설명</th><th>변경자 / 시각</th></tr></thead>
      <tbody>
      <?php foreach ($settings as $s): ?>
        <tr>
          <td><?= h($settingLabel[(string)$s['setting_key']] ?? $s['setting_key']) ?>
              <span class="muted mono"><?= h($s['setting_key']) ?></span></td>
          <td><strong><?= h($s['value']) ?></strong></td>
          <td class="wrap-td"><?= h($s['description']) ?></td>
          <td><?= h($s['updated_by'] ?? '-') ?><br><span class="muted"><?= h(kst($s['updated_at'], 'Y-m-d H:i')) ?></span></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
  <p class="note">설정 변경은 서버 프로그램(stock_svr)에서만 가능합니다.</p>
</section>

<section class="panel">
  <div class="panel-h"><h2>최근 서버 실행 이력</h2></div>
  <?php if ($runs === []): ?>
    <?= empty_note('실행 이력이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th class="num">ID</th><th>시작</th><th>종료</th><th>환경</th><th>주문허용</th><th>비고</th></tr></thead>
      <tbody>
      <?php foreach ($runs as $r): ?>
        <tr>
          <td class="num"><?= h($r['id']) ?></td>
          <td><?= h(kst($r['started_at'])) ?></td>
          <td><?= $r['ended_at'] === null ? badge('실행중', 'ok') : h(kst($r['ended_at'])) ?></td>
          <td><?= badge($r['env'] === 'real' ? 'REAL' : 'MOCK', $r['env'] === 'real' ? 'err' : 'info') ?></td>
          <td><?= (int)$r['order_enabled'] === 1 ? badge('ON', 'warn') : badge('OFF', 'muted') ?></td>
          <td class="wrap-td"><?= h($r['note']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
</div>

<section class="panel">
  <div class="panel-h"><h2>최근 24시간 API 호출</h2></div>
  <?php if ($apiStats === []): ?>
    <?= empty_note('API 호출 기록이 없습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr><th>API</th><th class="num">호출수</th><th class="num">오류</th><th class="num">평균(ms)</th><th>마지막 호출</th></tr></thead>
      <tbody>
      <?php foreach ($apiStats as $r): ?>
        <tr>
          <td class="mono"><?= h($r['api_id']) ?></td>
          <td class="num"><?= h(nfmt($r['cnt'])) ?></td>
          <td class="num <?= ((int)$r['err_cnt'] > 0 ? 'v-up' : '') ?>"><?= h(nfmt($r['err_cnt'])) ?></td>
          <td class="num"><?= h(nfmt($r['avg_ms'])) ?></td>
          <td><?= h(kst($r['last_at'])) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
