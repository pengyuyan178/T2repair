(async()=>{
(function () {
  var log = [];
  function note(m) { log.push(m); }
  note('Prism global: ' + (typeof window.Prism !== 'undefined'));
  note('json lang before: ' + !!(window.Prism && Prism.languages && Prism.languages.json));
  if (window.Prism && !(Prism.languages && Prism.languages.json)) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', 'components/prism-json.js', false);
      xhr.send(null);
      if (xhr.status === 200 || xhr.status === 0) {
        (new Function(xhr.responseText))();
        note('loaded components/prism-json.js via sync XHR: ' + !!Prism.languages.json);
      } else {
        note('xhr status ' + xhr.status);
      }
    } catch (e) { note('xhr error: ' + e.message); }
  }
  var nl = String.fromCharCode(10);
  var src = ['{', '  "A": "/*",', '  "B": "B",', '  "C": "C"', '}'].join(nl);
  var pre = document.createElement('pre');
  var code = document.createElement('code');
  code.className = 'language-json';
  code.textContent = src;
  pre.appendChild(code);
  document.body.appendChild(pre);
  if (window.Prism && Prism.languages && Prism.languages.json) {
    Prism.highlightElement(code);
    note('highlightElement done');
    var html = document.createElement('pre');
    html.id = 'highlight-html';
    html.textContent = code.innerHTML;
    document.body.appendChild(html);
    var toks = Prism.tokenize(src, Prism.languages.json).map(function (t) {
      return (typeof t === 'string') ? { text: t } : { type: t.type, content: t.content };
    });
    var tokEl = document.createElement('pre');
    tokEl.id = 'token-dump';
    tokEl.textContent = JSON.stringify(toks);
    document.body.appendChild(tokEl);
  } else {
    note('json language unavailable in page');
  }
  var m = document.createElement('pre');
  m.id = 'probe-log';
  m.textContent = log.join(' | ');
  document.body.appendChild(m);
  document.title = 'json-comment-probe';
})();

})().then(()=>new Promise(r=>setTimeout(r,250))).then(()=>{state.scenario_complete=state.errors.length===0;state.canvas_count=document.querySelectorAll('canvas').length;state.body_text=document.body.innerText.slice(0,200);state.dom_elements=document.body.querySelectorAll(':not(script)').length;flush();});
