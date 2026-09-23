/*!
 * 주식 자동매매 관제 웹 — 바닐라 JS (프레임워크/CDN 사용 안 함)
 * CSP 상 인라인 스크립트가 금지되므로 모든 동작은 이 파일에서 이벤트로 연결한다.
 */
(function () {
  'use strict';

  var base = document.body.getAttribute('data-base') || './';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function fmtInt(v) {
    if (v === null || v === undefined || v === '') { return '-'; }
    var n = Number(v);
    if (isNaN(n)) { return String(v); }
    return n.toLocaleString('ko-KR', { maximumFractionDigits: 0 });
  }
  function fmtSigned(v) {
    if (v === null || v === undefined || v === '') { return '-'; }
    var n = Number(v);
    if (isNaN(n)) { return String(v); }
    return (n > 0 ? '+' : '') + n.toLocaleString('ko-KR', { maximumFractionDigits: 0 });
  }
  function fmtPct(v) {
    if (v === null || v === undefined || v === '') { return '-'; }
    var n = Number(v);
    if (isNaN(n)) { return String(v); }
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }
  function signClass(v) {
    var n = Number(v);
    if (v === null || v === undefined || v === '' || isNaN(n)) { return 'v-flat'; }
    return n > 0 ? 'v-up' : (n < 0 ? 'v-down' : 'v-flat');
  }
  function setSign(el, cls) {
    if (!el) { return; }
    el.classList.remove('v-up', 'v-down', 'v-flat');
    el.classList.add(cls);
  }

  /* ------------------------------------------------ 모바일 메뉴 · 드롭다운 */
  var navToggle = $('#navtoggle');
  var mainNav = $('#mainnav');
  if (navToggle && mainNav) {
    navToggle.addEventListener('click', function () {
      var open = mainNav.classList.toggle('is-open');
      navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }
  // 하나를 열면 나머지는 닫는다(데스크톱 기준).
  $all('details.dd').forEach(function (d) {
    d.addEventListener('toggle', function () {
      if (!d.open) { return; }
      $all('details.dd').forEach(function (o) { if (o !== d) { o.open = false; } });
    });
  });
  document.addEventListener('click', function (e) {
    if (window.innerWidth <= 760) { return; }
    if (e.target.closest && e.target.closest('details.dd')) { return; }
    $all('details.dd').forEach(function (o) { o.open = false; });
  });

  /* ------------------------------------------------ 거래 분석 "상세" 행 펼치기
   * 상세 내용이 표의 좁은 마지막 칸에 갇혀 가로 스크롤 없이는 안 보이던 문제 —
   * <summary>만 원래 칸에 두고, 실제 내용은 표 전체 너비의 다음 행(rowdet-line)에 둔다. */
  $all('details.rowdet[data-target]').forEach(function (d) {
    var target = document.getElementById(d.getAttribute('data-target'));
    if (!target) { return; }
    d.addEventListener('toggle', function () { target.hidden = !d.open; });
  });

  /* ------------------------------------------------ select 자동 제출 */
  $all('select[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      if (sel.form) { sel.form.submit(); }
    });
  });

  /* ------------------------------------------------ 표 정렬 */
  $all('table.tbl-sortable').forEach(function (table) {
    var ths = $all('thead th[data-sort]', table);
    ths.forEach(function (th, idx) {
      th.setAttribute('tabindex', '0');
      var handler = function () {
        var type = th.getAttribute('data-sort');
        var desc = th.classList.contains('sort-asc');
        ths.forEach(function (o) { o.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(desc ? 'sort-desc' : 'sort-asc');
        var tbody = table.tBodies[0];
        if (!tbody) { return; }
        var rows = Array.prototype.slice.call(tbody.rows);
        rows.sort(function (a, b) {
          var ca = a.cells[idx], cb = b.cells[idx];
          if (!ca || !cb) { return 0; }
          var va, vb;
          if (type === 'num') {
            va = parseFloat(ca.getAttribute('data-v') || ca.textContent.replace(/[^0-9.\-]/g, ''));
            vb = parseFloat(cb.getAttribute('data-v') || cb.textContent.replace(/[^0-9.\-]/g, ''));
            if (isNaN(va)) { va = -Infinity; }
            if (isNaN(vb)) { vb = -Infinity; }
          } else {
            va = ca.textContent.trim();
            vb = cb.textContent.trim();
            return desc ? vb.localeCompare(va, 'ko') : va.localeCompare(vb, 'ko');
          }
          return desc ? vb - va : va - vb;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      };
      th.addEventListener('click', handler);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
      });
    });
  });

  /* ------------------------------------------------ 자동 갱신 (GET JSON) */
  function apiUrl(r) { return base + 'api.php?r=' + encodeURIComponent(r); }

  function onUnauthenticated() {
    window.location.href = base + 'index.php?p=login&expired=idle';
  }

  function setText(key, text, cls) {
    var el = document.querySelector('[data-k="' + key + '"]');
    if (!el) { return; }
    el.textContent = text;
    if (cls) { setSign(el, cls); }
  }

  function renderStatusList(components) {
    var ul = document.querySelector('.statuslist[data-k="components"]');
    if (!ul || !components) { return; }
    ul.textContent = '';
    components.forEach(function (c) {
      var li = document.createElement('li');
      li.className = 'statusitem';
      var dot = document.createElement('span');
      dot.className = 'dot st-' + (c.status === 'ok' ? 'ok' : c.status === 'warn' ? 'warn' : c.status === 'error' ? 'error' : 'unknown');
      dot.setAttribute('aria-hidden', 'true');
      var name = document.createElement('span');
      name.className = 'st-name';
      name.textContent = c.component;
      var label = document.createElement('span');
      label.className = 'st-label st-' + (c.status === 'ok' ? 'ok' : c.status === 'warn' ? 'warn' : c.status === 'error' ? 'error' : 'unknown');
      label.textContent = c.label || '';
      var msg = document.createElement('span');
      msg.className = 'st-msg';
      msg.textContent = (c.message || '') + (c.note ? ' · ' + c.note : '');
      li.appendChild(dot); li.appendChild(name); li.appendChild(label); li.appendChild(msg);
      ul.appendChild(li);
    });
  }

  function renderEvents(events) {
    var ul = document.querySelector('.eventlist[data-k="events"]');
    if (!ul || !events) { return; }
    ul.textContent = '';
    events.forEach(function (ev) {
      var li = document.createElement('li');
      var t = document.createElement('span');
      t.className = 'ev-time';
      t.textContent = (ev.created_at || '').substring(5, 19);
      var b = document.createElement('span');
      b.className = 'badge badge-' + (ev.level === 'ERROR' ? 'err' : ev.level === 'WARN' ? 'warn' : ev.level === 'INFO' ? 'info' : 'muted');
      b.textContent = ev.level;
      var c = document.createElement('span');
      c.className = 'ev-cat';
      c.textContent = ev.category;
      var m = document.createElement('span');
      m.className = 'ev-msg';
      m.textContent = ev.message;   // textContent 사용 → XSS 불가
      li.appendChild(t); li.appendChild(b); li.appendChild(c); li.appendChild(m);
      ul.appendChild(li);
    });
  }

  function refreshDashboard() {
    fetch(apiUrl('dashboard'), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (res.status === 401) { onUnauthenticated(); return null; }
        if (!res.ok) { return null; }
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) { return; }
        var b = data.balance, t = data.holding_totals || {};
        setText('entr', b ? fmtInt(b.entr) : '-');
        setText('ord_alow_amt', b ? fmtInt(b.ord_alow_amt) : '-');
        setText('tot_evlt_amt', b ? fmtInt(b.tot_evlt_amt) : fmtInt(t.evlt_amt));
        setText('tot_pur_amt', b ? fmtInt(b.tot_pur_amt) : fmtInt(t.pur_amt));
        var pl = b ? b.tot_evlt_pl : t.evltv_prft;
        setText('tot_evlt_pl', fmtSigned(pl), signClass(pl));
        var rt = (b && b.tot_prft_rt !== null && b.tot_prft_rt !== undefined) ? b.tot_prft_rt : t.prft_rt;
        setText('tot_prft_rt', fmtPct(rt), signClass(rt));
        setText('holding_cnt', fmtInt(t.cnt));
        // 상장폐지 종목 제외 총수익률(해당 요소가 있을 때만 갱신)
        if (t.prft_rt_ex !== null && t.prft_rt_ex !== undefined) {
          setText('prft_rt_ex', fmtPct(t.prft_rt_ex), signClass(t.prft_rt_ex));
        }
        // 오늘 Claude 검토 요약 한 줄
        if (data.llm) {
          setText('llm_summary', '호출 ' + fmtInt(data.llm.calls) + '건 · 차단 ' + fmtInt(data.llm.blocks)
            + '건 · 오류 ' + fmtInt(data.llm.errors) + '건 · 캐시 ' + fmtInt(data.llm.cache_hits) + '건');
        }
        // 오늘 산업 트렌드 스캔 한 줄 (해당 요소가 이미 있을 때만 갱신)
        if (data.trend) {
          setText('trend_summary', '후보 ' + fmtInt(data.trend.candidates) + '개 (신호 '
            + fmtInt(data.trend.signals) + '개)');
        }
        if (data.today) {
          setText('today_pl', fmtSigned(data.today.pl_amt), signClass(data.today.pl_amt));
          setText('today_orders', fmtInt(data.today.orders));
          setText('today_signals', fmtInt(data.today.signals));
          // 최근 24시간 주문 실패·거부 요약(해당 요소가 이미 있을 때만 갱신)
          if (data.today.fail_24h !== undefined) { setText('fail_24h', fmtInt(data.today.fail_24h)); }
        }
        renderStatusList(data.components);
        renderEvents(data.events);
        var up = document.getElementById('dash-updated');
        if (up) { up.textContent = '갱신: ' + (data.server_time || '').substring(11); }
      })
      .catch(function () { /* 네트워크 오류는 다음 주기에 재시도 */ });
  }

  var dash = document.getElementById('dash');
  if (dash) {
    var sec = parseInt(dash.getAttribute('data-refresh'), 10);
    if (!isNaN(sec) && sec > 0) {
      setInterval(function () {
        if (!document.hidden) { refreshDashboard(); }
      }, sec * 1000);
    }
  }

  /* 서버 상태 화면: 상태/경과 컬럼만 주기적으로 갱신 (페이지 리로드 없음) */
  var statusBody = document.querySelector('tbody[data-k="status-rows"]');
  if (statusBody) {
    var updateStatus = function () {
      if (document.hidden) { return; }
      fetch(apiUrl('status'), { credentials: 'same-origin' })
        .then(function (res) {
          if (res.status === 401) { onUnauthenticated(); return null; }
          return res.ok ? res.json() : null;
        })
        .then(function (data) {
          if (!data || !data.ok || !data.components) { return; }
          data.components.forEach(function (c) {
            var tr = statusBody.querySelector('tr[data-c="' + c.component + '"]');
            if (!tr) { return; }
            var cls = 'st-' + (c.status === 'ok' ? 'ok' : c.status === 'warn' ? 'warn' : c.status === 'error' ? 'error' : 'unknown');
            var dot = tr.querySelector('.dot');
            var lab = tr.querySelector('.st-label');
            var msg = tr.querySelector('.cell-msg');
            var upd = tr.querySelector('.cell-upd');
            var age = tr.querySelector('.cell-age');
            if (dot) { dot.className = 'dot ' + cls; }
            if (lab) { lab.className = 'st-label ' + cls; lab.textContent = c.label || ''; }
            if (msg) { msg.textContent = (c.message || '-') + (c.note ? ' · ' + c.note : ''); }
            if (upd) { upd.textContent = c.updated_at || '-'; }
            if (age) { age.textContent = (c.age_sec === null || c.age_sec === undefined) ? '-' : fmtInt(c.age_sec) + '초'; }
          });
        })
        .catch(function () {});
    };
    setInterval(updateStatus, 30000);
  }

  /* ------------------------------------------------ 자동거래 제어: 아이디·비밀번호 재확인 모달
   * 서버 GUI(stock_svr) 와 동일한 방향 — 고정 문구("REAL") 대신 로그인 아이디+비밀번호 재입력을
   * 요구한다. 실제 검증은 index.php 의 auto_trading_start 핸들러가 password_verify 로 다시
   * 수행하므로, 이 JS 는 "완전히 비어있는 입력"만 막는 사용성 보조일 뿐이다. */
  var startBtn = $('#btnAutoStart');
  var dlg = $('#realConfirmDialog');
  if (startBtn && dlg && typeof dlg.showModal === 'function') {
    var usernameInput = $('#confirmUsername', dlg);
    var passwordInput = $('#confirmPassword', dlg);
    var okBtn = $('#realConfirmOk', dlg);
    var cancelBtn = $('#realConfirmCancel', dlg);
    var startForm = $('#startForm');

    function checkInput() {
      okBtn.disabled = (usernameInput.value.trim() === '' || passwordInput.value === '');
    }
    startBtn.addEventListener('click', function () {
      passwordInput.value = '';
      checkInput();
      dlg.showModal();
      passwordInput.focus();
    });
    usernameInput.addEventListener('input', checkInput);
    passwordInput.addEventListener('input', checkInput);
    cancelBtn.addEventListener('click', function () { dlg.close(); });
    okBtn.addEventListener('click', function () {
      if (usernameInput.value.trim() === '' || passwordInput.value === '') { return; }
      dlg.close();
      startForm.requestSubmit ? startForm.requestSubmit() : startForm.submit();
    });
  }
})();
