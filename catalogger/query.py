"""Query layer -- interrogate the archive.

The headline query: "I found a bypass on tech X -- show me every flow that runs X"
    catalogger query --tech akamai
Combine with full-text over deduplicated bodies, host/status filters, and
--curl to emit a replayable request for each hit.
"""
from __future__ import annotations

from typing import List, Optional

import zstandard as zstd

from .models import Flow

_dctx = zstd.ZstdDecompressor()


def search(conn, *, tech: List[str] = None, host: str = None, status: int = None,
           method: str = None, grep: str = None, program: str = None,
           session: str = None, since: str = None, limit: int = 50):
    where, params = [], []
    if tech:
        where.append("f.fingerprints @> %s")
        params.append(list(tech))
    if host:
        where.append("f.host ILIKE %s")
        params.append(f"%{host}%")
    if status is not None:
        where.append("f.status = %s")
        params.append(status)
    if method:
        where.append("f.method = %s")
        params.append(method.upper())
    if program:
        where.append("f.program = %s")
        params.append(program)
    if session:
        where.append("f.session_id = %s")
        params.append(session)
    if since:
        where.append("f.ts >= %s")
        params.append(since)
    if grep:
        # full-text over UNIQUE text bodies -- fast and small thanks to dedup
        where.append("""f.resp_body_sha IN (
            SELECT sha256 FROM body_text WHERE tsv @@ plainto_tsquery('simple', %s))""")
        params.append(grep)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT f.id, f.ts, f.method, f.status, f.url, f.host,
               f.fingerprints, f.session_id, f.program
        FROM flows f
        {clause}
        ORDER BY f.ts DESC
        LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def load_flow(conn, flow_id: int) -> Optional[Flow]:
    """Rehydrate a stored flow into a Flow object (for replay / curl export)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.ts, f.method, f.scheme, f.host, f.port, f.path, f.query,
                   f.url, f.status, f.req_headers, rb.content
            FROM flows f
            LEFT JOIN bodies rb ON rb.sha256 = f.req_body_sha
            WHERE f.id = %s
        """, (flow_id,))
        row = cur.fetchone()
    if not row:
        return None
    ts, method, scheme, host, port, path, query, url, status, req_headers, rbody = row
    body = None
    if rbody is not None:
        raw = rbody if isinstance(rbody, (bytes, bytearray)) else bytes(rbody)
        try:
            body = _dctx.decompress(raw)
        except zstd.ZstdError:
            body = None
    return Flow(ts=ts, method=method, scheme=scheme, host=host, port=port,
                path=path, query=query, url=url, status=status,
                req_headers=req_headers or {}, req_body=body)


def stats(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM flows")
        out["flows"] = cur.fetchone()[0]
        cur.execute("SELECT count(*), coalesce(sum(seen_count),0), coalesce(sum(size),0) FROM bodies")
        unique_bodies, total_seen, raw_bytes = cur.fetchone()
        out["unique_bodies"] = unique_bodies
        out["body_occurrences"] = total_seen
        out["dedup_ratio"] = round(total_seen / unique_bodies, 1) if unique_bodies else 0
        out["raw_body_bytes_if_undeduped"] = raw_bytes  # sum of unique sizes only
        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('bodies'))")
        out["bodies_on_disk"] = cur.fetchone()[0]
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        out["db_total"] = cur.fetchone()[0]
        cur.execute("""
            SELECT unnest(fingerprints) AS t, count(*) c
            FROM flows GROUP BY t ORDER BY c DESC LIMIT 15
        """)
        out["top_tech"] = cur.fetchall()
    return out


def load_full(conn, flow_id: int) -> Optional[dict]:
    """Load one flow with BOTH bodies decompressed -- for the detail view."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.ts, f.program, f.source_tool, f.session_id, f.method,
                   f.scheme, f.host, f.port, f.path, f.query, f.url, f.status,
                   f.duration_ms, f.fingerprints, f.req_headers, f.resp_headers,
                   qb.content, qb.is_text, rb.content, rb.is_text
            FROM flows f
            LEFT JOIN bodies qb ON qb.sha256 = f.req_body_sha
            LEFT JOIN bodies rb ON rb.sha256 = f.resp_body_sha
            WHERE f.id = %s
        """, (flow_id,))
        row = cur.fetchone()
    if not row:
        return None
    keys = ("id", "ts", "program", "source_tool", "session_id", "method",
            "scheme", "host", "port", "path", "query", "url", "status",
            "duration_ms", "fingerprints", "req_headers", "resp_headers",
            "_qc", "req_is_text", "_rc", "resp_is_text")
    d = dict(zip(keys, row))
    d["req_body"] = _decompress(d.pop("_qc"))
    d["resp_body"] = _decompress(d.pop("_rc"))
    return d


def _decompress(content):
    if content is None:
        return None
    raw = content if isinstance(content, (bytes, bytearray)) else bytes(content)
    try:
        return _dctx.decompress(raw)
    except zstd.ZstdError:
        return raw


def render_flow(d: dict, body_limit: int = 4000) -> str:
    """Render a flow as a Caido-style raw request / response pair."""
    import json
    from http.client import responses as _reason

    def fmt_headers(h):
        return "\n".join(f"{k}: {v}" for k, v in (h or {}).items())

    def fmt_body(body, is_text):
        if body is None or body == b"":
            return "(no body)"
        if not is_text:
            return f"<binary, {len(body)} bytes>"
        text = body.decode("utf-8", "replace")
        s = text.lstrip()
        if s[:1] in "{[":
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        if len(text) > body_limit:
            text = text[:body_limit] + f"\n... [truncated, {len(body)} bytes total]"
        return text

    q = f"?{d['query']}" if d.get("query") else ""
    tags = ", ".join(d["fingerprints"]) if d["fingerprints"] else "-"
    bar = "\u2500" * 64

    meta = (f"flow #{d['id']}  {d['ts']:%Y-%m-%d %H:%M:%S}  "
            f"{d['method']} \u2192 {d['status']}  ({d.get('duration_ms')}ms)\n"
            f"program={d['program']}  session={d['session_id']}  "
            f"tool={d['source_tool']}\n"
            f"tech: [{tags}]\n{d['url']}")

    req = (f"\n{bar}\nREQUEST\n{bar}\n"
           f"{d['method']} {d['path']}{q} HTTP/1.1\n"
           f"{fmt_headers(d['req_headers'])}\n\n"
           f"{fmt_body(d['req_body'], d['req_is_text'])}")

    reason = _reason.get(d["status"] or 0, "")
    resp = (f"\n{bar}\nRESPONSE\n{bar}\n"
            f"HTTP/1.1 {d['status']} {reason}\n"
            f"{fmt_headers(d['resp_headers'])}\n\n"
            f"{fmt_body(d['resp_body'], d['resp_is_text'])}")

    return meta + "\n" + req + "\n" + resp
