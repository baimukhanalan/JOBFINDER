/* JobFinder — premium animation layer (scroll-reveal + entrance), additive.
 *
 * Loaded once from the shared <head> (see mailcrm_ui._HEAD_PWA), so it runs on every
 * server-rendered screen (main CRM, login, manager portal, employee cabinet) without
 * a single component file being touched. Everything here is defensive by design:
 * a thrown error, a missing element, or JS never loading at all must never leave any
 * content invisible — the CSS baseline (backend/static/anim.css) only hides an
 * element AFTER this script explicitly opts it in, so "no JS" == "everything visible".
 *
 * Re-triggering after the in-place tab switch (jfSwap, defined in mailcrm_ui.py _JS):
 * jfSwap always ends by doing `main.innerHTML = <new markup>`, and the same list
 * views append rows into #maillist/#mbxlist via insertAdjacentHTML. Rather than
 * special-casing jfSwap (fragile — it isn't always present, e.g. on /catalog, and a
 * future rename would silently break the hook), a MutationObserver on <main>
 * (childList+subtree) catches every one of those cases generically.
 */
(function(){
  'use strict';
  try{
    var reduced = false;
    try{ reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; }catch(_){}

    // Under reduced motion we don't hide anything at all — no observer, no delay,
    // content is exactly what the server rendered. This is the simplest way to
    // "fully respect" prefers-reduced-motion (nothing to fail-open from).
    if(reduced) return;

    // ---- candidate detection -----------------------------------------------------
    // Explicit, known component classes (correctness) + a generic single-class
    // fallback (`[class$=...]`, genericity for future components) — see the anim
    // task notes: a bare substring/"contains" match on class names is NOT safe in
    // this codebase (e.g. ".narrow-section" contains "row", ".card-field-input"
    // contains "card"); matching only on a whole class TOKEN avoids that.
    var CARD_SUFFIX = /(?:^|-)(?:card|row|cell|tile)$/;
    var EXTRA_TOKENS = {mitem:1, item:1};
    // components known to legitimately end in one of those suffixes without a
    // hyphen (mbxrow, mrow, tcard, dfcard, rlrow, msgcard …) — the regex above
    // already matches a bare "row"/"card"/"cell"/"tile" token AND a
    // hyphen-prefixed one, so plain "mbxrow" needs its own allowance since it has
    // neither form; keep a short explicit list for those.
    var EXTRA_CLASSES = ['mbxrow','mrow','tcard','dfcard','rlrow','msgcard','mhm-row',
      'mh-crow','unf-toprow','st-fn-row','st-h-row'];
    var EXCLUDE_ANCESTOR_SEL = '.iv-grid, table, .jf-skeleton';

    function isCandidate(el){
      if(!el || el.nodeType !== 1) return false;
      var cls = el.className;
      if(typeof cls !== 'string' || !cls) return false;
      if(el.hasAttribute('data-jfa-done')) return false;
      var toks = cls.split(/\s+/);
      var hit = false;
      for(var i = 0; i < toks.length; i++){
        var t = toks[i];
        if(!t) continue;
        if(EXTRA_TOKENS[t] || CARD_SUFFIX.test(t) || EXTRA_CLASSES.indexOf(t) !== -1){ hit = true; break; }
      }
      if(!hit) return false;
      try{ if(el.closest(EXCLUDE_ANCESTOR_SEL)) return false; }catch(_){}
      return true;
    }

    function collect(root){
      var out = [];
      if(!root) return out;
      if(isCandidate(root)) out.push(root);
      if(root.querySelectorAll){
        var all = root.querySelectorAll('*');
        for(var i = 0; i < all.length; i++){ if(isCandidate(all[i])) out.push(all[i]); }
      }
      // section headers inside <main> — a lighter, opacity-only reveal.
      if(root.querySelectorAll){
        var heads = root.matches && (root.matches('main h2') || root.matches('main h3'))
          ? [root] : root.querySelectorAll('main h2, main h3, h2, h3');
        for(var j = 0; j < heads.length; j++){
          var h = heads[j];
          if(!h.hasAttribute('data-jfa-done') && !h.closest('.modal, .page-head')){
            out.push(h);
          }
        }
      }
      return out;
    }

    // ---- reveal engine -------------------------------------------------------------
    var io = null;
    try{
      io = new IntersectionObserver(onIntersect, {root: null, rootMargin: '0px 0px -6% 0px', threshold: 0.01});
    }catch(_){ io = null; }

    function isHeading(el){ return el.tagName === 'H2' || el.tagName === 'H3'; }

    function markPending(el, i){
      if(el.hasAttribute('data-jfa-done')) return;
      el.setAttribute('data-jfa-done', '1');
      el.classList.add(isHeading(el) ? 'jfa-ro' : 'jfa-r');
      // small, capped stagger so a long list doesn't take forever to finish revealing
      var delay = Math.min((i || 0) * 40, 260);
      if(delay) el.style.transitionDelay = delay + 'ms';
    }

    function reveal(el){
      // double-rAF: let the browser paint the hidden state first, then flip — a
      // same-frame class swap wouldn't be seen as a transition at all.
      requestAnimationFrame(function(){
        requestAnimationFrame(function(){
          try{
            el.classList.add('jfa-in');
            setTimeout(function(){
              try{
                el.classList.remove('jfa-r','jfa-ro');
                el.style.transitionDelay = '';
              }catch(_){}
            }, 900);
          }catch(_){}
        });
      });
    }

    function onIntersect(entries){
      var i = 0;
      entries.forEach(function(entry){
        if(!entry.isIntersecting) return;
        io && io.unobserve(entry.target);
        reveal(entry.target);
        i++;
      });
    }

    function inViewport(el){
      try{
        var r = el.getBoundingClientRect();
        var vh = window.innerHeight || document.documentElement.clientHeight;
        return r.top < vh && r.bottom > 0;
      }catch(_){ return true; }
    }

    function scan(root){
      try{
        var els = collect(root);
        var aboveFold = [], belowFold = [];
        for(var i = 0; i < els.length; i++){
          (inViewport(els[i]) ? aboveFold : belowFold).push(els[i]);
        }
        aboveFold.forEach(function(el, i){ markPending(el, i); reveal(el); });
        belowFold.forEach(function(el, i){
          markPending(el, i % 8);
          if(io) io.observe(el);
          else reveal(el); // no IO support — just show it, never block content
        });
      }catch(_){
        // Never let a scan failure leave anything hidden.
        try{
          (root && root.querySelectorAll ? root : document).querySelectorAll('.jfa-r,.jfa-ro').forEach(function(el){
            el.classList.add('jfa-in');
          });
        }catch(__){}
      }
    }

    // ---- initial pass + jfSwap / infinite-scroll re-trigger -----------------------
    function boot(){
      var main = document.querySelector('main');
      scan(main || document.body);
      if(!main) return;
      try{
        var mo = new MutationObserver(function(records){
          for(var i = 0; i < records.length; i++){
            var added = records[i].addedNodes;
            for(var j = 0; j < added.length; j++){
              if(added[j].nodeType === 1) scan(added[j]);
            }
          }
        });
        mo.observe(main, {childList: true, subtree: true});
      }catch(_){}
    }

    if(document.readyState === 'loading'){
      document.addEventListener('DOMContentLoaded', boot, {once: true});
    }else{
      boot();
    }

    // Safety net: a bfcache restore (Safari back/forward) can resurrect a page mid
    // animation-state; make sure nothing is stuck invisible.
    window.addEventListener('pageshow', function(e){
      if(e.persisted){
        document.querySelectorAll('.jfa-r,.jfa-ro').forEach(function(el){ el.classList.add('jfa-in'); });
      }
    });
  }catch(_){
    // Absolute last resort — never let this file's own failure hide content.
    try{
      document.querySelectorAll('.jfa-r,.jfa-ro').forEach(function(el){ el.classList.add('jfa-in'); });
    }catch(__){}
  }
})();
