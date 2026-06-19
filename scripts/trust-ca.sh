#!/usr/bin/env bash
# Trust the mitmproxy capture CA everywhere capture needs it.
#
# Why this exists: tools that ship their own trust store (Java's per-JDK
# cacerts, Python certifi, Node) don't honor the system CA store, so they fail
# the TLS handshake through the capture proxy. The usual "fix" is to bypass the
# proxy — which means those requests never reach the DB. This closes that gap.
#
#   bash trust-ca.sh        # run once (after the capture proxy has generated its CA)
set -euo pipefail
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
JKS="$HOME/.local/share/catalogger/cacerts.jks"

[ -f "$CA" ] || { echo "mitmproxy CA not found at $CA — start the capture proxy once first."; exit 1; }

# 1. System trust store: covers curl/wget/openssl/git and anything using it.
if ! trust list 2>/dev/null | grep -qi mitmproxy; then
  echo "adding mitmproxy CA to the system trust store (needs sudo)…"
  sudo trust anchor "$CA"
fi

# 2. Java: every JDK ships its OWN cacerts and ignores system trust, and the
#    JDKs here are ephemeral per-project downloads — so per-JDK keytool import
#    is whack-a-mole. Instead extract one store (public CAs + mitmproxy) and let
#    JAVA_TOOL_OPTIONS (set in proxy.env) pin every JVM to it, JDK-agnostic.
trust extract --overwrite --format=java-cacerts --filter=ca-anchors --purpose server-auth "$JKS"
echo "wrote $JKS — Java tools now trust the capture CA via \$JAVA_TOOL_OPTIONS"
