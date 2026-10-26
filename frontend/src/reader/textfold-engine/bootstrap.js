/* TextFold 引擎初始层 bootstrap（宿主外壳适配；替代原内联 try/setLevel，CSP 外链化）。
 * 初始层从 #tf-config 读取（application/json 非执行载荷）。内核初始化成功的真握手由
 * after.js 的 {type:'ready'} 承担——本文件只负责把初始层应用到已就绪的内核。 */
(function () {
  'use strict';
  var cfg = {};
  try {
    cfg = JSON.parse(document.getElementById('tf-config').textContent) || {};
  } catch (e) { cfg = {}; }
  var level = Number(cfg.initialLevel);
  if (!isFinite(level)) return;
  try {
    if (window.__reader && window.__reader.setLevel) window.__reader.setLevel(Math.max(0, Math.min(4, Math.round(level))));
  } catch (e2) { /* 内核未就绪：宿主折叠状态机不进入 ready（无假握手） */ }
})();
