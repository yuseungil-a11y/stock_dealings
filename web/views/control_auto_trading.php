<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/**
 * 자동거래 제어 (관리자 전용 쓰기).
 *
 * 이 화면이 쓰는 것은 딱 하나, auto_trading_command 에 명령 한 줄을 INSERT 하는 것뿐이다
 * ('start' 또는 'stop', status='pending'). 실제 시작/중지 처리는 서버쪽 폴링 에이전트가
 * 이 큐를 읽어서 수행한다 — 이 웹은 order_enabled · trading_mode · real_trading_confirm
 * 게이트 3키를 절대 쓰지 않는다(읽기만).
 */

$g = repo_auto_trading_status();
$cmds = repo_recent_auto_trading_commands(20);
?>
<section class="panel">
  <div class="panel-h"><h2>자동거래 제어</h2><span class="muted badge badge-admin">관리자 전용</span></div>
  <p class="alert alert-err"><strong>⚠ 이 버튼은 실제 서버의 자동매매를 원격으로 시작/중지합니다.
    현재 게이트가 실계좌(REAL)이면 시작 즉시 실주문이 나갈 수 있습니다.</strong></p>

  <ul class="kv">
    <li><span>서버가 보고한 상태</span><strong>
      <?php if ($g['status'] === null): ?>
        <?= badge('미확인', 'muted') ?>
      <?php else: ?>
        <?php $eff = repo_effective_status($g['status']); ?>
        <span class="dot <?= h(status_class($eff['status'])) ?>" aria-hidden="true"></span>
        <?= h(status_label($eff['status'])) ?> — <?= h(($g['status']['message'] ?? '') !== '' ? $g['status']['message'] : '-') ?>
        <span class="muted"> (<?= h(kst($g['status']['updated_at'])) ?>)</span>
      <?php endif; ?>
    </strong></li>
    <li><span>주문 게이트(읽기 전용)</span><strong><?= h(auto_trading_gate_text($g)) ?></strong></li>
  </ul>
  <p class="note">게이트(실계좌/모의투자, 주문 전송 허용, 실계좌 이중확인)는 이 화면에서 바꿀 수 없습니다 —
    서버 프로그램(stock_svr) 설정 화면에서만 변경됩니다. 이 화면은 <strong>알고리즘 자동거래의 시작/중지 요청</strong>만 보냅니다.</p>

  <div class="autotrade-actions">
    <form method="post" action="<?= h(u('index.php')) ?>" id="startForm">
      <?= csrf_field() ?>
      <input type="hidden" name="action" value="auto_trading_start">
      <input type="hidden" name="confirm_word" id="confirmWordInput" value="">
      <button class="btn btn-primary" type="button" id="btnAutoStart">자동거래 시작</button>
    </form>
    <form method="post" action="<?= h(u('index.php')) ?>">
      <?= csrf_field() ?>
      <input type="hidden" name="action" value="auto_trading_stop">
      <button class="btn" type="submit">자동거래 중지</button>
    </form>
  </div>
  <p class="hint">시작은 "REAL" 재확인 절차를 거칩니다(대소문자·공백까지 정확히 일치해야 함). 중지는 항상 안전한 방향이므로 확인 절차가 없습니다.</p>

  <dialog id="realConfirmDialog" class="confirmdlg">
    <div class="confirmdlg-body">
      <h3>실계좌 자동거래 시작 확인</h3>
      <p>⚠ 자동거래를 시작하면 서버 게이트 설정에 따라 <strong>실제 주문이 즉시 전송될 수 있습니다.</strong></p>
      <p>계속하려면 아래 입력칸에 정확히 <strong class="mono">REAL</strong> 을 입력하세요(대문자, 공백 없이).</p>
      <input type="text" id="realConfirmInput" autocomplete="off" spellcheck="false" placeholder="REAL">
      <div class="confirmdlg-actions">
        <button class="btn" type="button" id="realConfirmCancel">취소</button>
        <button class="btn btn-primary" type="button" id="realConfirmOk" disabled>시작</button>
      </div>
    </div>
  </dialog>
</section>

<section class="panel">
  <div class="panel-h"><h2>최근 명령 이력 (최근 20건)</h2></div>
  <p class="note">서버쪽 에이전트가 이 큐를 몇 초 안에 처리합니다 — 새로고침하면 상태가
    <?= auto_trading_cmd_status_badge('pending') ?> → <?= auto_trading_cmd_status_badge('done') ?> /
    <?= auto_trading_cmd_status_badge('error') ?> 로 바뀌는 것을 볼 수 있습니다.</p>
  <?php if ($cmds === []): ?>
    <?= empty_note('명령 이력이 없습니다.',
        '서버쪽 에이전트가 auto_trading_command 표를 아직 준비하지 않았을 수도 있습니다.') ?>
  <?php else: ?>
  <div class="tablewrap">
    <table class="tbl">
      <thead><tr>
        <th>요청시각</th><th>명령</th><th>요청자</th><th>상태</th><th>처리시각</th><th>결과</th>
      </tr></thead>
      <tbody>
      <?php foreach ($cmds as $c): ?>
        <tr>
          <td><?= h(kst($c['requested_at'])) ?></td>
          <td><?= auto_trading_cmd_badge((string)$c['command']) ?></td>
          <td><?= h($c['requested_by'] ?? '-') ?></td>
          <td><?= auto_trading_cmd_status_badge((string)$c['status']) ?></td>
          <td><?= h(kst($c['handled_at'] ?? null)) ?></td>
          <td class="wrap-td"><?= ($c['result_message'] ?? '') !== '' ? h($c['result_message']) : '<span class="muted">-</span>' ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endif; ?>
</section>
