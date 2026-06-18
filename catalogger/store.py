"""The dedup core: content-addressed body storage + flow/agg writes.

store_body() is where deduplication happens:
  sha = sha256(body)
  INSERT INTO bodies ... ON CONFLICT (sha256) DO UPDATE seen_count += 1
The millionth identical 404 body is a counter bump, not a new blob.
"""
from __future__ import annotations

import hashlib
from typing import Optional

import zstandard as zstd
from psycopg.types.json import Jsonb

from .models import Flow

_TEXT_HINTS = ("text/", "json", "xml", "javascript", "html", "x-www-form-urlencoded")
_cctx = zstd.ZstdCompressor(level=10)


def _is_text(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(h in ct for h in _TEXT_HINTS)


def store_body(cur, body: Optional[bytes], content_type: str = "") -> Optional[str]:
    """Insert (or dedup) a body. Returns its sha256, or None for no body."""
    if body is None:
        return None
    sha = hashlib.sha256(body).hexdigest()
    is_text = _is_text(content_type)
    compressed = _cctx.compress(body)
    cur.execute(
        """
        INSERT INTO bodies (sha256, size, encoding, content, is_text, seen_count)
        VALUES (%s, %s, 'zstd', %s, %s, 1)
        ON CONFLICT (sha256) DO UPDATE SET seen_count = bodies.seen_count + 1
        """,
        (sha, len(body), compressed, is_text),
    )
    if is_text:
        text = body.decode("utf-8", "replace")[:1_000_000]
        cur.execute(
            """
            INSERT INTO body_text (sha256, tsv)
            VALUES (%s, to_tsvector('simple', %s))
            ON CONFLICT (sha256) DO NOTHING
            """,
            (sha, text),
        )
    return sha


def write_flow(cur, f: Flow, req_sha: Optional[str], resp_sha: Optional[str]) -> None:
    cur.execute(
        """
        INSERT INTO flows (ts, program, source_tool, session_id, method, scheme,
            host, port, path, query, url, status, req_headers, resp_headers,
            req_body_sha, resp_body_sha, duration_ms)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            f.ts, f.program, f.source_tool, f.session_id, f.method, f.scheme,
            f.host, f.port, f.path, f.query, f.url, f.status,
            Jsonb(f.req_headers), Jsonb(f.resp_headers),
            req_sha, resp_sha, f.duration_ms,
        ),
    )


def write_agg(cur, f: Flow, resp_sha: Optional[str]) -> None:
    """Collapsed write for high-volume fuzz: one row per distinct shape."""
    key = "|".join([f.host, f.method, f.path, str(f.status), resp_sha or ""])
    shape = hashlib.sha256(key.encode()).hexdigest()
    cur.execute(
        """
        INSERT INTO flow_agg (shape_sha, program, source_tool, method, host, path,
            status, resp_body_sha, hit_count, first_seen, last_seen)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)
        ON CONFLICT (shape_sha) DO UPDATE
            SET hit_count = flow_agg.hit_count + 1,
                last_seen = EXCLUDED.last_seen
        """,
        (shape, f.program, f.source_tool, f.method, f.host, f.path,
         f.status, resp_sha, f.ts, f.ts),
    )


def persist(cur, f: Flow) -> None:
    """Full persist path for one flow (called by the batch writer).

    The flow_agg rollup is maintained for *every* flow so the unique-shape
    view (one row per host+method+path+status+resp_body, with hit_count and
    first/last_seen) always reflects the whole corpus. `collapse` only governs
    whether we ALSO keep the individual flow row: a high-volume fuzz firehose
    sets collapse=True to bump the aggregate without storing every hit.
    """
    req_sha = store_body(cur, f.req_body, f.req_content_type)
    resp_sha = store_body(cur, f.resp_body, f.resp_content_type)
    write_agg(cur, f, resp_sha)
    if not f.collapse:
        write_flow(cur, f, req_sha, resp_sha)


def rebuild_agg(conn, batch: int = 1000) -> int:
    """Rebuild flow_agg from scratch by replaying every stored flow through
    write_agg. Backfill for corpora captured before the rollup was maintained,
    and re-runnable any time. Idempotent: TRUNCATE then re-aggregate in ts
    order so first_seen/last_seen and hit_count come out correct.
    """
    from types import SimpleNamespace

    with conn.cursor() as w:
        w.execute("TRUNCATE flow_agg")
    n = 0
    with conn.cursor(name="agg_cursor") as cur:  # server-side cursor for big corpora
        cur.itersize = batch
        cur.execute(
            """
            SELECT ts, program, source_tool, method, host, path, status, resp_body_sha
            FROM flows ORDER BY ts
            """
        )
        with conn.cursor() as w:
            for ts, program, source_tool, method, host, path, status, resp_sha in cur:
                f = SimpleNamespace(
                    ts=ts, program=program, source_tool=source_tool,
                    method=method, host=host, path=path, status=status,
                )
                write_agg(w, f, resp_sha)
                n += 1
        conn.commit()
    return n
