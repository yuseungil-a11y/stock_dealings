<?php
declare(strict_types=1);
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }
/** @var ?int $accountId @var ?array $account @var array $accounts */

$d = repo_dashboard($accountId);
$bal = $d['balance'];
$tot = $d['holding_totals'];
$statusRows = repo_server_status();
$events = repo_recent_events(10);
$holdings = $accountId !== null ? repo_holdings($accountId, 'evlt_amt_desc') : [];
$topHoldings = array_slice($holdings, 0, 5);
$hasDelisted = false;
foreach ($topHoldings as $hd) {
    if (!empty($hd['delisted'])) { $hasDelisted = true; break; }
}
$llm = $d['llm'];
$mode = repo_setting('trading_mode', '-');
$orderEnabled = repo_setting('order_enabled', '0');
$realConfirm = repo_setting('real_trading_confirm', '0');
$canOrder = ($orderEnabled === '1') && ($mode === 'mock' || $realConfirm === '1');
?>
<section class="cards" id="dash" data-refresh="30">
  <div class="card">
    <h2 class="card-t">예수금</h2>
    <p class="card-v" data-k="entr"><?= $bal ? h(money($bal['entr'])) : '-' ?></p>
    <p class="card-s">주문가능 <span data-k="ord_alow_amt"><?= $bal ? h(money($bal['ord_alow_amt'])) : '-' ?></span></p>
  </div>
  <div class="card">
    <h2 class="card-t">총 평가금액</h2>
    <p class="card-v" data-k="tot_evlt_amt"><?= $bal ? h(money($bal['tot_evlt_amt'])) : h(money($tot['evlt_amt'])) ?></p>
    <p class="card-s">매입 <span data-k="tot_pur_amt"><?= $bal ? h(money($bal['tot_pur_amt'])) : h(money($tot['pur_amt'])) ?></span></p>
  </div>
  <div class="card">
    <h2 class="card-t">평가손익</h2>
    <?php $pl = $bal ? $bal['tot_evlt_pl'] : $tot['evltv_prft']; ?>
    <p class="card-v <?= h(sign_class($pl)) ?>" data-k="tot_evlt_pl"><?= h(money_signed($pl)) ?></p>
    <?php $rt = $bal && $bal['tot_prft_rt'] !== null ? $bal['tot_prft_rt'] : $tot['prft_rt']; ?>
    <p class="card-s <?= h(sign_class($rt)) ?>" data-k="tot_prft_rt"><?= h(pct($rt)) ?></p>
    <?php if ((int)$tot['delisted_cnt'] > 0 && $tot['prft_rt_ex'] !== null): ?>
      <p class="card-s">상장폐지 종목 제외 시
        <span class="<?= h(sign_class($tot['prft_rt_ex'])) ?>" data-k="prft_rt_ex"><?= h(pct($tot['prft_rt_ex'])) ?></span>
        <span class="muted">(보유 <?= h(nfmt($tot['delisted_cnt'])) ?>종목 제외)</span></p>
    <?php endif; ?>
  </div>
  <div class="card">
    <h2 class="card-t">보유종목</h2>
    <p class="card-v" data-k="holding_cnt"><?= h(nfmt($tot['cnt'])) ?><span class="unit">종목</span></p>
    <p class="card-s">오늘 실현손익 <span class="<?= h(sign_class($d['today_pl'])) ?>" data-k="today_pl"><?= h(money_signed($d['today_pl'])) ?></span></p>
  </div>
</section>

<section class="panel">
  <div class="panel-h">
    <h2>서버 연결 상태</h2>
    <span class="muted" id="dash-updated">갱신: <?= h(date('H:i:s')) ?></span>
  </div>
  <ul class="statuslist" data-k="components">
    <?php foreach ($statusRows as $row):
        $eff = repo_effective_status($row); ?>
      <li class="statusitem">
        <span class="dot <?= h(status_class($eff['status'])) ?>" aria-hidden="true"></span>
        <span class="st-name"><?= h($row['component']) ?></span>
        <span class="st-label <?= h(status_class($eff['status'])) ?>"><?= h(status_label($eff['status'])) ?></span>
        <span class="st-msg"><?= h($row['message'] ?? '') ?><?= $eff['note'] !== '' ? ' · ' . h($eff['note']) : '' ?></span>
        <span class="st-time"><?= h(ago($row['updated_at'])) ?></span>
      </li>
    <?php endforeach; ?>
    <?php if ($statusRows === []): ?>
      <li class="statusitem"><span class="st-msg">서버 상태 정보가 없습니다.</span></li>
    <?php endif; ?>
  </ul>
  <p class="runmode">
    거래환경 <?= badge($mode === 'real' ? 'REAL (실전)' : 'MOCK (모의)', $mode === 'real' ? 'err' : 'info') ?>
    주문전송 <?= badge($orderEnabled === '1' ? 'ON' : 'OFF', $orderEnabled === '1' ? 'warn' : 'muted') ?>
    실전이중확인 <?= badge($realConfirm === '1' ? 'ON' : 'OFF', $realConfirm === '1' ? 'warn' : 'muted') ?>
    <?= $canOrder ? badge('주문 전송 가능', 'err') : badge('신호만 기록 (주문 미전송)', 'ok') ?>
  </p>
