"""Async ingest: a non-blocking queue + background writer.

The capture source calls submit() and returns IMMEDIATELY -- the response is
already on its way back to the client. All hashing, compression and DB writes
happen on the worker thread, off the request hot path. submit() never blocks;
under extreme backpressure it drops and counts rather than stalling the proxy.
"""
from __future__ import annotations

import os
import queue
import threading
import time

import psycopg

from .models import Flow
from .store import persist


class BatchWriter:
    def __init__(self, dsn: str, batch_size: int = 200,
                 flush_interval: float = 1.0, max_queue: int = 20000):
        self.dsn = dsn
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.q: "queue.Queue[Flow]" = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.written = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="catalogger-writer", daemon=True)
        self._t.start()

    @classmethod
    def from_env(cls, **kw) -> "BatchWriter":
        dsn = os.environ.get("CATALOGGER_DSN")
        if not dsn:
            raise RuntimeError("set CATALOGGER_DSN (e.g. postgresql://catalogger:pw@127.0.0.1/catalogger)")
        return cls(dsn, **kw)

    def submit(self, flow: Flow) -> None:
        try:
            self.q.put_nowait(flow)
        except queue.Full:
            self.dropped += 1  # never block the proxy

    def _run(self) -> None:
        conn = psycopg.connect(self.dsn, autocommit=False)
        try:
            while not (self._stop.is_set() and self.q.empty()):
                batch = self._drain()
                if not batch:
                    continue
                with conn.cursor() as cur:
                    ok = 0
                    for f in batch:
                        # Per-flow savepoint: a single bad flow rolls back only
                        # itself, not the whole batch (one NUL-laden body used to
                        # take down up to batch_size good flows with it).
                        try:
                            cur.execute("SAVEPOINT f")
                            persist(cur, f)
                            cur.execute("RELEASE SAVEPOINT f")
                            ok += 1
                        except Exception as e:
                            cur.execute("ROLLBACK TO SAVEPOINT f")
                            print(f"[catalogger] persist error (flow dropped): {e}")
                    conn.commit()
                    self.written += ok
        finally:
            conn.close()

    def _drain(self):
        batch = []
        deadline = time.monotonic() + self.flush_interval
        while len(batch) < self.batch_size:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                batch.append(self.q.get(timeout=timeout))
            except queue.Empty:
                break
        return batch

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._t.join(timeout=timeout)
        if self.dropped:
            print(f"[catalogger] dropped {self.dropped} flows under backpressure")
