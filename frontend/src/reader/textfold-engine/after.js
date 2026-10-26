/* TextFold 引擎宿主握手（宿主外壳适配，非上游逐字；来源 TextFoldPane AFTER_SRC 外链化，见 VENDOR.md）。
 * CSP：同源外链脚本。inst 从 #tf-config 读取（与 bridge.js 同一来源）。 */
(function () {
  'use strict';
  var INST = '';
  try {
    INST = String((JSON.parse(document.getElementById('tf-config').textContent) || {}).inst || '');
  } catch (e) { INST = ''; }
  var r = window.__reader;
  var emit = function (ev) { try { parent.postMessage(Object.assign({ __tf: 1, inst: INST }, ev), '*'); } catch (e2) {} };
  if (!r) { emit({ type: 'error', message: 'TextFold 内核初始化失败（window.__reader 未就绪）' }); return; }
  var orig = r.setLevel.bind(r);
  r.setLevel = function (l) { emit({ type: 'level', level: l }); return orig(l); };
  var doc = document.getElementById('doc');
  if (doc) {
    doc.addEventListener('mouseover', function (e) {
      var u = e.target.closest && e.target.closest('.u');
      emit({ type: 'hover', uid: u ? (u.dataset.u || u.id) : null });
    }, { passive: true });
    doc.addEventListener('mouseleave', function () { emit({ type: 'hover', uid: null }); });
    doc.addEventListener('click', function (e) {
      var u = e.target.closest && e.target.closest('.u');
      if (u && !(e.target.closest && e.target.closest('button, a, mark.hl, .npop, .selbar'))) emit({ type: 'activate', uid: u.dataset.u || u.id });
    });
  }
  emit({ type: 'ready' });   // 内核初始化成功的真握手（替代定时器假 ready）
})();
