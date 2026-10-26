/* TextFold iframe 桥（宿主外壳适配，非上游逐字；来源 TextFoldPane BRIDGE_SRC 外链化，见 VENDOR.md）。
 * CSP：本文件为同源外链脚本（script-src 'self'），替代原内联实现。
 * inst 从 #tf-config（application/json，非执行载荷）读取：多实例（原文/译文栏）路由互不串扰。 */
(function () {
  'use strict';
  var INST = '';
  try {
    INST = String((JSON.parse(document.getElementById('tf-config').textContent) || {}).inst || '');
  } catch (e) { INST = ''; }
  if (window.__drPerf === undefined) { try { window.__drPerf = []; } catch (e2) {} }
  var pending = new Map();   // reqId -> {resolve, reject}
  var reqSeq = 0;
  var emit = function (ev) { try { parent.postMessage(Object.assign({ __tf: 1, inst: INST }, ev), '*'); } catch (e) {} };
  // 宿主 RPC：内核的 /api/notes/*、/api/ask 由宿主真实处理（overlay 持久化/统一问答），不外发、不伪造
  window.litFetchBridge = function (path, init) {
    return new Promise(function (resolve, reject) {
      var reqId = 'r' + (++reqSeq);
      pending.set(reqId, { resolve: resolve, reject: reject });
      emit({ type: 'rpc', reqId: reqId, path: path, method: (init && init.method) || 'GET', body: init && init.body });
      window.setTimeout(function () {
        if (pending.has(reqId)) { pending.delete(reqId); reject(new Error('宿主桥超时')); }
      }, 15000);
    });
  };
  window.addEventListener('message', function (e) {
    if (e.source !== parent) return;                       // 只信宿主
    var d = e.data || {};
    if (d.__lit === 'light') return litUnit(d.uid, d.on);
    if (d.__lit === 'goto') return gotoUnit(d.uid);
    if (d.__tfresp && pending.has(d.reqId)) {
      var p = pending.get(d.reqId); pending.delete(d.reqId);
      if (d.error) p.reject(new Error(d.error)); else p.resolve(d.data);
    }
  });
  function litUnit(uid, on) {
    try {
      document.querySelectorAll('.u.tf-lit').forEach(function (el) { el.classList.remove('tf-lit'); });
      if (on && uid) { var u = document.getElementById(uid); if (u) u.classList.add('tf-lit'); }
    } catch (e) {}
  }
  function gotoUnit(uid) {
    try {
      var u = document.getElementById(uid);
      if (!u) return false;
      u.scrollIntoView({ block: 'center', behavior: 'smooth' });
      litUnit(uid, true);
      return true;
    } catch (e2) { return false; }
  }
  // 右键 .u：选中整段 + 触发内核 mouseup → 内核原生 selbar（划词/右键同一菜单）
  document.addEventListener('contextmenu', function (e) {
    var u = e.target.closest && e.target.closest('.u');
    if (!u || !u._tx) return;
    e.preventDefault();
    try {
      var r = document.createRange(); r.selectNodeContents(u._tx);
      var s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.getElementById('doc').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    } catch (e3) {}
  }, true);
  // 真实网络钩子：改路由到宿主（由桥重写 fetch；此处只替换内核用到的 fetch 包装）
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.indexOf('/api/notes/') === 0 || url.indexOf('/api/ask') === 0) {
      return window.litFetchBridge(url, init).then(
        function (data) { return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }); },
        function (error) { return new Response(JSON.stringify({ error: String(error && error.message || error) }), { status: 502, headers: { 'Content-Type': 'application/json' } }); }
      );
    }
    return realFetch(input, init);
  };
})();
