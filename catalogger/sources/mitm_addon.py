"""mitmproxy capture source.

Run:  CATALOGGER_DSN=... CATALOGGER_PROGRAM=my-program \
      CATALOGGER_SESSION=$CLAUDE_SESSION_ID \
      mitmdump -s catalogger/sources/mitm_addon.py

Point your tools (and Claude Code's shell) at the proxy:
      export HTTPS_PROXY=http://127.0.0.1:8080 HTTP_PROXY=http://127.0.0.1:8080
and trust mitmproxy's CA (~/.mitmproxy/mitmproxy-ca-cert.pem).

The response() hook only enqueues -- it never touches Postgres -- so it adds
no latency to the request path.
"""
import os

from catalogger.ingest import BatchWriter
from catalogger.models import Flow

_PROGRAM = os.environ.get("CATALOGGER_PROGRAM")
_SESSION = os.environ.get("CATALOGGER_SESSION")
_SOURCE = os.environ.get("CATALOGGER_SOURCE_TOOL", "mitm")
_COLLAPSE = os.environ.get("CATALOGGER_COLLAPSE", "").lower() in ("1", "true", "yes")


class Catalogger:
    def __init__(self):
        self.writer = BatchWriter.from_env()

    def response(self, flow):
        self.writer.submit(Flow.from_mitm(
            flow,
            program=_PROGRAM,
            session_id=_SESSION,
            source_tool=_SOURCE,
            collapse=_COLLAPSE,
        ))

    def done(self):
        self.writer.close()


addons = [Catalogger()]
