"""catalogger viewer -- a lean, self-contained GUI.

Zero frontend build, zero node_modules, no CDN. A stdlib HTTP server (psycopg
is the only dependency) serves one embedded HTML page with the fuzzy finder
inlined. Bound to 127.0.0.1 only -- the archive holds live tokens.

    catalogger serve            # then open http://127.0.0.1:8765

Search syntax (mix freely):
    checkout.example          bare terms -> literal substring on host/url
    tech:f5-big-ip            exact tech tag (repeatable)
    body:"access denied"      full-text over request+response bodies
    status:403  method:POST  program:my-program
"""
from __future__ import annotations

import json
import os
import shlex
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import psycopg
import zstandard as zstd

_dctx = zstd.ZstdDecompressor()


def _dsn() -> str:
    dsn = os.environ.get("CATALOGGER_DSN")
    if not dsn:
        raise SystemExit("set CATALOGGER_DSN")
    return dsn


# -- query parsing -------------------------------------------------------------

def parse_query(q: str) -> dict:
    f = {"tech": [], "status": None, "program": None, "method": None,
         "body": None, "terms": []}
    try:
        toks = shlex.split(q or "")
    except ValueError:
        toks = (q or "").split()
    for tok in toks:
        if ":" in tok[1:]:
            k, _, v = tok.partition(":")
            k = k.lower()
            if k == "tech":
                f["tech"].append(v)
            elif k == "status" and v.isdigit():
                f["status"] = int(v)
            elif k == "program":
                f["program"] = v
            elif k == "method":
                f["method"] = v.upper()
            elif k in ("body", "content"):
                f["body"] = v
            elif k == "host":
                f["terms"].append(v)
            else:
                f["terms"].append(tok)
        else:
            f["terms"].append(tok)
    return f


