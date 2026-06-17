#!/usr/bin/env bash
# Route GUI browsers through the catalogger mitmproxy and trust its CA.
#
#   ./scripts/setup-browsers.sh
#
# Chrome/Chromium read the proxy from the graphical-session environment and
# trust certs from the system store + their NSS db. Firefox uses its own proxy
# prefs and cert store, so it needs a profile-level user.js.
set -euo pipefail
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
[ -f "$CA" ] || { echo "mitmproxy CA not found at $CA (start the capture service first)"; exit 1; }

# 1) session-wide proxy env (Chrome, Electron apps, ...) — applies next login
mkdir -p "$HOME/.config/environment.d"
cat > "$HOME/.config/environment.d/95-catalogger-proxy.conf" <<'EOF'
HTTP_PROXY=http://127.0.0.1:8888
HTTPS_PROXY=http://127.0.0.1:8888
http_proxy=http://127.0.0.1:8888
https_proxy=http://127.0.0.1:8888
ALL_PROXY=http://127.0.0.1:8888
NO_PROXY=localhost,127.0.0.1,::1,.anthropic.com
no_proxy=localhost,127.0.0.1,::1,.anthropic.com
EOF
echo "[1] wrote ~/.config/environment.d/95-catalogger-proxy.conf"

# 2) Chrome/Chromium NSS trust store
if command -v certutil >/dev/null; then
  mkdir -p "$HOME/.pki/nssdb"
  [ -f "$HOME/.pki/nssdb/cert9.db" ] || certutil -d sql:"$HOME/.pki/nssdb" -N --empty-password
  certutil -d sql:"$HOME/.pki/nssdb" -D -n "mitmproxy" 2>/dev/null || true
  certutil -d sql:"$HOME/.pki/nssdb" -A -t "C,," -n "mitmproxy" -i "$CA"
  echo "[2] imported CA into Chrome NSS store"
else
  echo "[2] certutil missing — Chrome will rely on the system trust store"
fi

# 3) Firefox profiles: proxy prefs + trust system/enterprise roots
shopt -s nullglob
profs=("$HOME"/.mozilla/firefox/*.default*)
if [ ${#profs[@]} -eq 0 ]; then
  echo "[3] no Firefox profile yet — launch Firefox once, then re-run this script"
else
  for p in "${profs[@]}"; do
    cat > "$p/user.js" <<'EOF'
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "127.0.0.1");
user_pref("network.proxy.http_port", 8888);
user_pref("network.proxy.ssl", "127.0.0.1");
user_pref("network.proxy.ssl_port", 8888);
user_pref("network.proxy.share_proxy_settings", true);
user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1, ::1, .anthropic.com");
user_pref("security.enterprise_roots.enabled", true);
user_pref("network.http.http3.enable", false);
EOF
    echo "[3] configured Firefox profile: $p"
  done
fi

echo "Log out/in (or relaunch the browser from a proxied terminal) for changes to apply."
