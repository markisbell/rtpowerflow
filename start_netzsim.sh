#!/usr/bin/env bash
# =============================================================================
#  netzsim Starter (Linux): Backend (FastAPI) + Oberflaeche (Vite)
#
#  Gegenstueck zu start_netzsim.bat. Die Ports sind NICHT fest verdrahtet:
#  Standard 8000/5173 (oder NETZSIM_PORT / NETZSIM_UI_PORT aus Umgebung/.env);
#  ist ein Port von einer FREMDEN Anwendung belegt (z. B. rtheatflow), wird
#  automatisch der naechste freie genommen. Ein bereits laufendes netzsim wird
#  an /health ("app": "netzsim") erkannt und wiederverwendet.
#
#  Beide Server laufen im Hintergrund; Protokolle und PIDs liegen in .run/.
#  Beenden: ./stop_netzsim.sh
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$PWD"
RUN="$ROOT/.run"
mkdir -p "$RUN"

OPEN_BROWSER=1
BACKEND_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-browser|--headless) OPEN_BROWSER=0 ;;
        --backend-only)          BACKEND_ONLY=1 ;;
        -h|--help)
            echo "Aufruf: ./start_netzsim.sh [--no-browser] [--backend-only]"; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
info() { printf '%s\n' "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '\n%sFEHLER:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# projektlokales Node (von install.sh angelegt), falls vorhanden
# shellcheck source=/dev/null
[ -f "$ROOT/.tools/env.sh" ] && . "$ROOT/.tools/env.sh"

printf '%s=== netzsim Starter ===%s\n' "$B" "$N"

# Server im Hintergrund starten: sie sollen das Starterfenster ueberleben.
# Die PID landet in .run/<name>.pid; Kindprozesse (esbuild) findet der Stopper
# ueber die Kommandozeile — genau wie unter Windows.
# setsid + stdin auf /dev/null: beim Schliessen des Terminals geht SIGHUP an
# dessen Prozessgruppe, und npm/vite setzen EIGENE Signal-Handler — die
# Oberflaeche stirbt dann trotz nohup still (das Backend ignoriert stdin und
# ueberlebt). Eine eigene Session haengt an keinem Terminal mehr. Ohne fork
# bleibt $! die richtige PID (der Aufrufer ist nie Prozessgruppenfuehrer).
launch() {   # launch <name> <logfile> -- <befehl...>
    local name="$1" log="$2"; shift 3
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$@" >"$log" 2>&1 </dev/null &
    else
        nohup "$@" >"$log" 2>&1 </dev/null &
    fi
    echo $! > "$RUN/$name.pid"
    disown 2>/dev/null || true
}

# --noproxy: mit gesetztem http_proxy (Hochschul-/Firmennetz) wuerde curl selbst
# eine Anfrage an 127.0.0.1 ueber den Proxy schicken — der sie nicht erreichen
# kann. Ohne diesen Schalter erkennt der Starter sein eigenes Backend nicht.
#
# EINE Anfrage, EIN Urteil: waehrend des Kaltstarts nimmt uvicorn die
# Verbindung schon an, antwortet aber noch nicht. Zwei getrennte Anfragen
# ("antwortet ueberhaupt?" / "ist es netzsim?") koennen daher unterschiedlich
# ausgehen — und der Starter haette faelschlich eine fremde Anwendung gemeldet.
probe() {        # probe <port> <pfad> -> gibt den Antworttext aus (leer = keine Antwort)
    local h body
    for h in 127.0.0.1 '[::1]'; do
        body="$(curl -fsS -m 3 --noproxy '*' "http://$h:$1$2" 2>/dev/null)" || continue
        [ -n "$body" ] && { printf '%s' "$body"; return 0; }
    done
    return 1
}
is_netzsim() {   # is_netzsim <port> <pfad>
    case "$(probe "$1" "$2" || true)" in *'"netzsim"'*) return 0 ;; *) return 1 ;; esac
}

# ---------- Backend-Port bestimmen ------------------------------------------
out="$(bash scripts/pick_ports.sh api)" || die "Kein freier Backend-Port gefunden."
eval "$out"; API_PORT="$PORT"; API_REUSE="$REUSE"

if [ "$API_REUSE" = "1" ]; then
    info "Backend laeuft bereits auf Port $API_PORT — wird wiederverwendet."
