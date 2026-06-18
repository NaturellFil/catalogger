"""Normalized flow model -- the common shape every capture source produces."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit


@dataclass
class Flow:
    ts: datetime
    method: str
    scheme: str
    host: str
    path: str
    url: str
    status: Optional[int] = None
    port: Optional[int] = None
    query: Optional[str] = None
    req_headers: dict = field(default_factory=dict)
    resp_headers: dict = field(default_factory=dict)
    req_body: Optional[bytes] = None
    resp_body: Optional[bytes] = None
    duration_ms: Optional[int] = None
    program: Optional[str] = None
    source_tool: Optional[str] = None
    session_id: Optional[str] = None
    collapse: bool = False
    req_content_type: str = ""
    resp_content_type: str = ""

    # -- capture-source adapters ------------------------------------------------

    @classmethod
    def from_mitm(cls, flow, *, program=None, session_id=None,
                  source_tool="mitm", collapse=False) -> "Flow":
        req = flow.request
        resp = getattr(flow, "response", None)
        u = urlsplit(req.url)
        dur = None
        if resp is not None and resp.timestamp_end and req.timestamp_start:
            dur = int((resp.timestamp_end - req.timestamp_start) * 1000)
        return cls(
            ts=datetime.fromtimestamp(req.timestamp_start or 0, tz=timezone.utc),
            method=req.method,
            scheme=req.scheme,
            host=req.host,
            port=req.port,
            path=u.path or "/",
            query=u.query or None,
            url=req.url,
            status=resp.status_code if resp else None,
            req_headers=dict(req.headers),
            resp_headers=dict(resp.headers) if resp else {},
            req_body=_mitm_content(req),
            resp_body=_mitm_content(resp),
            duration_ms=dur,
            program=program,
            session_id=session_id,
            source_tool=source_tool,
            collapse=collapse,
            req_content_type=req.headers.get("content-type", ""),
            resp_content_type=(resp.headers.get("content-type", "") if resp else ""),
        )

    @classmethod
    def from_proxify(cls, obj: dict, *, program=None, session_id=None,
                     source_tool="proxify", collapse=False) -> "Flow":
        # proxify v0.0.16 schema: top-level `url`; method/path/scheme/host are
        # pseudo-keys *inside* request.header; status lives only in the first
        # line of response.raw.
        #
        # IMPORTANT: proxify does NOT emit response bodies in its jsonl (headers
        # only), so flows captured this way carry metadata but no body.
        # catalogger's headline feature -- storing full response bodies for
        # retroactive fingerprinting -- needs the *mitmproxy* source. Prefer
        # mitm_addon.py for general capture; proxify suits a metadata-only
        # firehose where body storage isn't required.
        req = obj.get("request", {}) or {}
        resp = obj.get("response", {}) or {}
        rhdr = dict(req.get("header") or req.get("headers") or {})
        # split proxify's pseudo-keys out of the real request headers
        pseudo = {k: rhdr.pop(k, None) for k in ("method", "path", "scheme", "host")}
        url = obj.get("url") or req.get("url") or req.get("raw_url") or ""
        u = urlsplit(url)
        resp_hdr = dict(resp.get("header") or resp.get("headers") or {})
        return cls(
            ts=_parse_ts(obj.get("timestamp")),
            method=(pseudo.get("method") or "GET"),
            scheme=u.scheme or pseudo.get("scheme") or "https",
            host=u.hostname or (pseudo.get("host") or "").split(":")[0],
            port=u.port,
            path=u.path or pseudo.get("path") or "/",
            query=u.query or None,
            url=url,
            status=_status_from_raw(resp.get("raw")),
            req_headers=rhdr,
            resp_headers=resp_hdr,
            req_body=_body_from_raw(req.get("raw")),
            resp_body=_body_from_raw(resp.get("raw")),
            program=program,
            session_id=session_id,
            source_tool=source_tool,
            collapse=collapse,
            resp_content_type=resp_hdr.get("Content-Type") or resp_hdr.get("content-type", ""),
        )

    # -- replay -----------------------------------------------------------------

    def to_curl(self) -> str:
        parts = [f"curl -sk -X {self.method} {_q(self.url)}"]
        for k, v in self.req_headers.items():
            if k.lower() in ("content-length",):
                continue
            parts.append(f"-H {_q(f'{k}: {v}')}")
        if self.req_body:
            parts.append("--data-binary @-")  # body piped on stdin to stay safe
        return " ".join(parts)


# -- helpers --------------------------------------------------------------------

def _mitm_content(msg):
    """Decoded message body (gunzip/brotli/deflate applied), best-effort.

    mitmproxy's `raw_content` is the bytes as they came off the wire -- i.e.
    still gzip/br-compressed when the response was encoded. Storing that and
    then full-text indexing it as "text" produces garbage full of NUL bytes,
    which Postgres rejects. `get_content(strict=False)` returns the *decoded*
    body and never raises on a bad/unknown encoding (falls back to raw).
    """
    if msg is None:
        return None
    get = getattr(msg, "get_content", None)
    if callable(get):
        try:
            return get(strict=False)
        except Exception:
            pass
    return getattr(msg, "raw_content", None)


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_bytes(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return v
    return str(v).encode("utf-8", "replace")


def _as_header_dict(h):
    if isinstance(h, dict):
        return h
    out = {}
    if isinstance(h, list):
        for item in h:
            if isinstance(item, str) and ":" in item:
                k, _, val = item.partition(":")
                out[k.strip()] = val.strip()
    return out


def _ct(h):
    return _as_header_dict(h).get("content-type", "") or _as_header_dict(h).get("Content-Type", "")


def _parse_ts(v):
    if not v:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=timezone.utc)


def _status_from_raw(raw):
    """Pull the HTTP status code out of a raw response's first line."""
    if not raw:
        return None
    first = str(raw).split("\n", 1)[0]
    for tok in first.split():
        if tok.isdigit() and len(tok) == 3:
            return int(tok)
    return None


def _body_from_raw(raw):
    """Return the body bytes after the header/body separator in a raw message."""
    if not raw:
        return None
    s = str(raw)
    for sep in ("\r\n\r\n", "\n\n"):
        idx = s.find(sep)
        if idx >= 0:
            body = s[idx + len(sep):]
            return body.encode("utf-8", "replace") if body else None
    return None
