#!/usr/bin/env python3
"""
observer — a read-only 50/50 inspector that attaches to a *running* Claude Code
session by tailing its on-disk transcript.

  python3 observer.py              # attach to the most recently active session,
                                   # and keep following whichever session is newest
  python3 observer.py -u <uuid>    # pin to a specific session id (no auto-switch)
  python3 observer.py --list       # list recent sessions and exit
  python3 observer.py -p 9000      # change port

Left pane:  user prompts, the agent's text + thinking, and tool *calls*.
Right pane: the *output* of those calls, wired to the call that produced it.

It NEVER spawns claude, NEVER writes to ~/.claude, and opens transcripts
read-only — so it has zero effect on the observed session, including its
permission prompts. It only reads the append-only JSONL that Claude Code
already writes at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
(override the root with CLAUDE_CONFIG_DIR). Stdlib only.
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 4.0


def projects_root() -> Path:
    root = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".claude"
    return base / "projects"


def all_transcripts() -> list[Path]:
    root = projects_root()
    if not root.is_dir():
        return []
    # recursive glob is robust to whether files sit directly under the project
    # dir or under a sessions/ subdir
    return [p for p in root.rglob("*.jsonl") if p.is_file()]


def find_by_uuid(uuid: str) -> Path | None:
    for p in all_transcripts():
        if p.stem == uuid:
            return p
    return None


def newest_transcript() -> Path | None:
    files = all_transcripts()
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def session_meta(path: Path) -> dict:
    """Cheaply derive a label + cwd by scanning the first lines only."""
    cwd = None
    label = None
    summary = None
    try:
        with path.open("r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 60:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                if summary is None and o.get("type") == "summary" and o.get("summary"):
                    summary = str(o["summary"])
                if label is None and o.get("type") == "user":
                    c = (o.get("message") or {}).get("content")
                    if isinstance(c, str):
                        t = c.strip()
                        if t and not t.startswith("<"):
                            label = t.splitlines()[0][:90]
    except OSError:
        pass
    st = path.stat()
    return {
        "uuid": path.stem,
        "cwd": cwd or "",
        "label": summary or label or path.stem[:8],
        "mtime": st.st_mtime,
        "size": st.st_size,
        "active": (time.time() - st.st_mtime) < 90,
    }


def recent_sessions(limit: int = 40) -> list[dict]:
    files = sorted(all_transcripts(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [session_meta(p) for p in files[:limit]]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    pinned_uuid: str | None = None  # set from CLI

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/sessions":
            self._json({
                "sessions": recent_sessions(),
                "pinned": self.pinned_uuid,
                "root": str(projects_root()),
            })
        elif u.path == "/stream":
            q = parse_qs(u.query)
            uuid = (q.get("uuid", [None])[0]) or self.pinned_uuid
            follow_newest = (q.get("follow", ["0"])[0] == "1") and not uuid
            self.stream(uuid, follow_newest)
        else:
            self.send_error(404)

    # --- the live tail --------------------------------------------------
    def stream(self, uuid: str | None, follow_newest: bool):
        # resolve initial file
        if uuid:
            path = find_by_uuid(uuid)
        else:
            path = newest_transcript()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj) -> bool:
            try:
                self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        if path is None:
            emit({"type": "__none__", "root": str(projects_root())})
            return

        offset = 0
        buf = ""
        last_beat = time.time()
        meta = session_meta(path)
        if not emit({"type": "__attach__", **meta, "path": str(path), "follow": follow_newest}):
            return

        while True:
            # follow-newest: hop to a newer session if one appears
            if follow_newest:
                nf = newest_transcript()
                if nf is not None and nf != path:
                    path = nf
                    offset = 0
                    buf = ""
                    meta = session_meta(path)
                    if not emit({"type": "__switch__", **meta, "path": str(path)}):
                        return

            try:
                size = path.stat().st_size
            except OSError:
                if not emit({"type": "__gone__"}):
                    return
                time.sleep(POLL_SECONDS)
                continue

            if size < offset:  # truncated / rotated
                offset = 0
                buf = ""

            if size > offset:
                try:
                    with path.open("r", errors="replace") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                except OSError:
                    chunk = ""
                buf += chunk
                while True:
                    nl = buf.find("\n")
                    if nl < 0:
                        break  # keep the incomplete trailing line for next poll
                    line = buf[:nl].strip()
                    buf = buf[nl + 1:]
                    if line:
                        if not emit({"type": "__line__", "raw": line}):
                            return
                last_beat = time.time()
            else:
                if time.time() - last_beat > HEARTBEAT_SECONDS:
                    if not emit({"type": "__hb__"}):
                        return
                    last_beat = time.time()

            time.sleep(POLL_SECONDS)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>observer</title>
<style>
  :root{
    --bg:#0d0f13; --panel:#14171d; --panel-2:#181c23; --border:#242a33;
    --text:#cdd3da; --dim:#6b7480;
    --user:#a892d6; --think:#d9a05b; --call:#54c7d4; --result:#7ec98f;
    --error:#e06c75; --wire:#f0c674;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;line-height:1.5;display:flex;flex-direction:column;}

  header{display:flex;align-items:center;gap:14px;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--panel-2);flex-wrap:wrap;}
  .brand{font-family:var(--mono);font-weight:600;letter-spacing:.5px;color:var(--call);}
  .brand .eye{color:var(--result);}
  select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:6px 9px;font-family:var(--mono);font-size:11px;max-width:340px;}
  select:focus{outline:none;border-color:var(--call);}
  .chk{display:flex;align-items:center;gap:5px;font-family:var(--mono);font-size:11px;color:var(--dim);cursor:pointer;user-select:none;}
  .meta{font-family:var(--mono);font-size:11px;color:var(--dim);display:flex;gap:16px;flex:1;min-width:0;justify-content:flex-end;}
  .meta span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .ro{font-family:var(--mono);font-size:10px;color:var(--result);border:1px solid rgba(126,201,143,.4);border-radius:5px;padding:2px 7px;}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;margin-right:6px;}
  .dot.live{background:var(--result);box-shadow:0 0 8px var(--result);animation:pulse 1.6s ease-in-out infinite;}
  .dot.idle{background:var(--dim);}
  .dot.err{background:var(--error);}
  @keyframes pulse{50%{opacity:.4;}}

  .split{flex:1;display:flex;min-height:0;}
  .pane{display:flex;flex-direction:column;min-width:0;flex:1 1 50%;}
  .pane-head{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:1.5px;color:var(--dim);padding:7px 14px;border-bottom:1px solid var(--border);background:var(--panel);}
  .pane-head .tick{color:var(--think);}
  #right .pane-head .tick{color:var(--result);}
  .scroll{overflow-y:auto;padding:14px;flex:1;scroll-behavior:smooth;}
  .scroll::-webkit-scrollbar{width:9px;}
  .scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px;}

  .divider{width:1px;background:var(--border);cursor:col-resize;position:relative;}
  .divider::after{content:"";position:absolute;inset:0 -4px;}
  .divider:hover,.divider.drag{background:var(--wire);box-shadow:0 0 8px var(--wire);}

  .block{border:1px solid var(--border);border-radius:8px;margin-bottom:11px;background:var(--panel);overflow:hidden;}
  .block .bh{display:flex;align-items:center;gap:8px;padding:7px 11px;font-family:var(--mono);font-size:11px;border-bottom:1px solid var(--border);}
  .block .bh .name{font-weight:600;}
  .block .bh .tag{margin-left:auto;font-size:10px;color:var(--dim);}
  .block .body{padding:10px 12px;}
  .block pre{margin:0;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;word-break:break-word;color:var(--text);}
  .text-block{padding:2px 4px 12px;white-space:pre-wrap;}

  .prompt{border-color:rgba(168,146,214,.4);}
  .prompt .bh{color:var(--user);}
  .prompt .body{color:#d8cdf0;white-space:pre-wrap;}

  .think{border-color:rgba(217,160,91,.32);}
  .think .bh{color:var(--think);cursor:pointer;}
  .think .body{color:#c9b79a;}
  .think.collapsed .body{display:none;}

  .call{border-color:rgba(84,199,212,.32);}
  .call .bh{color:var(--call);cursor:pointer;}
  .call .body pre{color:#b9e6ec;}

  .result{border-color:rgba(126,201,143,.30);}
  .result .bh{color:var(--result);cursor:pointer;}
  .result.is-error{border-color:rgba(224,108,117,.45);}
  .result.is-error .bh{color:var(--error);}
  .result.pending{opacity:.5;}
  .result.pending .bh::after{content:"running…";margin-left:auto;font-size:10px;color:var(--dim);}

  .sidechain::before{content:"subagent";font-family:var(--mono);font-size:9px;color:var(--dim);border:1px solid var(--border);border-radius:4px;padding:1px 5px;margin-right:6px;}
  .id-chip{font-family:var(--mono);font-size:9.5px;color:var(--dim);}
  .linked{outline:1px solid var(--wire);box-shadow:0 0 0 1px var(--wire),0 0 16px rgba(240,198,116,.25);}

  .empty{color:var(--dim);font-family:var(--mono);font-size:11px;padding:6px 4px;}
  .toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--panel-2);border:1px solid var(--wire);color:var(--text);font-family:var(--mono);font-size:11px;padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:10;}
  .toast.show{opacity:1;}
</style>
</head>
<body>
  <header>
    <div class="brand"><span class="eye">◉</span> observer</div>
    <select id="picker" title="session"></select>
    <label class="chk"><input type="checkbox" id="followNewest" checked /> follow newest</label>
    <span class="ro" title="read-only tail — cannot affect the observed session">READ-ONLY</span>
    <div class="meta">
      <span><span id="status" class="dot idle"></span><span id="statusText">connecting…</span></span>
      <span id="cwdMeta"></span>
      <span id="counts"></span>
    </div>
  </header>

  <div class="split" id="split">
    <section class="pane" id="left">
      <div class="pane-head"><span class="tick">▍</span> prompts · reasoning · calls</div>
      <div class="scroll" id="leftScroll"><div class="empty" id="leftEmpty">Waiting for the session…</div></div>
    </section>
    <div class="divider" id="divider"></div>
    <section class="pane" id="right">
      <div class="pane-head"><span class="tick">▍</span> tool output</div>
      <div class="scroll" id="rightScroll"><div class="empty" id="rightEmpty">Tool results land here, wired to the call that made them.</div></div>
    </section>
  </div>
  <div class="toast" id="toast"></div>

<script>
const $=(s)=>document.querySelector(s);
const leftScroll=$("#leftScroll"),rightScroll=$("#rightScroll");
let seen=new Set(), msgCount=0, stickL=true, stickR=true;
let ctrl=null;  // AbortController for the active stream

function nearBottom(el){return el.scrollHeight-el.scrollTop-el.clientHeight<60;}
leftScroll.addEventListener("scroll",()=>stickL=nearBottom(leftScroll));
rightScroll.addEventListener("scroll",()=>stickR=nearBottom(rightScroll));
function autoscroll(){if(stickL)leftScroll.scrollTop=leftScroll.scrollHeight;if(stickR)rightScroll.scrollTop=rightScroll.scrollHeight;}
function clearEmpties(){$("#leftEmpty")?.remove();$("#rightEmpty")?.remove();}
function setStatus(kind,text){$("#status").className="dot "+kind;$("#statusText").textContent=text;}
function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("show");clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("show"),2200);}
function cssEsc(s){return (s||"").replace(/["\\]/g,"\\$&");}
function pretty(v){if(v==null)return "";if(typeof v==="string")return v;try{return JSON.stringify(v,null,2);}catch{return String(v);}}

function resetPanes(){
  leftScroll.innerHTML='<div class="empty" id="leftEmpty">Waiting for the session…</div>';
  rightScroll.innerHTML='<div class="empty" id="rightEmpty">Tool results land here, wired to the call that made them.</div>';
  seen=new Set(); msgCount=0; updateCounts();
}
function updateCounts(){$("#counts").textContent=msgCount?msgCount+" msgs":"";}

// ---- renderers (left) --------------------------------------------------
function addPrompt(text,sc){
  if(!text||!text.trim())return; clearEmpties();
  const el=document.createElement("div"); el.className="block prompt"+(sc?" sidechain":"");
  el.innerHTML=`<div class="bh"><span class="name">user</span></div><div class="body"></div>`;
  el.querySelector(".body").textContent=text;
  leftScroll.appendChild(el);
}
function addText(text,sc){
  if(!text)return; clearEmpties();
  const el=document.createElement("div"); el.className="text-block"+(sc?" sidechain":"");
  el.textContent=text; leftScroll.appendChild(el);
}
function addThinking(text,sc){
  if(!text)return; clearEmpties();
  const el=document.createElement("div"); el.className="block think"+(sc?" sidechain":"");
  el.innerHTML=`<div class="bh"><span class="name">thinking</span><span class="tag">▾</span></div><div class="body"><pre></pre></div>`;
  el.querySelector("pre").textContent=text;
  el.querySelector(".bh").onclick=()=>el.classList.toggle("collapsed");
  leftScroll.appendChild(el);
}
function addToolCall(block,sc){
  clearEmpties();
  const id=block.id||("call_"+Math.random().toString(36).slice(2));
  const el=document.createElement("div"); el.className="block call"+(sc?" sidechain":""); el.dataset.id=id;
  el.innerHTML=`<div class="bh"><span class="name"></span><span class="id-chip"></span><span class="tag">call</span></div><div class="body"><pre></pre></div>`;
  el.querySelector(".name").textContent=block.name||"tool";
  el.querySelector(".id-chip").textContent=id.slice(0,10);
  el.querySelector("pre").textContent=pretty(block.input);
  wireHover(el,id); el.querySelector(".bh").onclick=()=>focusCounterpart(id,"right");
  leftScroll.appendChild(el);
  // pending result placeholder on the right
  const r=document.createElement("div"); r.className="block result pending"; r.dataset.id=id;
  r.innerHTML=`<div class="bh"><span class="name"></span><span class="id-chip"></span></div><div class="body"><pre></pre></div>`;
  r.querySelector(".name").textContent=block.name||"tool";
  r.querySelector(".id-chip").textContent=id.slice(0,10);
  wireHover(r,id); r.querySelector(".bh").onclick=()=>focusCounterpart(id,"left");
  rightScroll.appendChild(r);
}
// ---- renderers (right) -------------------------------------------------
function fillResult(toolUseId,content,isError){
  clearEmpties();
  let r=rightScroll.querySelector(`.result[data-id="${cssEsc(toolUseId)}"]`);
  if(!r){
    r=document.createElement("div"); r.className="block result"; r.dataset.id=toolUseId;
    r.innerHTML=`<div class="bh"><span class="name">result</span><span class="id-chip">${(toolUseId||"").slice(0,10)}</span></div><div class="body"><pre></pre></div>`;
    wireHover(r,toolUseId); rightScroll.appendChild(r);
  }
  r.classList.remove("pending");
  if(isError)r.classList.add("is-error");
  r.querySelector("pre").textContent=stringifyResult(content);
}
function stringifyResult(content){
  if(typeof content==="string")return content;
  if(Array.isArray(content))return content.map(b=>{
    if(b&&b.type==="text")return b.text;
    if(b&&b.type==="image")return "[image]";
    return pretty(b);
  }).join("\n");
  return pretty(content);
}
function wireHover(el,id){
  el.addEventListener("mouseenter",()=>highlight(id,true));
  el.addEventListener("mouseleave",()=>highlight(id,false));
}
function highlight(id,on){document.querySelectorAll(`[data-id="${cssEsc(id)}"]`).forEach(e=>e.classList.toggle("linked",on));}
function focusCounterpart(id,side){
  const t=(side==="right"?rightScroll:leftScroll).querySelector(`[data-id="${cssEsc(id)}"]`);
  if(t){t.scrollIntoView({behavior:"smooth",block:"center"});highlight(id,true);setTimeout(()=>highlight(id,false),1200);}
}

// ---- transcript line routing ------------------------------------------
function isRealPrompt(s){return s && s.trim() && !s.trim().startsWith("<");}
function handleLine(o){
  if(o.uuid){ if(seen.has(o.uuid))return; seen.add(o.uuid); }
  const sc=!!o.isSidechain;
  if(o.type==="user"){
    const c=(o.message||{}).content;
    if(typeof c==="string"){ if(isRealPrompt(c)) addPrompt(c,sc); }
    else if(Array.isArray(c)){
      for(const b of c){
        if(b.type==="tool_result") fillResult(b.tool_use_id,b.content,b.is_error);
        else if(b.type==="text" && isRealPrompt(b.text)) addPrompt(b.text,sc);
      }
    }
    msgCount++;
  } else if(o.type==="assistant"){
    for(const b of ((o.message||{}).content||[])){
      if(b.type==="text") addText(b.text,sc);
      else if(b.type==="thinking") addThinking(b.thinking,sc);
      else if(b.type==="tool_use") addToolCall(b,sc);
    }
    msgCount++;
  } else if(o.type==="summary" && o.summary){
    setStatus("live",o.summary.slice(0,60));
  }
  updateCounts(); autoscroll();
}

// ---- control events from the server -----------------------------------
function handleCtrl(o){
  switch(o.type){
    case "__attach__":
      setStatus(o.active?"live":"idle", (o.active?"live · ":"idle · ")+(o.label||o.uuid.slice(0,8)));
      $("#cwdMeta").textContent=o.cwd||""; selectInPicker(o.uuid); break;
    case "__switch__":
      resetPanes(); toast("switched to newer session "+o.uuid.slice(0,8));
      setStatus("live","live · "+(o.label||o.uuid.slice(0,8)));
      $("#cwdMeta").textContent=o.cwd||""; selectInPicker(o.uuid); break;
    case "__line__":
      try{ handleLine(JSON.parse(o.raw)); }catch(e){}
      setStatus("live", $("#statusText").textContent.replace(/^idle/,"live")); break;
    case "__hb__": break;
    case "__none__":
      setStatus("err","no sessions found under "+o.root); break;
    case "__gone__":
      setStatus("idle","transcript unavailable"); break;
  }
}

// ---- stream connection -------------------------------------------------
async function connect(uuid){
  if(ctrl) ctrl.abort();
  ctrl=new AbortController();
  resetPanes();
  const follow=$("#followNewest").checked && !uuid ? "1":"0";
  const qs=new URLSearchParams(); if(uuid)qs.set("uuid",uuid); qs.set("follow",follow);
  setStatus("idle","connecting…");
  let buf="";
  try{
    const resp=await fetch("/stream?"+qs.toString(),{signal:ctrl.signal});
    const reader=resp.body.getReader(); const dec=new TextDecoder();
    while(true){
      const {value,done}=await reader.read(); if(done)break;
      buf+=dec.decode(value,{stream:true});
      let nl; while((nl=buf.indexOf("\n"))>=0){
        const line=buf.slice(0,nl).trim(); buf=buf.slice(nl+1);
        if(line){ try{ handleCtrl(JSON.parse(line)); }catch(e){} }
      }
    }
  }catch(e){
    if(e.name!=="AbortError") setStatus("err","disconnected — "+e.message);
  }
}

// ---- session picker ----------------------------------------------------
async function loadSessions(){
  try{
    const r=await fetch("/sessions"); const data=await r.json();
    const pk=$("#picker"); const cur=pk.value;
    pk.innerHTML="";
    const auto=document.createElement("option"); auto.value=""; auto.textContent="◉ newest active session"; pk.appendChild(auto);
    for(const s of data.sessions){
      const o=document.createElement("option"); o.value=s.uuid;
      const when=new Date(s.mtime*1000).toLocaleTimeString();
      const cwd=s.cwd?s.cwd.split("/").slice(-1)[0]:"";
      o.textContent=`${s.active?"● ":""}${cwd?cwd+" — ":""}${s.label} · ${when}`;
      pk.appendChild(o);
    }
    if(data.pinned){ pk.value=data.pinned; }
    else if(cur) pk.value=cur;
  }catch(e){}
}
function selectInPicker(uuid){
  const pk=$("#picker");
  if($("#followNewest").checked){ pk.value=""; }
  else { for(const o of pk.options){ if(o.value===uuid){ pk.value=uuid; break; } } }
}

$("#picker").addEventListener("change",e=>{
  const v=e.target.value;
  $("#followNewest").checked = (v==="");
  connect(v||null);
});
$("#followNewest").addEventListener("change",e=>{
  if(e.target.checked){ $("#picker").value=""; connect(null); }
});

// ---- draggable divider -------------------------------------------------
(function(){
  const div=$("#divider"),split=$("#split"),left=$("#left"); let dragging=false;
  div.addEventListener("mousedown",()=>{dragging=true;div.classList.add("drag");document.body.style.userSelect="none";});
  window.addEventListener("mouseup",()=>{dragging=false;div.classList.remove("drag");document.body.style.userSelect="";});
  window.addEventListener("mousemove",e=>{
    if(!dragging)return; const rect=split.getBoundingClientRect();
    let pct=(e.clientX-rect.left)/rect.width*100; pct=Math.max(20,Math.min(80,pct));
    left.style.flex=`0 0 ${pct}%`;
  });
})();

// boot
loadSessions(); setInterval(loadSessions,5000);
connect(null);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Read-only live observer for Claude Code sessions.")
    ap.add_argument("-u", "--uuid", help="pin to a specific session id (disables auto-switch)")
    ap.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--list", action="store_true", help="list recent sessions and exit")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    if args.list:
        sessions = recent_sessions()
        if not sessions:
            print(f"No sessions found under {projects_root()}")
            return
        print(f"Recent sessions under {projects_root()}:\n")
        for s in sessions:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
            flag = "●" if s["active"] else " "
            print(f"  {flag} {s['uuid']}  {when}  {s['cwd']}")
            print(f"      {s['label']}")
        return

    if args.uuid and find_by_uuid(args.uuid) is None:
        print(f"Session '{args.uuid}' not found under {projects_root()}.")
        print("Run with --list to see available sessions.")
        sys.exit(1)

    Handler.pinned_uuid = args.uuid
    server = ThreadingServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    target = args.uuid or "most recent active session"
    print(f"observer (read-only) serving at {url}")
    print(f"attaching to: {target}")
    print(f"transcripts:  {projects_root()}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