else
    [ -x .venv/bin/python ] || die "Die virtuelle Umgebung .venv fehlt. Einmalig einrichten mit:
       bash install.sh"
    info "Starte Backend auf http://localhost:$API_PORT ..."
    PYTHONPATH="$ROOT/src" NETZSIM_PORT="$API_PORT" \
        launch backend "$RUN/backend.log" -- .venv/bin/python -m netzsim.main

    # Warten, bis das Backend antwortet (Kaltstart laedt pandapower: 20-60 s).
    # Identitaet pruefen, nicht nur Erreichbarkeit: bindet in der Kaltstart-
    # Luecke eine FREMDE Anwendung den Port, wird abgebrochen statt die
    # Oberflaeche still an das falsche Backend zu verdrahten.
    info "Warte auf das Backend ..."
    up=0
    for _ in $(seq 1 60); do
        answer="$(probe "$API_PORT" /health || true)"
        case "$answer" in
            '')            : ;;                       # noch keine Antwort — weiter warten
            *'"netzsim"'*) up=1; break ;;
            *)  die "Port $API_PORT wurde waehrend des Starts von einer fremden Anwendung
       belegt (Antwort ist kein netzsim). Bitte den Starter erneut ausfuehren —
       er waehlt dann den naechsten freien Port.
       Protokoll: $RUN/backend.log" ;;
        esac
        if [ -f "$RUN/backend.pid" ] && ! kill -0 "$(cat "$RUN/backend.pid")" 2>/dev/null; then
            printf '\n%sFEHLER:%s Das Backend hat sich beendet. Letzte Zeilen:\n' "$R" "$N" >&2
            tail -n 20 "$RUN/backend.log" >&2 || true
            exit 1
        fi
        sleep 1
    done
    [ "$up" = "1" ] || warn "Backend antwortet noch nicht — siehe $RUN/backend.log"
fi

if [ "$BACKEND_ONLY" = "1" ]; then
    printf '\n%sFertig:%s Backend unter http://localhost:%s\n' "$G" "$N" "$API_PORT"
    exit 0
fi

# ---------- UI-Port bestimmen (nach dem Backend, damit der Proxy-Check -------
# ---------- gegen ein lebendes Backend laeuft) -------------------------------
out="$(NETZSIM_PORT="$API_PORT" bash scripts/pick_ports.sh ui)" || die "Kein freier UI-Port gefunden."
eval "$out"; UI_PORT="$PORT"; UI_REUSE="$REUSE"

if [ "$UI_REUSE" = "1" ]; then
    info "Oberflaeche laeuft bereits auf Port $UI_PORT — wird wiederverwendet."
else
    command -v npm >/dev/null 2>&1 || die "npm nicht gefunden. Einmalig einrichten mit: bash install.sh"
    if [ ! -d ui/node_modules ]; then
        info "Hinweis: ui/node_modules fehlt — installiere einmalig die Abhaengigkeiten ..."
        ( cd ui && npm install --no-fund --no-audit ) || die "npm install fehlgeschlagen."
    fi
    info "Starte Oberflaeche auf http://localhost:$UI_PORT (Backend-Proxy: Port $API_PORT) ..."
    NETZSIM_PORT="$API_PORT" NETZSIM_UI_PORT="$UI_PORT" \
        launch ui "$RUN/ui.log" -- npm --prefix ui run dev

    # Identitaetscheck durch den Vite-Proxy: /api/health muss netzsim melden.
    for _ in $(seq 1 30); do
        if is_netzsim "$UI_PORT" /api/health; then break; fi
        sleep 1
    done
    is_netzsim "$UI_PORT" /api/health || warn "Oberflaeche antwortet noch nicht — siehe $RUN/ui.log
   (dort steht auch, auf welchem Port Vite tatsaechlich gestartet ist)."
fi

# ---------- Browser ---------------------------------------------------------
URL="http://localhost:$UI_PORT/"
if [ "$OPEN_BROWSER" = "1" ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
fi

cat <<EOF

${B}${G}Fertig:${N} Oberflaeche ${B}$URL${N}   ·   Backend http://localhost:$API_PORT

  Protokolle:  .run/backend.log · .run/ui.log
  Beenden:     ./stop_netzsim.sh
EOF
