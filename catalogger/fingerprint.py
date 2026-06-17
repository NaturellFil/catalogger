"""Fingerprint engine -- the retroactive-requery feature.

Rules tag each flow with technologies. The whole point: when you discover a
bypass on some tech you never tagged, you add ONE rule and re-run this over the
entire stored corpus to surface every host you ever hit that runs it.

Add a rule = append to RULES. Re-run = `catalogger fingerprint --all`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List

import zstandard as zstd

_dctx = zstd.ZstdDecompressor()


@dataclass
class Ctx:
    status: int
    headers: dict          # lowercased keys -> value
    cookies: str           # raw set-cookie blob, lowercased
    server: str            # lowercased server header
    body: str              # decoded text body (may be empty for binary)


@dataclass
class Rule:
    name: str
    match: Callable[[Ctx], bool]


def _h(ctx: Ctx, key: str) -> str:
    return ctx.headers.get(key.lower(), "").lower()


# -- starter rule set (extend freely) ------------------------------------------
RULES: List[Rule] = [
    Rule("f5-big-ip",      lambda c: "bigipserver" in c.cookies or "x-cnection" in c.headers),
    Rule("akamai",         lambda c: "akamaighost" in c.server or "x-akamai" in " ".join(c.headers)),
    Rule("cloudfront",     lambda c: "cloudfront" in _h(c, "via") or "x-amz-cf-id" in c.headers),
    Rule("cloudflare",     lambda c: "cloudflare" in c.server or "cf-ray" in c.headers),
    Rule("apigee",         lambda c: "apigee" in " ".join(c.headers.values()).lower()
                                     or '"fault"' in c.body[:2000]),
    Rule("aem-dispatcher", lambda c: "dispatcher" in _h(c, "x-vhost") or "cq" in _h(c, "x-cq")),
    Rule("nginx",          lambda c: "nginx" in c.server),
    Rule("apache",         lambda c: "apache" in c.server),
    Rule("iis-aspnet",     lambda c: "asp.net" in " ".join(c.headers.values()).lower()
                                     or "x-aspnet-version" in c.headers),
    Rule("graphql",        lambda c: '"errors"' in c.body[:500] and "graphql" in c.body[:2000].lower()),
    Rule("waf-generic",    lambda c: c.status in (403, 406, 419)
                                     and ("access denied" in c.body[:500].lower()
                                          or "request blocked" in c.body[:500].lower())),
]


def _decompress(content) -> bytes:
    if content is None:
        return b""
    raw = content if isinstance(content, (bytes, bytearray)) else bytes(content)
    try:
        return _dctx.decompress(raw)
    except zstd.ZstdError:
        return b""


def _build_ctx(status, headers, is_text, content) -> Ctx:
    h = {k.lower(): str(v) for k, v in (headers or {}).items()}
    body = _decompress(content).decode("utf-8", "replace") if is_text else ""
    return Ctx(
        status=status or 0,
        headers=h,
        cookies=h.get("set-cookie", "").lower(),
        server=h.get("server", "").lower(),
        body=body,
    )


def tag(ctx: Ctx) -> List[str]:
    return [r.name for r in RULES if _safe(r, ctx)]


def _safe(rule: Rule, ctx: Ctx) -> bool:
    try:
        return bool(rule.match(ctx))
    except Exception:
        return False


def fingerprint_all(conn, only_untagged: bool = False, batch: int = 500) -> int:
    """(Re)compute fingerprints for every flow. Re-runnable and idempotent."""
    where = "WHERE f.fingerprints = '{}'" if only_untagged else ""
    updated = 0
    with conn.cursor(name="fp_cursor") as cur:  # server-side cursor for big corpora
        cur.itersize = batch
        cur.execute(f"""
            SELECT f.id, f.status, f.resp_headers, b.is_text, b.content
            FROM flows f
            LEFT JOIN bodies b ON b.sha256 = f.resp_body_sha
            {where}
        """)
        pending = []
        with conn.cursor() as w:
            for fid, status, headers, is_text, content in cur:
                tags = tag(_build_ctx(status, headers, is_text, content))
                pending.append((tags, fid))
                if len(pending) >= batch:
                    _flush(w, pending); updated += len(pending); pending = []
            if pending:
                _flush(w, pending); updated += len(pending)
        conn.commit()
    return updated


def _flush(cur, rows):
    cur.executemany("UPDATE flows SET fingerprints = %s WHERE id = %s", rows)
