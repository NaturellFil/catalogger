# catalogger 2.0

A local, deduplicated archive of every HTTP request/response on your machine —
built so that when you find a bypass on some technology *tomorrow*, you can pull
up **every host you ever hit that runs it**, with a replayable request for each.

It is not a live proxy window (Caido already does that well). It is the
**permanent, queryable memory** that sits behind one.

Ships with **observer**, a companion read-only viewer for live Claude Code
sessions (see [`observer/`](observer/)).

---

## What's new in 2.0

- **mitmproxy is the default capture engine.** It reliably stores full,
  decoded response bodies — catalogger's whole reason to exist. The addon
  writes **straight to Postgres** (no file tailing, no log rotation). proxify
  remains supported as a metadata-only firehose source, but note it does *not*
  emit response bodies.
- **One-command install** (`./install.sh`) — venv, a user-owned Postgres
  cluster, the capture service, and a fingerprint timer, all as **systemd user
  services** that autostart on login. No root except the Postgres package.
- **Whole-system capture**, including **browsers** — route every CLI tool and
  GUI app through the local proxy with a trusted CA.
- **Auto-fingerprinting** on a timer, so the `tech` facet stays current.
- Packaged: `pyproject.toml`, `catalogger` console entry point, fixed proxify
  v0.0.16 parser.

---

## The core idea

1. **Capture** — a proxy logs every flow. Easy.
2. **Retrieval** — "show me every host running tech X." This is where the
   design lives, with one consequence: **store full raw responses, not just
   metadata**, because the tech you'll want to search for next month is one you
   haven't thought to fingerprint yet.

So the headline operation is *running a brand-new detection rule across the
entire historical corpus* (`catalogger fingerprint --all`). Everything is
shaped around making that cheap.

### Dedup, concretely

Storage splits into **bodies** (one row per *unique* body, content-addressed by
sha256, zstd-compressed) and **flows** (full records that *point* at bodies by
hash). The millionth identical 404 body is a counter bump, not a new blob.
Full-text search indexes each distinct text body once, so it stays small no
matter how much you fuzz.

---

## Architecture

```
  Claude Code / curl / your browser / your Go tools
        │   HTTP(S)_PROXY=127.0.0.1:8888   (+ trusted CA)
        ▼
  ┌────────────────────┐   submit() — off the hot path   ┌──────────────────┐
  │ mitmproxy + addon  │ ───────────────────────────────▶│  Postgres (local)│
  │ (catalogger source)│                                 │  bodies (dedup)  │
  └────────────────────┘                                 │  flows / flow_agg│
                                                          │  body_text (FTS) │
                                                          └────────┬─────────┘
                                   fingerprint ──▶ tags ──▶ query / replay / GUI
```

---

## Install

```bash
sudo pacman -S --needed postgresql      # the only system package needed
./install.sh                            # venv, DB, services, launchers
sudo trust anchor ~/.mitmproxy/mitmproxy-ca-cert.pem   # trust the proxy CA
```

Then open a fresh terminal (so the proxy env loads) and:

```bash
curl https://example.com
catalogger query                # see it land
catalogger serve                # GUI at http://127.0.0.1:8765
```

Everything runs user-local under systemd:

```bash
systemctl --user status catalogger-pg catalogger-mitm
systemctl --user list-timers catalogger-fingerprint.timer
```

---

## Capturing browser traffic

Browsers ignore the shell proxy env and (Firefox) use their own cert store.
Run the helper, then log out/in:

```bash
./scripts/setup-browsers.sh
```

- **Chrome/Chromium** — proxy from the graphical-session env
  (`~/.config/environment.d`), CA trusted via the system store + NSS db.
- **Firefox** — `user.js` in each profile sets the proxy, enables
  `enterprise_roots` (so it trusts the system CA), and disables HTTP/3 (QUIC
  would otherwise bypass the proxy). Launch Firefox once first to create a
  profile, then re-run the script.

> If the capture proxy is stopped, proxied apps lose network access until it's
> back (it's a `Restart=on-failure` service). Comment the `proxy.env` source
> line in your shell rc / remove the `environment.d` file to disable.

---

## Usage

```bash
catalogger fingerprint --all                 # re-tag the whole corpus
catalogger query --tech cloudflare --host .fr --curl
catalogger query --grep "BIGipServer"        # FTS over deduped bodies
catalogger show <id>                         # full request/response detail
catalogger stats                             # corpus + dedup overview
catalogger serve                             # local search GUI
```

**The workflow this is built for:** you find a new bypass on, say, F5 BIG-IP.
Add one line to `RULES` in `catalogger/fingerprint.py`, then:

```bash
catalogger fingerprint --all
catalogger query --tech f5-big-ip --curl
```

→ every host across everything you ever proxied that runs it, replayable.

---

## Security

The archive contains live session tokens, auth headers, and internal hostnames.
Treat it as sensitive: Postgres is bound to `127.0.0.1` only, the DB password
and capture config are `chmod 600`, and `api.anthropic.com` is excluded from
the proxy by default. Keep the cluster on an encrypted volume.

---

## Layout

```
catalogger/        the Python package (CLI, dedup store, fingerprint, query, GUI)
  sources/         capture sources: mitm_addon.py (default), proxify_tail.py
observer/          read-only live viewer for Claude Code sessions
systemd/           user service + timer units
scripts/           init-db.sh, setup-browsers.sh
install.sh         one-command deployment
schema.sql         Postgres schema
```