</section>

<div class="grid2">
  <section class="panel">
    <div class="panel-h">
      <h2>보유종목 상위</h2>
      <a class="btn btn-sm" href="<?= h(u('index.php?p=account.holdings')) ?>">전체 보기</a>
    </div>
    <?php if ($topHoldings === []): ?>
      <?= empty_note('보유 중인 종목이 없습니다.', '서버 모듈이 계좌를 동기화하면 표시됩니다.') ?>
    <?php else: ?>
    <div class="tablewrap">
      <table class="tbl">
        <thead><tr><th>종목</th><th class="num">수량</th><th class="num">평가금액</th><th class="num">손익</th><th class="num">수익률</th></tr></thead>
        <tbody>
        <?php foreach ($topHoldings as $hd): ?>
          <?php $hdDel = !empty($hd['delisted']); $hdRt = holding_display_rate($hd); ?>
          <tr<?= $hdDel ? ' class="row-delisted"' : '' ?>>
            <td><span class="stk-nm"><?= h($hd['stk_nm']) ?></span> <span class="stk-cd"><?= h($hd['stk_cd']) ?></span>
              <?= $hdDel ? delisted_badge() : '' ?></td>
            <td class="num"><?= h(nfmt($hd['rmnd_qty'])) ?></td>
            <td class="num"><?= h(money($hd['evlt_amt'])) ?></td>
            <td class="num <?= h(sign_class($hd['evltv_prft'])) ?>"><?= h(money_signed($hd['evltv_prft'])) ?></td>
            <?php if ($hdDel): ?>
              <td class="num <?= h(sign_class($hdRt)) ?>" title="<?= h(holding_delisted_note($hd)) ?>">
                <?= h(pct($hdRt, 1)) ?><span class="delisted-mark" aria-hidden="true">*</span></td>
            <?php else: ?>
              <td class="num <?= h(sign_class($hd['prft_rt'])) ?>"><?= h(pct($hd['prft_rt'])) ?></td>
            <?php endif; ?>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
    <?php if ($hasDelisted): ?>
      <p class="note"><strong>*</strong> 현재가 0 · 종목명 <code>(폐)</code> 인 <strong>상장폐지</strong> 종목은
        키움 수익률(0.00%) 대신 <strong>평가손익 ÷ 매입금액</strong> 기준 실제 수익률로 표시합니다.</p>
    <?php endif; ?>
    <?php endif; ?>
  </section>

  <section class="panel">
    <div class="panel-h">
      <h2>최근 이벤트</h2>
      <a class="btn btn-sm" href="<?= h(u('index.php?p=system.events')) ?>">전체 보기</a>
    </div>
    <?php if ((int)($d['fail_24h'] ?? 0) > 0): ?>
      <p class="failline">
        <span class="failline-t">최근 24시간</span>
        <span>주문 실패 · 거부 <strong data-k="fail_24h"><?= h(nfmt($d['fail_24h'])) ?></strong>건</span>
        <a class="btn btn-sm" href="<?= h(u('index.php?p=trade.analysis&result=failed')) ?>">원인 보기</a>
      </p>
    <?php endif; ?>
    <?php if ($llm !== null && (int)$llm['total'] > 0): ?>
      <p class="llmline">
        <span class="llmline-t">오늘 Claude 검토</span>
        <span data-k="llm_summary">호출 <?= h(nfmt($llm['calls'])) ?>건 · 차단 <?= h(nfmt($llm['blocks'])) ?>건 · 오류 <?= h(nfmt($llm['errors'])) ?>건 · 캐시 <?= h(nfmt($llm['cache_hits'])) ?>건</span>
        <a class="btn btn-sm" href="<?= h(u('index.php?p=strategy.claude')) ?>">판단 보기</a>
      </p>
    <?php endif; ?>
    <?php if ($events === []): ?>
      <?= empty_note('기록된 이벤트가 없습니다.') ?>
    <?php else: ?>
    <ul class="eventlist" data-k="events">
      <?php foreach ($events as $ev): ?>
        <li>
          <span class="ev-time"><?= h(kst($ev['created_at'], 'm-d H:i:s')) ?></span>
          <?= level_badge((string)$ev['level']) ?>
          <span class="ev-cat"><?= h($ev['category']) ?></span>
          <span class="ev-msg"><?= h($ev['message']) ?></span>
        </li>
      <?php endforeach; ?>
    </ul>
    <?php endif; ?>
  </section>
</div>

<section class="panel">
  <div class="panel-h"><h2>오늘 요약</h2></div>
  <ul class="kv">
    <li><span>오늘 주문 건수</span><strong data-k="today_orders"><?= h(nfmt($d['today_orders'])) ?></strong></li>
    <li><span>오늘 신호 건수</span><strong data-k="today_signals"><?= h(nfmt($d['today_signals'])) ?></strong></li>
    <li><span>선택 계좌</span><strong><?= h(repo_account_label($account)) ?></strong></li>
    <li><span>최근 잔고 스냅샷</span><strong><?= $bal ? h(kst($bal['snapshot_at'])) : '-' ?></strong></li>
  </ul>
</section>