def search_summaries(conn, q: str, limit: int = 300):
    f = parse_query(q)
    where, params = [], []
    if f["tech"]:
        where.append("f.fingerprints @> %s")
        params.append(f["tech"])
    if f["status"] is not None:
        where.append("f.status = %s")
        params.append(f["status"])
    if f["program"]:
        where.append("f.program = %s")
        params.append(f["program"])
    if f["method"]:
        where.append("f.method = %s")
        params.append(f["method"])
    if f["body"]:
        where.append("""(f.req_body_sha IN (SELECT sha256 FROM body_text WHERE tsv @@ plainto_tsquery('simple',%s))
                      OR f.resp_body_sha IN (SELECT sha256 FROM body_text WHERE tsv @@ plainto_tsquery('simple',%s)))""")
        params += [f["body"], f["body"]]
    for t in f["terms"]:
        # a bare term is a literal substring match on host/url (case-insensitive).
        # Body content is searched only via the explicit body:"..." filter, so
        # `au` returns URLs containing "au" -- not every flow with an a and a u.
        where.append("(f.host ILIKE %s OR f.url ILIKE %s)")
        params += [f"%{t}%", f"%{t}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT f.id, f.ts, f.method, f.status, f.host, f.path, f.url, f.fingerprints
        FROM flows f {clause}
        ORDER BY f.ts DESC LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [{"id": i, "ts": ts.isoformat(), "method": m, "status": s,
                 "host": h, "path": p, "url": u, "tech": fp}
                for i, ts, m, s, h, p, u, fp in cur.fetchall()]


def flow_detail(conn, flow_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.ts, f.program, f.source_tool, f.session_id, f.method,
                   f.host, f.path, f.query, f.url, f.status, f.duration_ms,
                   f.fingerprints, f.req_headers, f.resp_headers,
                   qb.content, qb.is_text, rb.content, rb.is_text
            FROM flows f
            LEFT JOIN bodies qb ON qb.sha256 = f.req_body_sha
            LEFT JOIN bodies rb ON rb.sha256 = f.resp_body_sha
            WHERE f.id = %s
        """, (flow_id,))
        row = cur.fetchone()
    if not row:
        return None
    (fid, ts, prog, tool, sess, method, host, path, query, url, status, dur,
     fps, rqh, rsh, qc, qt, rc, rt) = row
    return {
        "id": fid, "ts": ts.isoformat(), "program": prog, "source_tool": tool,
        "session_id": sess, "method": method, "host": host, "path": path,
        "query": query, "url": url, "status": status, "duration_ms": dur,
        "tech": fps, "req_headers": rqh or {}, "resp_headers": rsh or {},
        "req_body": _body(qc, qt), "resp_body": _body(rc, rt),
    }


def _body(content, is_text):
    if content is None:
        return {"empty": True}
    raw = content if isinstance(content, (bytes, bytearray)) else bytes(content)
    try:
        data = _dctx.decompress(raw)
    except zstd.ZstdError:
        data = raw
    if not data:
        return {"empty": True}
    if not is_text:
        return {"binary": True, "size": len(data)}
    return {"text": data.decode("utf-8", "replace"), "size": len(data)}


def facets(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT unnest(fingerprints) t, count(*) c FROM flows GROUP BY t ORDER BY c DESC LIMIT 30")
        return {"tech": [{"name": t, "count": c} for t, c in cur.fetchall()]}


# -- HTTP server ---------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = INDEX_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._html()
            with psycopg.connect(_dsn()) as conn:
                if u.path == "/api/search":
                    q = (qs.get("q") or [""])[0]
                    try:
                        lim = min(1000, max(1, int((qs.get("limit") or ["300"])[0])))
                    except ValueError:
                        lim = 300
                    return self._json(search_summaries(conn, q, lim))
                if u.path == "/api/facets":
                    return self._json(facets(conn))
                if u.path.startswith("/api/flow/"):
                    fid = int(u.path.rsplit("/", 1)[1])
                    d = flow_detail(conn, fid)
                    return self._json(d) if d else self._json({"error": "not found"}, 404)
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def serve(host="127.0.0.1", port=8765):
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"catalogger viewer -> http://{host}:{port}  (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


# -- the entire frontend: one file, no build, no CDN ---------------------------

INDEX_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>catalogger</title>
<style>
  :root{--bg:#0c0c0f;--fg:#d8d8dd;--dim:#6b7280;--sel:#15233b;--line:#1c1c22;--acc:#7dd3fc;--mark:#fde047}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
  #bar{padding:8px 10px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
  #q{flex:1;background:#000;border:1px solid var(--line);color:var(--fg);padding:7px 9px;border-radius:6px;font:inherit;outline:none}
  #q:focus{border-color:var(--acc)}
  #hint{color:var(--dim);font-size:11px;white-space:nowrap}
  #count{color:var(--dim);font-size:11px;min-width:60px;text-align:right}
  #main{display:grid;grid-template-columns:420px 1fr;height:calc(100vh - 49px)}
  #list{overflow:auto;border-right:1px solid var(--line)}
  .row{padding:6px 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row:hover{background:#101018}
  .row.sel{background:var(--sel)}
  .st{display:inline-block;width:30px;font-weight:700}
  .s2{color:#4ade80}.s3{color:#60a5fa}.s4{color:#fbbf24}.s5{color:#f87171}
  .me{color:var(--dim);width:46px;display:inline-block}
  .pa{color:var(--fg)}.ho{color:var(--acc)}
  .chip{display:inline-block;background:#1f2937;color:#9ca3af;border-radius:4px;padding:0 5px;margin-left:5px;font-size:10px}
  mark{background:var(--mark);color:#000;border-radius:2px}
  #detail{overflow:auto;padding:12px 14px}
  #detail h3{margin:14px 0 6px;font-size:11px;letter-spacing:1px;color:var(--dim)}
  #meta{color:var(--dim);font-size:11px;margin-bottom:4px}
  #meta b{color:var(--fg)}
  pre{white-space:pre-wrap;word-break:break-word;margin:0;background:#000;border:1px solid var(--line);border-radius:6px;padding:9px}
  .hk{color:var(--acc)}.empty{color:var(--dim)}
  .reqline{color:#a7f3d0}.statusline{font-weight:700}
</style></head><body>
<div id="bar">
  <input id="q" placeholder="substring host/url… · body:&quot;text&quot; · tech:f5-big-ip · status:403 · method:POST" autofocus>
  <span id="hint">↑↓ navigate · empty = live tail</span><span id="count"></span>
</div>
<div id="main"><div id="list"></div><div id="detail" class="empty">select a flow</div></div>
<script>
const $=s=>document.querySelector(s), q=$('#q'), list=$('#list'), detail=$('#detail'), count=$('#count');
const LIVE_LIMIT=50, SEARCH_LIMIT=300, POLL_MS=2000;
let results=[], view=[], sel=0, curId=null, polling=0, timer=0;
// empty query = "live tail": poll for new flows and keep the latest LIVE_LIMIT on top.
const isLive=()=>q.value.trim()==='';

// --- literal substring matcher (matches the server's ILIKE), client-side ---
// `au` highlights the literal "au" span(s), not scattered a's and u's.
function sub(needle, hay){
  if(!needle) return {score:0, pos:[]};
  const n=needle.toLowerCase(), h=hay.toLowerCase();
  let from=0, idx, pos=[], hits=0, first=-1;
  while((idx=h.indexOf(n, from))>=0){
    if(first<0) first=idx;
    for(let k=0;k<n.length;k++) pos.push(idx+k);
    hits++; from=idx+n.length;
  }
  if(!hits) return null;                          // no substring -> no match
  let score=100-Math.min(first,99)+hits;          // earlier + more hits rank higher
  if(first===0||'/.:-_?&='.includes(h[first-1])) score+=20; // word-boundary bonus
  return {score, pos};
}
// AND across bare terms; union highlight positions on host+path
function rankTerms(terms, r){
  const hay=(r.host+r.path);
  let total=0, pos=new Set();
  for(const t of terms){ const m=sub(t,hay); if(!m) return null; total+=m.score; m.pos.forEach(p=>pos.add(p)); }
  return {score:total, pos};
}
function bareTerms(){
  return q.value.split(/\s+/).filter(t=>t && !/^[a-z]+:/i.test(t));
}
function hl(str, posSet, off){
  let out='';
  for(let k=0;k<str.length;k++){ const c=str[k].replace(/[&<>]/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[x]));
    out += posSet&&posSet.has(off+k) ? '<mark>'+c+'</mark>' : c; }
  return out;
}
function render(){
  const terms=bareTerms();
  // The server already vetted every row (literal substring on host+url, or the
  // body: full-text filter). Rank/highlight by host+path; a term that only
  // lives in the query string stays a real match, it just won't highlight here.
  view = results.map(r=>{
    if(terms.length){ const m=rankTerms(terms,r);
      return {r, score:m?m.score:0, pos:m?m.pos:null}; }
    return {r, score:0, pos:null};
  });
  if(terms.length) view.sort((a,b)=>b.score-a.score);
  // keep the user's selected flow across live refreshes; otherwise clamp.
  const keep = curId!=null ? view.findIndex(v=>v.r.id===curId) : -1;
  sel = keep>=0 ? keep : Math.min(sel, Math.max(0,view.length-1));
  const scrollTop=list.scrollTop;
  list.innerHTML = view.map((v,idx)=>{
    const r=v.r, sc='s'+String(r.status||0)[0];
    const ho=hl(r.host, v.pos, 0), pa=hl(r.path, v.pos, r.host.length);
    const chips=(r.tech||[]).slice(0,3).map(t=>`<span class="chip">${t}</span>`).join('');
    return `<div class="row ${idx===sel?'sel':''}" data-i="${idx}">
      <span class="st ${sc}">${r.status??''}</span><span class="me">${r.method}</span>
      <span class="ho">${ho}</span><span class="pa">${pa}</span>${chips}</div>`;
  }).join('');
  list.scrollTop=scrollTop;                       // re-render shouldn't jump the list
  const cap=isLive()?LIVE_LIMIT:SEARCH_LIMIT;
  count.textContent=(isLive()?'● live · ':'')+view.length+(view.length>=cap?'+':'');
  if(!view.length){ detail.innerHTML='<span class="empty">no matches</span>'; curId=null; return; }
  showSel(false);
}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[x]));}
function bodyHtml(b, ct){
  if(!b||b.empty) return '<pre class="empty">(no body)</pre>';
  if(b.binary) return `<pre class="empty">&lt;binary, ${b.size} bytes&gt;</pre>`;
  let t=b.text;
  if(/json/.test(ct||'')){ try{ t=JSON.stringify(JSON.parse(t),null,2);}catch(e){} }
  return '<pre>'+esc(t)+'</pre>';
}
function headers(h){
  const ks=Object.keys(h||{}); if(!ks.length) return '';
  return '<pre>'+ks.map(k=>`<span class="hk">${esc(k)}</span>: ${esc(h[k])}`).join('\n')+'</pre>';
}
function select(idx, scroll){ if(idx<0||idx>=view.length) return; sel=idx; showSel(scroll); }
function showSel(scroll){
  [...list.children].forEach((el,i)=>el.classList.toggle('sel',i===sel));
  const r=view[sel]?.r; if(!r) return;
  if(scroll){ const cur=list.children[sel]; if(cur) cur.scrollIntoView({block:'nearest'}); }
  if(r.id!==curId) openDetail(r.id);              // skip the fetch if it's already shown
}
async function openDetail(id){
  curId=id;
  const d=await (await fetch('/api/flow/'+id)).json();
  const rct=(d.resp_headers&&(d.resp_headers['content-type']||d.resp_headers['Content-Type']))||'';
  const qct=(d.req_headers&&(d.req_headers['content-type']||d.req_headers['Content-Type']))||'';
  const qline=`${d.method} ${esc(d.path)}${d.query?'?'+esc(d.query):''} HTTP/1.1`;
  detail.className='';
  detail.innerHTML=`
    <div id="meta">flow <b>#${d.id}</b> · ${esc(d.ts)} · ${d.duration_ms??'?'}ms ·
      program=<b>${esc(d.program)}</b> · session=${esc(d.session_id)} · ${esc(d.source_tool)}<br>
      tech: ${(d.tech||[]).map(t=>`<span class="chip">${t}</span>`).join('')||'-'}<br>
      <span class="ho">${esc(d.url)}</span></div>
    <h3>REQUEST</h3><pre class="reqline">${qline}</pre>${headers(d.req_headers)}${bodyHtml(d.req_body,qct)}
    <h3>RESPONSE</h3><pre class="statusline">HTTP/1.1 ${d.status}</pre>${headers(d.resp_headers)}${bodyHtml(d.resp_body,rct)}`;
}
async function run(reset){
  const r=await fetch('/api/search?q='+encodeURIComponent(q.value)+'&limit='+(isLive()?LIVE_LIMIT:SEARCH_LIMIT));
  results=await r.json();
  if(reset){ sel=0; curId=null; }
  render();
}
function poll(){                                  // single self-rescheduling live-tail loop
  clearTimeout(polling);
  polling=setTimeout(async()=>{ if(isLive() && !document.hidden) await run(false); poll(); }, POLL_MS);
}
q.addEventListener('input',()=>{ clearTimeout(timer); timer=setTimeout(()=>run(true),120); });
list.addEventListener('click',e=>{ const row=e.target.closest('.row'); if(row) select(+row.dataset.i,true); });
document.addEventListener('keydown',e=>{
  if(document.activeElement===q && !['ArrowDown','ArrowUp'].includes(e.key)) return;
  if(e.key==='ArrowDown'){e.preventDefault(); select(sel+1,true);}
  if(e.key==='ArrowUp'){e.preventDefault(); select(sel-1,true);}
});
run(true); poll();
</script></body></html>"""
