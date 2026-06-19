#!/usr/bin/env bash
# catalogger installer — sets up a user-local deployment from this repo.
#
# Reproduces: venv + deps, a user-owned Postgres cluster, the mitmproxy capture
# service, the fingerprint timer, the `catalogger` launcher, and the systemd
# user services. No root required EXCEPT installing the system
# packages (postgresql, and optionally the proxy CA into the trust store).
#
#   ./install.sh
#
# Prereqs (install with your package manager, e.g. on Arch):
#   sudo pacman -S --needed postgresql
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HOME/.local/share/catalogger"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"

say() { printf '\n\033[1;36m[catalogger]\033[0m %s\n' "$*"; }

command -v initdb >/dev/null || { echo "Postgres not found — install it first (e.g. sudo pacman -S postgresql)"; exit 1; }

say "deploying package to $ROOT"
mkdir -p "$ROOT" "$BIN" "$UNITS"
cp -r "$REPO/catalogger" "$ROOT/"
cp "$REPO/schema.sql" "$REPO/requirements.txt" "$REPO/config.example.env" "$ROOT/"
cp "$REPO/scripts/init-db.sh" "$ROOT/"; chmod +x "$ROOT/init-db.sh"
cp "$REPO/scripts/trust-ca.sh" "$ROOT/"; chmod +x "$ROOT/trust-ca.sh"

say "creating venv + installing dependencies (incl. mitmproxy)"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ROOT/.venv/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt" mitmproxy

if [ ! -f "$ROOT/.env" ]; then
  say "generating .env (random DB password)"
  PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
  cat > "$ROOT/.env" <<EOF
export CATALOGGER_DSN="postgresql://catalogger:${PW}@127.0.0.1:5432/catalogger"
export CATALOGGER_PROGRAM="\${CATALOGGER_PROGRAM:-default}"
export CATALOGGER_SESSION="\${CATALOGGER_SESSION:-\$(date +%Y%m%d-%H%M)}"
EOF
  chmod 600 "$ROOT/.env"
fi

# systemd EnvironmentFile form (no `export`)
DSN=$(grep -oP 'postgresql://[^"]+' "$ROOT/.env")
cat > "$ROOT/capture.env" <<EOF
CATALOGGER_DSN=$DSN
CATALOGGER_PROGRAM=system
CATALOGGER_SESSION=always-on
PYTHONPATH=$ROOT
EOF
chmod 600 "$ROOT/capture.env"

# shell proxy env (sourced from rc files)
cat > "$ROOT/proxy.env" <<'EOF'
export HTTP_PROXY=http://127.0.0.1:8888
export HTTPS_PROXY=http://127.0.0.1:8888
export http_proxy=http://127.0.0.1:8888
export https_proxy=http://127.0.0.1:8888
export NO_PROXY="localhost,127.0.0.1,::1,.anthropic.com,api.anthropic.com"
export no_proxy="$NO_PROXY"

# --- TLS: trust the mitmproxy CA so tools don't fail the proxied handshake ---
# Without this, Java/Python/Node use private trust stores, fail TLS through the
# proxy, and get "fixed" by bypassing capture — a blind spot in the DB. The CA
# files below already contain mitmproxy after running scripts/trust-ca.sh.
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
export NODE_EXTRA_CA_CERTS="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
# Java ignores all of the above and every JDK ships its own cacerts, so point
# all JVMs at a generated store that includes the mitmproxy CA (JDK-agnostic).
# Guarded: no store -> don't set it, so a missing file can't break TLS instead.
[ -f "$HOME/.local/share/catalogger/cacerts.jks" ] && \
  export JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=$HOME/.local/share/catalogger/cacerts.jks -Djavax.net.ssl.trustStorePassword=changeit"
EOF

say "installing launcher (catalogger)"
cat > "$BIN/catalogger" <<EOF
#!/usr/bin/env bash
set -euo pipefail
ROOT="$ROOT"
[ -f "\$ROOT/.env" ] && source "\$ROOT/.env"
export PYTHONPATH="\$ROOT\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$ROOT/.venv/bin/python" -m catalogger.cli "\$@"
EOF
chmod +x "$BIN/catalogger"

say "installing systemd user services"
for u in catalogger-pg catalogger-mitm catalogger-fingerprint.service catalogger-fingerprint.timer; do
  [ -f "$REPO/systemd/$u" ] && cp "$REPO/systemd/$u" "$UNITS/"
  [ -f "$REPO/systemd/$u.service" ] && cp "$REPO/systemd/$u.service" "$UNITS/"
done
systemctl --user daemon-reload

say "initialising Postgres cluster + schema"
systemctl --user enable --now catalogger-pg.service
bash "$ROOT/init-db.sh" || true   # idempotent: starts pg, creates role+db
"$BIN/catalogger" initdb

say "starting capture + fingerprint services"
systemctl --user enable --now catalogger-mitm.service
systemctl --user enable --now catalogger-fingerprint.timer

# wire the proxy env into the user's shells (idempotent)
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] || continue
  grep -q "catalogger/proxy.env" "$rc" || \
    printf '\n# catalogger traffic capture (comment out to disable)\n[ -f "$HOME/.local/share/catalogger/proxy.env" ] && source "$HOME/.local/share/catalogger/proxy.env"\n' >> "$rc"
done

cat <<EOF

$(say "done")
Remaining manual steps:
  1. Trust the mitmproxy CA so HTTPS can be read AND so Java/Python/Node tools
     don't fail TLS through the proxy (and get bypassed, blinding capture):
       bash ~/.local/share/catalogger/trust-ca.sh   # runs once; uses sudo for the system store
  2. Optional — capture browser traffic too: see README "Capturing browser traffic".
  3. Open a fresh terminal so the proxy env loads, then:
       curl https://example.com && catalogger query

GUIs:
  catalogger serve     # archive GUI   -> http://127.0.0.1:8765

The live Claude Code session viewer (observer) now lives in its own repo:
  https://github.com/NaturellFil/observer
EOF
