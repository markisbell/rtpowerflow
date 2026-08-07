#!/usr/bin/env bash
# Port picker for the Linux dev launcher (start_netzsim.sh) — the twin of
# pick_ports.ps1.
#
# Problem it solves: several apps share the same default ports (netzsim and
# rtheatflow both like :8000/:5173). "Port is busy" therefore does NOT mean
# "netzsim is already running". This script decides per role:
#   - a listener that answers /health with app=netzsim  -> REUSE=1 (don't start)
#   - a foreign/unresponsive listener                   -> try the next port
#   - the first free port                               -> REUSE=0 (start there)
# The UI role checks /api/health through the Vite proxy, so a stale dev server
# whose backend is gone (proxy-500) is skipped instead of reused.
#
# Output (eval'ed by the launcher): two lines "PORT=<n>" and "REUSE=<0|1>".
# Defaults honor NETZSIM_PORT / NETZSIM_UI_PORT from the environment or .env.
set -euo pipefail

MODE="${1:-api}"
RANGE="${2:-20}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# minimal .env reader: first "NAME=value" line, no quoting/expansion
read_dotenv() {
    [ -f "$ROOT/.env" ] || return 1
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^[:space:]#]\{1,\}\).*/\1/p" "$ROOT/.env" | head -1
}

base_port() {   # base_port <VAR> <fallback>
    local v="${!1-}"
    [ -n "$v" ] || v="$(read_dotenv "$1" || true)"
    case "$v" in ''|*[!0-9]*) echo "$2" ;; *) echo "$v" ;; esac
}

# Something bound to this port? ss sees both address families; the bash
# /dev/tcp fallback covers containers without iproute2. That fallback MUST run
# under `timeout`: a /dev/tcp connect has no timeout of its own and can block
# forever when the connection is silently dropped (seen under WSL2).
listening() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH "sport = :$p" 2>/dev/null | grep -q . && return 0 || return 1
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$p" 2>/dev/null && return 0
    fi
    return 1
}

# Identity, not mere reachability: only OUR app may be reused. Both loopbacks,
# because uvicorn binds 127.0.0.1 while a dev server may sit on ::1.
# --noproxy is essential: with http_proxy set (campus/company network) curl
# would send even a 127.0.0.1 request to the proxy, which cannot reach it.
is_netzsim() {
    local p="$1" path="$2" h body
    for h in 127.0.0.1 '[::1]'; do
        body="$(curl -fsS -m 2 --noproxy '*' "http://$h:$p$path" 2>/dev/null)" || continue
        case "$body" in *'"app"'*'"netzsim"'*) return 0 ;; esac
    done
    return 1
}

if [ "$MODE" = "api" ]; then
    BASE="$(base_port NETZSIM_PORT 8000)"; PROBE_PATH="/health"
else
    BASE="$(base_port NETZSIM_UI_PORT 5173)"; PROBE_PATH="/api/health"
fi

REUSE=0
PORT="$BASE"
LIMIT=$((BASE + RANGE))
while [ "$PORT" -lt "$LIMIT" ]; do
    listening "$PORT" || break
    if is_netzsim "$PORT" "$PROBE_PATH"; then REUSE=1; break; fi
    PORT=$((PORT + 1))
done

if [ "$PORT" -ge "$LIMIT" ]; then
    echo "kein freier Port in $BASE..$((LIMIT - 1)) gefunden" >&2
    exit 1
fi

echo "PORT=$PORT"
echo "REUSE=$REUSE"
