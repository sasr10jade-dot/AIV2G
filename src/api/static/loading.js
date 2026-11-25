/* 공용 로딩 표시 — 데이터를 가져오는 동안(fetch 진행 중) 화면을 어둡게(딤) 하고 가운데에 프로그레스 링을 보여 준다.
 *
 * - 요청이 300ms 넘게 걸릴 때만 보인다(짧은 요청에 깜빡이지 않게). 모든 요청이 끝나고 0.4초 동안 새 요청이 없으면 사라진다
 *   (요청이 이어서 나가는 화면에서 링이 껐다 켜지며 여러 번 도는 것을 막는다).
 * - 새로고침·이동 때 켠 링은 다음 화면이 이어받아 바로 보여 주고, 화면 로딩이 끝나면 정리한다(한 번만 도는 것처럼 보이게).
 * - 새로고침·다른 화면으로 이동할 때(beforeunload)는 바로 보인다.
 * - 백그라운드 자동 새로고침처럼 화면을 가리면 안 되는 요청은 제외한다:
 *     fetch(url, { axQuiet: true })            // 요청 하나만 제외
 *     axLoading.quiet(async () => { ... })     // 그 안의 요청 전부 제외
 *   /health 같은 상태 확인 요청도 기본으로 제외한다.
 * - 화면 읽기 프로그램용으로 aria-busy·role=status를 쓰고, 움직임 줄이기 설정이면 회전 애니메이션을 끈다.
 */
(function () {
  if (window.axLoading) return;
  var QUIET_PATHS = ['/health', '/auth/quick-login/available', '/sw.js', '/manifest.webmanifest'];
  var SHOW_DELAY_MS = 300;
  var HIDE_GRACE_MS = 400;
  var CARRY_KEY = 'ax-loading-carry';
  var inflight = 0, quietDepth = 0, timer = null, hideTimer = null, overlay = null, label = null, visible = false;

  var css = '' +
    '.ax-loading{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;' +
    'background:rgba(11,21,48,.38);backdrop-filter:blur(1.5px);-webkit-backdrop-filter:blur(1.5px);opacity:0;transition:opacity .18s ease;pointer-events:none}' +
    '.ax-loading.on{opacity:1;pointer-events:auto;cursor:progress}' +
    '.ax-loading-box{display:flex;flex-direction:column;align-items:center;gap:12px;padding:22px 26px;border-radius:16px;' +
    'background:var(--card,#fff);color:var(--text,#0F1B33);box-shadow:0 10px 30px rgba(0,0,0,.18);min-width:150px}' +
    '.ax-loading-ring{width:46px;height:46px;animation:ax-spin .9s linear infinite}' +
    '.ax-loading-ring circle{fill:none;stroke-width:5;stroke-linecap:round}' +
    '.ax-loading-ring .track{stroke:var(--tint,#E8F0FD)}' +
    '.ax-loading-ring .arc{stroke:var(--accent,#1F5EDB);stroke-dasharray:90 150}' +
    '.ax-loading-text{font-size:13px;font-weight:600;color:var(--muted,#5B6B88);font-family:var(--font,sans-serif)}' +
    '@keyframes ax-spin{to{transform:rotate(360deg)}}' +
    '@media (prefers-reduced-motion:reduce){.ax-loading-ring{animation-duration:2.4s}}';

  function build() {
    if (overlay) return;
    var st = document.createElement('style');
    st.textContent = css;
    document.head.appendChild(st);
    overlay = document.createElement('div');
    overlay.className = 'ax-loading';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.innerHTML = '<div class="ax-loading-box"><svg class="ax-loading-ring" viewBox="0 0 50 50" aria-hidden="true">' +
      '<circle class="track" cx="25" cy="25" r="20"></circle><circle class="arc" cx="25" cy="25" r="20"></circle></svg>' +
      '<div class="ax-loading-text">불러오는 중…</div></div>';
    label = overlay.querySelector('.ax-loading-text');
    document.body.appendChild(overlay);
  }

  function lang() { return (document.documentElement.getAttribute('lang') || 'ko').slice(0, 2); }
  function show(text) {
    if (!document.body) return;
    build();
    label.textContent = text || (lang() === 'en' ? 'Loading…' : '불러오는 중…');
    overlay.classList.add('on');
    document.body.setAttribute('aria-busy', 'true');
    visible = true;
  }
  function hide() {
    clearTimeout(timer); timer = null;
    clearTimeout(hideTimer); hideTimer = null;
    visible = false;
    if (!overlay) return;
    overlay.classList.remove('on');
    document.body.removeAttribute('aria-busy');
  }
  function scheduleHide() {   // 바로 끄지 않고 잠깐 기다린다 — 그 사이 다음 요청이 오면 링을 이어서 돌린다
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { hideTimer = null; if (inflight === 0) hide(); }, HIDE_GRACE_MS);
  }
  function begin() {
    inflight += 1;
    clearTimeout(hideTimer); hideTimer = null;
    if (visible) return;   // 이미 보이는 중이면 지연 없이 이어 간다
    if (inflight === 1 && !timer) timer = setTimeout(function () { timer = null; if (inflight > 0) show(); }, SHOW_DELAY_MS);
  }
  function end() {
    inflight = Math.max(0, inflight - 1);
    if (inflight === 0) {
      clearTimeout(timer); timer = null;   // 300ms 안에 끝난 요청은 링을 띄우지 않는다
      if (visible) scheduleHide();
    }
  }
  function isQuiet(input, init) {
    if (quietDepth > 0 || (init && init.axQuiet)) return true;
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    try { url = new URL(url, location.href).pathname; } catch (e) { /* 상대 경로 그대로 */ }
    return QUIET_PATHS.indexOf(url) !== -1;
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (isQuiet(input, init)) return nativeFetch(input, init);
    begin();
    return nativeFetch(input, init).then(function (res) { end(); return res; }, function (err) { end(); throw err; });
  };

  window.addEventListener('beforeunload', function () {
    show(lang() === 'en' ? 'Loading…' : '화면을 불러오는 중…');
    try { sessionStorage.setItem(CARRY_KEY, String(Date.now())); } catch (e) { /* 저장 못 해도 동작에는 문제없다 */ }
  });
  // 직전 화면이 링을 켠 채 넘어왔으면(새로고침·이동) 이어서 바로 보여 주고, 화면 로딩이 끝난 뒤 남은 요청이 없으면 정리한다
  (function carryOver() {
    var at = 0;
    try { at = Number(sessionStorage.getItem(CARRY_KEY) || 0); sessionStorage.removeItem(CARRY_KEY); } catch (e) { at = 0; }
    if (!at || Date.now() - at > 10000) return;
    inflight += 1;   // 화면 로딩을 요청 하나로 셈
    var start = function () { show(lang() === 'en' ? 'Loading…' : '화면을 불러오는 중…'); };
    if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
    window.addEventListener('load', function () { setTimeout(end, 50); });
  })();
  window.addEventListener('pageshow', function (e) { if (e.persisted) { inflight = 0; hide(); } });   // 뒤로 가기 캐시 복원 시 남은 딤 제거

  window.axLoading = {
    show: show,
    hide: function () { inflight = 0; hide(); },
    quiet: async function (fn) {
      quietDepth += 1;
      try { return await fn(); } finally { quietDepth -= 1; }
    },
  };
})();
