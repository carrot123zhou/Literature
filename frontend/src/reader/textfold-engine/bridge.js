/* Literature host bridge. Executed as a same-origin external resource under Tauri CSP. */
(function () {
  'use strict';
  var INST = JSON.parse(document.getElementById('tf-config').textContent).inst;
  var pending = new Map(), seq = 0, muteScrollUntil = 0, scrollFrame = 0;
  window.__litHostMenu = true;
  function emit(ev) { parent.postMessage(Object.assign({ __tf: 1, inst: INST }, ev), '*'); }
  function fail(error) { emit({type:'operation-error', message:String(error.message || error)}); }
  window.litFetchBridge = function(path, init) {
    return new Promise(function(resolve, reject) {
      var reqId = 'r' + (++seq);
      pending.set(reqId, {resolve:resolve, reject:reject});
      emit({type:'rpc', reqId:reqId, path:path, method:(init && init.method) || 'GET', body:init && init.body});
      setTimeout(function() { if(pending.has(reqId)) { pending.delete(reqId); reject(new Error('宿主桥超时')); } }, 15000);
    });
  };
  function locate(uid) {
    var el = document.getElementById(uid);
    if (el && el.getClientRects().length && el.getBoundingClientRect().height) return el;
    var data = JSON.parse(document.getElementById('paper-data').textContent);
    var c = data.chunks.find(function(c) { return c.id === uid || c.units.indexOf(uid) >= 0 || c.block_id === uid; });
    return c && document.getElementById(c.id);
  }
  function moveTo(uid, ratio) {
    var el = locate(uid); if (!el) return;
    var r = el.getBoundingClientRect(); muteScrollUntil = Date.now() + 180;
    window.scrollTo(0, Math.max(0, window.scrollY + r.top + ratio * r.height - innerHeight * .3));
  }
  window.addEventListener('message', function(e) {
    if(e.source !== parent) return;
    var d=e.data || {}; if(d.inst !== INST) return;
    try {
      if(d.__tfresp && pending.has(d.reqId)) {
        var p=pending.get(d.reqId); pending.delete(d.reqId); if(d.error) p.reject(new Error(d.error)); else p.resolve(d.data); return;
      }
      if(d.__lit==='apply-notes') { window.__tfApply.applyNotes(d.notes); return; }
      if(d.__lit==='set-open') { window.__tfApply[d.kind==='section'?'setSectionOpen':'setUnitOpen'](d.id,d.open); return; }
      if(d.__lit==='scroll-to') { moveTo(d.uid,d.ratio || 0); return; }
      if(d.__lit==='goto') { moveTo(d.uid,.5); return; }
      if(d.__lit==='light') {
        document.querySelectorAll('.u.tf-lit').forEach(function(el) {el.classList.remove('tf-lit');});
        var u=d.on && document.getElementById(d.uid); if(u) u.classList.add('tf-lit');
      }
    } catch(error) { fail(error); }
  });
  window.addEventListener('lit-open', function(e) { emit(Object.assign({type:'open-change'},e.detail)); });
  function editMark(e) {
    var m=e.target.closest && e.target.closest('.hl,.hlsp,.ncard');
    if(!m || !window.__tfApply) return false;
    var n=m.classList.contains('ncard') ? window.__tfNotes.notes().find(function(n){return n.id===m.dataset.note;}) : window.__tfApply.noteAt(m);
    if(!n) return false;
    e.preventDefault();e.stopImmediatePropagation();
    var r=m.getBoundingClientRect();emit({type:'edit-note',id:n.id,rect:{left:r.left,top:r.top,width:r.width,height:r.height}});return true;
  }
  document.addEventListener('contextmenu', function(e) {
    if(editMark(e)) return;
    var u=e.target.closest && e.target.closest('.u,.stake,.hd'); if(!u) return;
    e.preventDefault();
    try {var r=document.createRange();r.selectNodeContents(u._tx || u);var s=getSelection();s.removeAllRanges();s.addRange(r);reportSelection();}catch(error){fail(error);}
  },true);
  document.addEventListener('click',function(e){
    if(e.target.closest && e.target.closest('.hl,.hlsp,.ncard')) { e.preventDefault();e.stopImmediatePropagation();return; }
    var st=e.target.closest && e.target.closest('.stake');
    if(st && !String(getSelection())) {var section=st.closest('section.chunk');if(section){e.preventDefault();e.stopImmediatePropagation();window.__tfApply.toggleSection(section.id);}}
  },true);
  function reportSelection() {
    try {
      var s=getSelection();if(!s || s.isCollapsed || !s.rangeCount){emit({type:'sel-clear'});return;}
      var text=String(s), rect=s.getRangeAt(0).getBoundingClientRect();
      var anchors=window.__tfNotes.selectionAnchors();if(!anchors.length)return;
      s=getSelection();if(s.rangeCount){var rr=s.getRangeAt(0).getBoundingClientRect();if(rr.height)rect=rr;}
      emit({type:'selection',text:text,anchor:anchors[0],rect:{left:rect.left,top:rect.top,width:rect.width,height:rect.height}});
    }catch(error){fail(error);}
  }
  document.addEventListener('mouseup',function(e){if(e.button!==2)setTimeout(reportSelection,0);},true);
  document.addEventListener('scroll',function(){
    if(scrollFrame || Date.now()<muteScrollUntil)return;
    scrollFrame=requestAnimationFrame(function(){
      scrollFrame=0;if(Date.now()<muteScrollUntil)return;
      var y=innerHeight*.3, best=null, delta=Infinity;
      document.querySelectorAll('#doc .u, #doc .stake').forEach(function(el){
        var r=el.getBoundingClientRect();if(!r.height || !el.getClientRects().length)return;
        var dist=r.top<=y && r.bottom>=y ? 0 : Math.min(Math.abs(r.top-y),Math.abs(r.bottom-y));
        if(dist<delta){best=el;delta=dist;}
      });
      if(best){var r=best.getBoundingClientRect(), section=best.closest('section.chunk');emit({type:'scroll',uid:best.dataset.u || best.id || (section && section.id),ratio:Math.max(0,Math.min(1,(y-r.top)/r.height))});}
    });
  },{capture:true,passive:true});
  // Shared five-colour palette; retain the engine's Full/Key/Takeaway tone classes and strengths.
  var paintFrame=0;
  function paintNotes() {
    if(paintFrame)return;
    paintFrame=requestAnimationFrame(function(){paintFrame=0;if(!window.__tfApply)return;
      var hues={y:'yellow',g:'green',b:'blue',p:'purple',o:'orange'};
      document.querySelectorAll('#doc .hl,#doc .hlsp').forEach(function(el){
        var note=window.__tfApply.noteAt(el);if(!note || !hues[note.color])return;
        var fill='var(--c-'+hues[note.color]+')';
        el.style.setProperty('--hl',fill);el.style.setProperty('--hl-key',fill);el.style.setProperty('--hl-take',fill);
        el.dataset.annId=note.id;
      });
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    var doc=document.getElementById('doc');if(doc)new MutationObserver(paintNotes).observe(doc,{childList:true,subtree:true});paintNotes();
  });
  var realFetch=window.fetch.bind(window);
  window.fetch=function(input,init){var url=typeof input==='string'?input:(input && input.url)||'';
    if(url.indexOf('/api/notes/')===0 || url.indexOf('/api/ask')===0)return window.litFetchBridge(url,init).then(function(data){return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});},function(error){return new Response(JSON.stringify({error:String(error.message||error)}),{status:502,headers:{'Content-Type':'application/json'}});});
    return realFetch(input,init);
  };
})();
