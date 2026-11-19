/* 공용 로딩 표시 — 데이터를 가져오는 동안(fetch 진행 중) 화면을 어둡게(딤) 하고 가운데에 프로그레스 링을 보여 준다.
 *
 * - 요청이 300ms 넘게 걸릴 때만 보인다(짧은 요청에 깜빡이지 않게). 모든 요청이 끝나면 사라진다.
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
  var inflight = 0, quietDepth = 0, timer = null, overlay = null, label = null;

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
  }
  function hide() {
    clearTimeout(timer); timer = null;
    if (!overlay) return;
    overlay.classList.remove('on');
    document.body.removeAttribute('aria-busy');
  }
  function begin() {
    inflight += 1;
    if (inflight === 1 && !timer) timer = setTimeout(function () { timer = null; if (inflight > 0) show(); }, SHOW_DELAY_MS);
  }
  function end() {
    inflight = Math.max(0, inflight - 1);
    if (inflight === 0) hide();
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

  window.addEventListener('beforeunload', function () { show(lang() === 'en' ? 'Loading…' : '화면을 불러오는 중…'); });
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
