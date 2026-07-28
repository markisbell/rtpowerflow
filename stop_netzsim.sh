#!/usr/bin/env bash
# =============================================================================
#  netzsim Stopper (Linux): beendet Backend, Oberflaeche und verwaiste
#  Hintergrundprozesse (node/esbuild/python) DIESES Projekts.
#
#  Gegenstueck zu stop_netzsim.bat und bewusst port-unabhaengig: der Starter
#  waehlt die Ports dynamisch (scripts/pick_ports.sh). Erkannt wird deshalb
#  ueber die gemerkten PIDs und ueber Arbeitsverzeichnis/Kommandozeile der
#  Prozesse. Ein parallel laufendes rtheatflow bleibt unberuehrt — ebenso ein
#  netzsim aus einem ANDEREN Verzeichnis.
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$PWD"
RUN="$ROOT/.run"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else B=""; G=""; Y=""; N=""; fi

printf '%s=== netzsim Stopper ===%s\n' "$B" "$N"

# Gehoert der Prozess zu DIESEM Projektordner? Das Backend laeuft als
# relatives ".venv/bin/python -m netzsim.main" — im Kommandozeilentext steht
# der Pfad also nicht. Deshalb zaehlt zusaetzlich das Arbeitsverzeichnis.
belongs_to_project() {   # belongs_to_project <pid>
    local pid="$1" cmd cwd
    [ "$pid" = "$$" ] && return 1
    [ "$pid" = "${PPID:-0}" ] && return 1
    # 2>/dev/null MUSS vor der Eingabeumleitung stehen: verschwindet der Prozess
    # zwischen Auflistung und Lesen, meldet sonst die Shell selbst den Fehler.
    cmd="$(2>/dev/null tr '\0' ' ' < "/proc/$pid/cmdline")" || return 1
    [ -n "$cmd" ] || return 1
    case "$cmd" in
        *python*|*node*|*npm*|*esbuild*|*vite*) ;;
        *) return 1 ;;
    esac
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    case "$cwd" in "$ROOT"|"$ROOT"/*) return 0 ;; esac
    case "$cmd" in *"$ROOT"*) return 0 ;; esac
    return 1
}

project_pids() {
    local p pid
    for p in /proc/[0-9]*; do
        pid="${p#/proc/}"
        belongs_to_project "$pid" && printf '%s\n' "$pid"
    done
}

# ---------- 1. die gemerkten PIDs aus .run/ ---------------------------------
killed=0
for f in "$RUN"/backend.pid "$RUN"/ui.pid; do
    [ -f "$f" ] || continue
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null && killed=$((killed + 1))
    fi
    rm -f "$f"
done
[ "$killed" -gt 0 ] && printf '  %s Serverprozess(e) beendet (gemerkte PIDs).\n' "$killed"

# ---------- 2. Nachzuegler dieses Projekts ----------------------------------
mapfile -t rest < <(project_pids)
if [ "${#rest[@]}" -gt 0 ]; then
    printf '  beende %s weitere Prozess(e) dieses Projekts ...\n' "${#rest[@]}"
    kill -TERM "${rest[@]}" 2>/dev/null || true
fi

# ---------- 3. Gnadenfrist, dann hart ---------------------------------------
for _ in $(seq 1 10); do
    mapfile -t rest < <(project_pids)
    [ "${#rest[@]}" -eq 0 ] && break
    sleep 0.5
done
mapfile -t rest < <(project_pids)
if [ "${#rest[@]}" -gt 0 ]; then
    printf '  %s Prozess(e) reagieren nicht — SIGKILL.\n' "${#rest[@]}"
    kill -KILL "${rest[@]}" 2>/dev/null || true
    sleep 0.5
fi

mapfile -t rest < <(project_pids)
if [ "${#rest[@]}" -gt 0 ]; then
    printf '%sWARNUNG:%s %s netzsim-Prozess(e) laufen noch: %s\n' "$Y" "$N" "${#rest[@]}" "${rest[*]}"
    exit 1
fi
printf '%sAlle netzsim-Dienste beendet.%s\n' "$G" "$N"
