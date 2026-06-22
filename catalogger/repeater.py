"""Repeater -- turn the archive into a Burp/Caido-style resend tool.

catalogger already stores every request with full fidelity and can rehydrate one
(`query.load_flow`). The repeater closes the loop: load a stored flow, optionally
mutate it (method / url / headers / body), resend it, and persist the result as a
NEW flow linked to its parent via `replay_of`.

That linkage is what makes it an anti-hallucination substrate: a finding cites a
flow id that physically exists in the DB, and `repeat <id>` re-proves it live,
recording the fresh response as its own citable flow.

Auth note: a verbatim resend sends the stored headers as-is. Time-bound creds
(e.g. Google FPA SAPISIDHASH) will be stale -- callers that need fresh auth pass
it in via `set_headers` (this is the seam mayhem uses to inject a freshly
computed Authorization before resending).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from .models import Flow
from .query import load_flow
from .store import store_body, write_agg

# headers we must not forward verbatim: recomputed by the client or hop-by-hop.
_DROP_ON_RESEND = {"content-length", "transfer-encoding", "connection"}


@dataclass
class Mutations:
    method: Optional[str] = None
    url: Optional[str] = None
    set_headers: dict[str, str] = field(default_factory=dict)
    unset_headers: list[str] = field(default_factory=list)
    body: Optional[bytes] = None  # replaces request body when not None

    def apply(self, f: Flow) -> Flow:
        if self.method:
            f.method = self.method.upper()
        if self.url:
            from urllib.parse import urlsplit

            f.url = self.url
            u = urlsplit(self.url)
            f.scheme = u.scheme or f.scheme
            f.host = u.hostname or f.host
            f.port = u.port
            f.path = u.path or "/"
            f.query = u.query or None
        # header mutations are case-insensitive on the key
        if self.set_headers or self.unset_headers:
            lower_unset = {h.lower() for h in self.unset_headers}
            new_headers = {
                k: v for k, v in f.req_headers.items() if k.lower() not in lower_unset
            }
            for k, v in self.set_headers.items():
                # replace any existing case-variant of the same header
                for existing in [h for h in new_headers if h.lower() == k.lower()]:
                    del new_headers[existing]
                new_headers[k] = v
            f.req_headers = new_headers
        if self.body is not None:
            f.req_body = self.body
        return f


def ensure_schema(conn) -> None:
    """Add the replay_of lineage column if an older DB predates it. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE flows ADD COLUMN IF NOT EXISTS replay_of bigint "
            "REFERENCES flows(id)"
        )
    conn.commit()


def _send_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_ON_RESEND}


def resend(
    conn,
    f: Flow,
    *,
    replay_of: Optional[int] = None,
    program: Optional[str] = None,
    timeout: float = 30.0,
    verify: bool = False,
) -> int:
    """Send `f` over the wire and persist the result as a new flow. Returns id.

    The persisted flow records the request actually sent and the live response,
    tagged source_tool='repeater' and linked to `replay_of`.
    """
    headers = _send_headers(f.req_headers)
    with httpx.Client(http2=True, timeout=timeout, verify=verify, follow_redirects=False) as cli:
        resp = cli.request(f.method, f.url, headers=headers, content=f.req_body)

    result = Flow(
        ts=datetime.now(tz=timezone.utc),
        method=f.method,
        scheme=f.scheme,
        host=f.host,
        port=f.port,
        path=f.path,
        query=f.query,
        url=f.url,
        status=resp.status_code,
        req_headers=f.req_headers,
        resp_headers=dict(resp.headers),
        req_body=f.req_body,
        resp_body=resp.content,  # httpx auto-decompresses
        duration_ms=int(resp.elapsed.total_seconds() * 1000),
        program=program or f.program,
        source_tool="repeater",
        req_content_type=dict(f.req_headers).get("content-type", ""),
        resp_content_type=resp.headers.get("content-type", ""),
    )

    new_id = _persist_returning(conn, result, replay_of=replay_of)
    conn.commit()
    return new_id


def _persist_returning(conn, f: Flow, *, replay_of: Optional[int]) -> int:
    """Persist one flow and return its id (write_flow doesn't RETURN id)."""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        req_sha = store_body(cur, f.req_body, f.req_content_type)
        resp_sha = store_body(cur, f.resp_body, f.resp_content_type)
        write_agg(cur, f, resp_sha)
        cur.execute(
            """
            INSERT INTO flows (ts, program, source_tool, session_id, method, scheme,
                host, port, path, query, url, status, req_headers, resp_headers,
                req_body_sha, resp_body_sha, duration_ms, replay_of)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                f.ts, f.program, f.source_tool, f.session_id, f.method, f.scheme,
                f.host, f.port, f.path, f.query, f.url, f.status,
                Jsonb(f.req_headers), Jsonb(f.resp_headers),
                req_sha, resp_sha, f.duration_ms, replay_of,
            ),
        )
        return cur.fetchone()[0]


def repeat(
    conn,
    flow_id: int,
    *,
    mutations: Optional[Mutations] = None,
    program: Optional[str] = None,
    timeout: float = 30.0,
    verify: bool = False,
) -> int:
    """Load flow `flow_id`, apply mutations, resend, persist. Returns new id."""
    ensure_schema(conn)
    f = load_flow(conn, flow_id)
    if f is None:
        raise LookupError(f"no flow #{flow_id}")
    if mutations is not None:
        f = mutations.apply(f)
    return resend(conn, f, replay_of=flow_id, program=program, timeout=timeout, verify=verify)


def render_request(f: Flow) -> str:
    """Raw-ish request preview for --dump (no send)."""
    target = f.path + (f"?{f.query}" if f.query else "")
    lines = [f"{f.method} {target} HTTP/2", f"host: {f.host}"]
    for k, v in f.req_headers.items():
        if k.lower() == "host":
            continue
        lines.append(f"{k}: {v}")
    out = "\n".join(lines)
    if f.req_body:
        body = f.req_body
        try:
            out += "\n\n" + body.decode("utf-8")
        except UnicodeDecodeError:
            out += f"\n\n<{len(body)} bytes binary body>"
    return out
