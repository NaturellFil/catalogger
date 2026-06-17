"""Proxify capture source -- tails proxify's jsonl output into the dedup store.

Run proxify (Go, fast -- good for the fuzz firehose):
    proxify -addr ":8080" -o proxify_logs.jsonl

Then ingest, live-tailing the file:
    CATALOGGER_DSN=... python -m catalogger.cli ingest-proxify proxify_logs.jsonl --follow

Because proxify dumps to a file and this reads it separately, the proxy never
touches Postgres -- ingestion is fully decoupled from the request path.

NOTE: proxify's jsonl schema varies by version; Flow.from_proxify() maps the
common fields but verify against your build and adjust if a field is missing.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterator

from ..ingest import BatchWriter
from ..models import Flow


def _follow(path: str) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.25)
                continue
            yield line


def _all(path: str) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield from fh


def ingest_proxify(path: str, *, follow: bool = False, program=None,
                   session_id=None, collapse: bool = False) -> None:
    writer = BatchWriter.from_env()
    lines = _follow(path) if follow else _all(path)
    try:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            flow = Flow.from_proxify(
                obj, program=program, session_id=session_id,
                source_tool="proxify", collapse=collapse,
            )
            if flow.method == "CONNECT":
                continue  # proxy tunnel setup, not a real request
            writer.submit(flow)
    finally:
        writer.close()
