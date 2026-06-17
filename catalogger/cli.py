"""catalogger CLI.

  catalogger initdb
  catalogger fingerprint [--all | --new]
  catalogger query --tech akamai --host .fr --grep "internal" --curl
  catalogger stats
  catalogger ingest-proxify proxify_logs.jsonl [--follow]
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg


def _dsn() -> str:
    dsn = os.environ.get("CATALOGGER_DSN")
    if not dsn:
        sys.exit("set CATALOGGER_DSN (e.g. postgresql://catalogger:pw@127.0.0.1/catalogger)")
    return dsn


def _connect():
    return psycopg.connect(_dsn(), autocommit=False)


def cmd_initdb(_):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "schema.sql"), encoding="utf-8") as fh:
        sql = fh.read()
    with _connect() as conn:
        conn.execute(sql)
        conn.commit()
    print("schema applied")


def cmd_fingerprint(args):
    from .fingerprint import fingerprint_all
    with _connect() as conn:
        n = fingerprint_all(conn, only_untagged=args.new)
    print(f"fingerprinted {n} flows")


def cmd_query(args):
    from .query import search, load_flow
    with _connect() as conn:
        rows = search(
            conn, tech=args.tech, host=args.host, status=args.status,
            method=args.method, grep=args.grep, program=args.program,
            session=args.session, since=args.since, limit=args.limit,
        )
        if not rows:
            print("no matches")
            return
        for fid, ts, method, status, url, host, fps, sess, prog in rows:
            tags = ",".join(fps) if fps else "-"
            print(f"#{fid:<8} {ts:%Y-%m-%d %H:%M} {str(status):>3} {method:<6} "
                  f"[{tags}] {url}")
        if args.curl:
            print("\n--- replay ---")
            for fid, *_ in rows:
                f = load_flow(conn, fid)
                if f:
                    print(f"# flow {fid}\n{f.to_curl()}\n")


def cmd_show(args):
    from .query import load_full, render_flow
    with _connect() as conn:
        d = load_full(conn, args.id)
    if not d:
        sys.exit(f"no flow #{args.id}")
    print(render_flow(d, body_limit=args.maxbody))


def cmd_stats(_):
    from .query import stats
    with _connect() as conn:
        s = stats(conn)
    print(f"flows:            {s['flows']:,}")
    print(f"unique bodies:    {s['unique_bodies']:,}")
    print(f"body occurrences: {s['body_occurrences']:,}")
    print(f"dedup ratio:      {s['dedup_ratio']}x  (occurrences / unique)")
    print(f"bodies on disk:   {s['bodies_on_disk']}")
    print(f"db total:         {s['db_total']}")
    if s["top_tech"]:
        print("top tech:")
        for t, c in s["top_tech"]:
            print(f"  {c:>8,}  {t}")


def cmd_ingest_proxify(args):
    from .sources.proxify_tail import ingest_proxify
    ingest_proxify(args.path, follow=args.follow, program=args.program,
                   session_id=args.session, collapse=args.collapse)


def cmd_serve(args):
    from .viewer import serve
    serve(host=args.host, port=args.port)


def main(argv=None):
    p = argparse.ArgumentParser(prog="catalogger")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(func=cmd_initdb)

    fp = sub.add_parser("fingerprint")
    fp.add_argument("--new", action="store_true", help="only flows with no tags yet")
    fp.add_argument("--all", dest="new", action="store_false")
    fp.set_defaults(func=cmd_fingerprint, new=False)

    q = sub.add_parser("query")
    q.add_argument("--tech", action="append", help="repeatable; matches all given")
    q.add_argument("--host")
    q.add_argument("--status", type=int)
    q.add_argument("--method")
    q.add_argument("--grep", help="full-text over response bodies")
    q.add_argument("--program")
    q.add_argument("--session")
    q.add_argument("--since")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--curl", action="store_true", help="emit replayable curl per hit")
    q.set_defaults(func=cmd_query)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    sh = sub.add_parser("show", help="full request/response detail for one flow")
    sh.add_argument("id", type=int)
    sh.add_argument("--maxbody", type=int, default=4000, help="body char limit")
    sh.set_defaults(func=cmd_show)

    sv = sub.add_parser("serve", help="launch the local fuzzy-finder GUI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)

    ip = sub.add_parser("ingest-proxify")
    ip.add_argument("path")
    ip.add_argument("--follow", action="store_true")
    ip.add_argument("--program")
    ip.add_argument("--session")
    ip.add_argument("--collapse", action="store_true")
    ip.set_defaults(func=cmd_ingest_proxify)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
