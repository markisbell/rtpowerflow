#!/usr/bin/env bash
# =============================================================================
#  netzsim (rtpowerflow) — Linux-Installer
#
#  Installiert ALLES, was der Echtzeit-Netzsimulator braucht:
#    * Systempakete (Python 3, venv, pip, curl, xz)  -> apt | dnf | pacman | zypper
#    * die Python-Abhaengigkeiten in ein projektlokales .venv (requirements.txt)
#    * Node.js >= 20, falls die Distribution keins mitbringt (offizielles
#      Tarball von nodejs.org, SHA256-geprueft, nach .tools/ — ohne root)
#    * die npm-Pakete der Oberflaeche (ui/node_modules)
#
#  Der Datensatz (Netze, LPG-Profile, Szenarien) liegt bereits im Repository —
#  es wird nichts weiter heruntergeladen.
#
#  Aufruf:   bash install.sh [Optionen]
#  Danach:   ./start_netzsim.sh
#
#  Das Skript ist idempotent: mehrfaches Ausfuehren aktualisiert nur, was fehlt.
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/markisbell/rtpowerflow.git"
PY_MIN="3.10"          # untere Grenze laut pyproject.toml
PY_PREFERRED=(python3.12 python3.11 python3.13 python3.10 python3)
NODE_MIN=18            # vite 6 laeuft ab 18 ...
NODE_WANT=20           # ... vitest 4 braucht 20 -> so viel installieren wir neu
NODE_FALLBACK="v22.20.0"   # nur falls nodejs.org/dist/index.json nicht erreichbar

# ---- Optionen ---------------------------------------------------------------
WITH_DEV=0; NO_ROOT=0; BUILD_TOOLS=0; SKIP_UI=0; RECREATE=0; VERIFY=1
PY_FORCE=""; NODE_FORCE=""

usage() {
    cat <<'EOF'
netzsim Linux-Installer

  bash install.sh [Optionen]

Optionen:
  --dev              zusaetzlich die Test-Abhaengigkeiten (pytest, httpx)
  --no-root          niemals sudo benutzen (Systempakete muessen vorhanden sein;
                     Node wird dann bei Bedarf lokal nach .tools/ entpackt)
  --python PFAD      diesen Interpreter fuer das .venv verwenden
  --node-version V   diese Node-Version lokal installieren (z. B. v22.20.0)
  --build-tools      zusaetzlich Compiler installieren (nur noetig, wenn ein
                     Python-Paket ohne fertiges Wheel gebaut werden muss)
  --skip-ui          nur das Backend einrichten (keine npm-Pakete)
  --recreate         .venv und lokales Node neu anlegen statt aktualisieren
  --no-verify        den abschliessenden Selbsttest ueberspringen
  -h, --help         diese Hilfe

Beispiele:
  bash install.sh                 # Standard: Backend + UI, Systempakete via sudo
  bash install.sh --dev           # zusaetzlich die Testsuite lauffaehig machen
  bash install.sh --no-root       # ohne Administratorrechte (nichts systemweit)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dev)          WITH_DEV=1 ;;
        --no-root)      NO_ROOT=1 ;;
        --build-tools)  BUILD_TOOLS=1 ;;
        --skip-ui)      SKIP_UI=1 ;;
        --recreate)     RECREATE=1 ;;
        --no-verify)    VERIFY=0 ;;
        --python)       PY_FORCE="${2:-}"; shift ;;
        --node-version) NODE_FORCE="${2:-}"; shift ;;
        -y|--yes)       ;;   # akzeptiert, aber der Lauf ist ohnehin interaktionsfrei
        -h|--help)      usage; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ---- Ausgabe ----------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
else
    B=""; G=""; Y=""; R=""; C=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B$C" "$*" "$N"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '\n%sFEHLER:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# =============================================================================
#  0. Repository finden (erlaubt auch:  curl -fsSL .../install.sh | bash)
# =============================================================================
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pyproject.toml" ]; then
    ROOT="$SELF_DIR"                       # Normalfall: im geklonten Repo
else
    # Aus der Pipe gestartet -> Repository erst besorgen
    ROOT="${NETZSIM_DIR:-$PWD/rtpowerflow}"
    if [ -d "$ROOT/.git" ]; then
        info "Vorhandenes Repository in $ROOT wird aktualisiert."
        command -v git >/dev/null 2>&1 || die "git fehlt — bitte installieren."
        git -C "$ROOT" pull --ff-only || warn "git pull fehlgeschlagen, fahre mit dem Bestand fort."
    else
        step "Repository klonen nach $ROOT"
        command -v git >/dev/null 2>&1 || die "git fehlt — bitte installieren (z. B. 'sudo apt install git')."
        git clone --depth 1 "$REPO_URL" "$ROOT" || die "Klonen fehlgeschlagen."
    fi
fi
cd "$ROOT"
[ -f requirements.txt ] || die "requirements.txt nicht gefunden — $ROOT ist kein netzsim-Repository."

printf '%s=== netzsim Linux-Installer ===%s\n' "$B" "$N"
info "Projektverzeichnis: $ROOT"

# =============================================================================
#  1. Distribution / Paketmanager erkennen
# =============================================================================
step "System pruefen"

OS_NAME="unbekannt"
[ -r /etc/os-release ] && OS_NAME="$(. /etc/os-release && echo "${PRETTY_NAME:-$NAME}")"
ARCH="$(uname -m)"
info "System: $OS_NAME ($ARCH)"

PKG=""
for candidate in apt-get dnf pacman zypper apk; do
    if command -v "$candidate" >/dev/null 2>&1; then PKG="$candidate"; break; fi
done
[ -n "$PKG" ] && info "Paketmanager: $PKG" || warn "Kein bekannter Paketmanager gefunden."

# sudo nur, wenn noetig und erlaubt
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if [ "$NO_ROOT" -eq 0 ] && command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        NO_ROOT=1
    fi
fi
[ "$NO_ROOT" -eq 1 ] && info "Modus: ohne Administratorrechte (keine Systempakete)"

pkg_install() {   # pkg_install <paket> [<paket> ...]
    [ $# -gt 0 ] || return 0
    case "$PKG" in
        apt-get) $SUDO apt-get install -y --no-install-recommends "$@" ;;
        dnf)     $SUDO dnf install -y "$@" ;;
        pacman)  $SUDO pacman -S --needed --noconfirm "$@" ;;
        zypper)  $SUDO zypper --non-interactive install "$@" ;;
        apk)     $SUDO apk add --no-cache "$@" ;;
        *)       return 1 ;;
    esac
}

APT_UPDATED=0
pkg_refresh() {
    case "$PKG" in
        apt-get) [ "$APT_UPDATED" -eq 1 ] || { $SUDO apt-get update -qq && APT_UPDATED=1; } ;;
        *) : ;;
    esac
}

# =============================================================================
#  2. Systempakete
# =============================================================================
step "Systempakete"

if [ "$NO_ROOT" -eq 1 ] || [ -z "$PKG" ]; then
    info "uebersprungen — es wird nichts systemweit installiert."
    for tool in python3 curl tar; do
        command -v "$tool" >/dev/null 2>&1 || die "$tool fehlt und darf nicht installiert werden (--no-root)."
    done
    ok "python3, curl, tar vorhanden"
else
    case "$PKG" in
        apt-get) BASE_PKGS=(python3 python3-venv python3-pip curl ca-certificates xz-utils)
                 DEV_PKGS=(build-essential python3-dev) ;;
        dnf)     BASE_PKGS=(python3 python3-pip curl ca-certificates xz)
                 DEV_PKGS=(gcc gcc-c++ make python3-devel) ;;
        pacman)  BASE_PKGS=(python python-pip curl ca-certificates xz)
                 DEV_PKGS=(base-devel) ;;
        zypper)  BASE_PKGS=(python3 python3-pip curl ca-certificates xz)
                 DEV_PKGS=(gcc gcc-c++ make python3-devel) ;;
        apk)     BASE_PKGS=(python3 py3-pip curl ca-certificates xz)
                 DEV_PKGS=(build-base python3-dev) ;;
    esac
    pkg_refresh
    info "installiere: ${BASE_PKGS[*]}"
    pkg_install "${BASE_PKGS[@]}" || die "Installation der Systempakete fehlgeschlagen."
    if [ "$BUILD_TOOLS" -eq 1 ]; then
        info "installiere Compiler: ${DEV_PKGS[*]}"
        pkg_install "${DEV_PKGS[@]}" || warn "Compiler-Pakete konnten nicht installiert werden."
    fi
    ok "Systempakete bereit"
fi

# =============================================================================
#  3. Python-Interpreter waehlen
# =============================================================================
step "Python"

py_version_ok() {   # py_version_ok <exe>  -> 0, wenn >= PY_MIN
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= tuple(int(x) for x in "'"$PY_MIN"'".split(".")) else 1)' 2>/dev/null
}

PY=""
if [ -n "$PY_FORCE" ]; then
    command -v "$PY_FORCE" >/dev/null 2>&1 || die "Interpreter '$PY_FORCE' nicht gefunden."
    py_version_ok "$PY_FORCE" || die "'$PY_FORCE' ist aelter als Python $PY_MIN."
    PY="$PY_FORCE"
else
    for cand in "${PY_PREFERRED[@]}"; do
        command -v "$cand" >/dev/null 2>&1 || continue
        py_version_ok "$cand" || continue
        PY="$cand"; break
    done
fi
[ -n "$PY" ] || die "Kein Python >= $PY_MIN gefunden. Bitte python3 installieren (oder --python angeben)."

PY_VER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
info "Interpreter: $(command -v "$PY")  (Python $PY_VER)"
case "$PY_VER" in
    3.14.*|3.15.*) warn "Python $PY_VER ist sehr neu — fuer pandapower/numpy gibt es dort ggf. noch keine fertigen Wheels."
                   warn "Falls die Installation scheitert: 'bash install.sh --python python3.12' oder --build-tools." ;;
esac

# venv-Modul pruefen (auf Debian/Ubuntu ein eigenes Paket)
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
    die "Das venv-Modul fehlt. Auf Debian/Ubuntu: 'sudo apt install python3-venv' (bzw. python${PY_VER%.*}-venv)."
fi

# =============================================================================
#  4. Virtuelle Umgebung + Python-Pakete
# =============================================================================
step "Python-Abhaengigkeiten (.venv)"

if [ "$RECREATE" -eq 1 ] && [ -d .venv ]; then
    info "entferne bestehendes .venv (--recreate)"
    rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
    [ -d .venv ] && rm -rf .venv     # z. B. ein Windows-.venv im geteilten Ordner
    info "lege .venv an"
    "$PY" -m venv .venv || die "Anlegen der virtuellen Umgebung fehlgeschlagen."
else
    info ".venv vorhanden — wird aktualisiert"
fi
VPY="$ROOT/.venv/bin/python"

"$VPY" -m pip install --upgrade pip setuptools wheel --quiet || die "pip konnte nicht aktualisiert werden."
info "installiere: $(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' requirements.txt | tr '\n' ' ')"
"$VPY" -m pip install -r requirements.txt --quiet || die "pip install fehlgeschlagen. Tipp: 'bash install.sh --build-tools'."
if [ "$WITH_DEV" -eq 1 ]; then
    info "installiere Test-Abhaengigkeiten (pytest, httpx)"
    "$VPY" -m pip install -e ".[dev]" --quiet || die "Installation der dev-Extras fehlgeschlagen."
fi
ok "Python-Umgebung bereit ($("$VPY" -c 'import pandapower; print("pandapower", pandapower.__version__)' 2>/dev/null || echo 'pandapower — Version nicht lesbar'))"

# =============================================================================
#  5. Node.js
# =============================================================================
NODE_BIN=""
if [ "$SKIP_UI" -eq 1 ]; then
    step "Node.js — uebersprungen (--skip-ui)"
else
step "Node.js"

TOOLS="$ROOT/.tools"
node_major() { "$1" -v 2>/dev/null | sed 's/^v//; s/\..*//'; }

# Ein unter WSL sichtbares Windows-Node (/mnt/c/...) taugt nicht: es liefert
# Windows-Binaries in den Linux-Baum. Solche Treffer ignorieren wir bewusst.
is_windows_path() { case "$1" in /mnt/*|/c/*) return 0 ;; *) return 1 ;; esac; }
usable_node() {
    local exe="$1" path major
    [ -n "$exe" ] || return 1
    path="$(command -v "$exe" 2>/dev/null)" || return 1
    is_windows_path "$path" && return 1
    major="$(node_major "$path")"
    [ -n "$major" ] && [ "$major" -ge "$NODE_MIN" ]
}

[ "$RECREATE" -eq 1 ] && rm -rf "$TOOLS/node"

# a) bereits lokal installiertes Node (frueherer Lauf)
if [ -x "$TOOLS/node/bin/node" ] && usable_node "$TOOLS/node/bin/node"; then
    NODE_BIN="$TOOLS/node/bin"
    info "lokales Node: $("$NODE_BIN/node" -v)"
# b) Node aus dem System
elif usable_node node && command -v npm >/dev/null 2>&1 && ! is_windows_path "$(command -v npm)"; then
    NODE_BIN="$(dirname "$(command -v node)")"
    info "System-Node: $(node -v)"
    [ "$(node_major "$(command -v node)")" -lt "$NODE_WANT" ] && \
        warn "Node $(node -v) startet die Oberflaeche, fuer 'npm test' (vitest 4) waeren $NODE_WANT+ noetig."
fi

# c) nichts brauchbares -> aus der Distribution, sonst offizielles Tarball
if [ -z "$NODE_BIN" ]; then
    if [ "$NO_ROOT" -eq 0 ] && [ -n "$PKG" ]; then
        # Nur nehmen, wenn die Distribution wirklich >= NODE_WANT anbietet.
        cand_major=""
        case "$PKG" in
            apt-get) pkg_refresh
                     cand_major="$(apt-cache policy nodejs 2>/dev/null | sed -n 's/.*Candidate: *\([0-9]\+\).*/\1/p' | head -1)" ;;
            dnf)     cand_major="$(dnf info -q nodejs 2>/dev/null | sed -n 's/^Version *: *\([0-9]\+\).*/\1/p' | head -1)" ;;
            pacman)  cand_major="$(pacman -Si nodejs 2>/dev/null | sed -n 's/^Version *: *\([0-9]\+\).*/\1/p' | head -1)" ;;
            zypper)  cand_major="" ;;
        esac
        if [ -n "$cand_major" ] && [ "$cand_major" -ge "$NODE_WANT" ]; then
            info "installiere Node $cand_major aus der Distribution"
            pkg_install nodejs npm || warn "Distributions-Node konnte nicht installiert werden."
            usable_node node && NODE_BIN="$(dirname "$(command -v node)")"
        else
            info "Distribution bietet Node ${cand_major:-?} (< $NODE_WANT) — hole offizielles LTS-Paket"
        fi
    fi
fi

if [ -z "$NODE_BIN" ]; then
    # Offizielles Binaerpaket von nodejs.org — braucht kein root, landet in .tools/
    case "$ARCH" in
        x86_64|amd64) NARCH="x64" ;;
        aarch64|arm64) NARCH="arm64" ;;
        armv7l) NARCH="armv7l" ;;
        ppc64le) NARCH="ppc64le" ;; s390x) NARCH="s390x" ;;
        *) die "Keine Node-Binaries fuer Architektur '$ARCH' — bitte Node >= $NODE_MIN selbst installieren." ;;
    esac

    NVER="$NODE_FORCE"
    if [ -z "$NVER" ]; then
        NVER="$("$VPY" - <<'PYEOF' 2>/dev/null || true
import json, urllib.request
with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=20) as r:
    print(next(e["version"] for e in json.load(r) if e["lts"]))
PYEOF
)"
    fi
    [ -n "$NVER" ] || { NVER="$NODE_FALLBACK"; warn "Versionsliste nicht erreichbar — nehme $NVER."; }

    TARBALL="node-$NVER-linux-$NARCH.tar.xz"
    BASE_URL="https://nodejs.org/dist/$NVER"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    info "lade $TARBALL"
    curl -fsSL --retry 3 -o "$TMP/$TARBALL" "$BASE_URL/$TARBALL" \
        || die "Download von $BASE_URL/$TARBALL fehlgeschlagen (Netzwerk/Proxy?)."
    info "pruefe Signaturpruefsumme"
    curl -fsSL --retry 3 -o "$TMP/SHASUMS256.txt" "$BASE_URL/SHASUMS256.txt" \
        || die "Pruefsummendatei nicht erreichbar."
    ( cd "$TMP" && grep " $TARBALL\$" SHASUMS256.txt | sha256sum -c - >/dev/null 2>&1 ) \
        || die "SHA256-Pruefsumme von $TARBALL stimmt nicht — Download verworfen."
    mkdir -p "$TOOLS"
    rm -rf "$TOOLS/node"
    tar -xJf "$TMP/$TARBALL" -C "$TOOLS" || die "Entpacken fehlgeschlagen (xz vorhanden?)."
    mv "$TOOLS/node-$NVER-linux-$NARCH" "$TOOLS/node"
    rm -rf "$TMP"; trap - EXIT
    NODE_BIN="$TOOLS/node/bin"
    ok "Node $NVER lokal installiert (.tools/node)"
fi

export PATH="$NODE_BIN:$PATH"
ok "node $(node -v) · npm $(npm -v)"

# Die Starter finden das lokale Node ueber diese Datei.
if [ "$NODE_BIN" = "$TOOLS/node/bin" ]; then
    mkdir -p "$TOOLS"
    cat > "$TOOLS/env.sh" <<EOF
# Von install.sh erzeugt: legt das projektlokale Node in den PATH.
export PATH="$TOOLS/node/bin:\$PATH"
EOF
    info "PATH-Schnipsel: .tools/env.sh (wird von start_netzsim.sh geladen)"
fi

# =============================================================================
#  6. npm-Pakete der Oberflaeche
# =============================================================================
step "Oberflaeche (npm)"
if [ -d ui/node_modules ] && [ "$RECREATE" -eq 0 ]; then
    info "ui/node_modules vorhanden — aktualisiere"
fi
[ "$RECREATE" -eq 1 ] && rm -rf ui/node_modules
( cd ui && npm install --no-fund --no-audit ) || die "npm install fehlgeschlagen."
ok "$(ls ui/node_modules | wc -l) npm-Pakete in ui/node_modules"
fi   # SKIP_UI

# =============================================================================
#  7. Selbsttest
# =============================================================================
if [ "$VERIFY" -eq 1 ]; then
    step "Selbsttest"
    PYTHONPATH="$ROOT/src" "$VPY" -c 'import netzsim, pandapower, fastapi, uvicorn' \
        || die "Die Python-Umgebung laesst sich nicht importieren."
    ok "Importe in Ordnung"

    # npm blockiert seit Version 11 die postinstall-Skripte der Pakete. esbuild
    # bringt sein Binary zwar als optionale Plattform-Abhaengigkeit mit, aber
    # geprueft wird das hier — ohne esbuild startet die Oberflaeche nicht.
    if [ "$SKIP_UI" -eq 0 ] && [ -x ui/node_modules/.bin/esbuild ]; then
        if ESB="$(ui/node_modules/.bin/esbuild --version 2>/dev/null)"; then
            ok "esbuild $ESB einsatzbereit"
        else
            warn "esbuild laesst sich nicht ausfuehren (npm hat das postinstall-Skript
      uebersprungen). Abhilfe:  cd ui && npm approve-scripts esbuild && npm install"
        fi
    fi

    # Backend kurz auf einem freien Port starten und /health befragen.
    # Belegt-Pruefung ueber ss (sieht beide Adressfamilien); der bash-Fallback
    # bekommt ein Zeitlimit — ein Verbindungsversuch auf /dev/tcp kennt keinen
    # Timeout und blockiert sonst ewig (z. B. unter WSL2).
    port_busy() {
        if command -v ss >/dev/null 2>&1; then
            ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
        elif command -v timeout >/dev/null 2>&1; then
            timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" 2>/dev/null
        else
            return 1
        fi
    }
    PROBE_PORT=8000
    while [ "$PROBE_PORT" -lt 8060 ] && port_busy "$PROBE_PORT"; do
        PROBE_PORT=$((PROBE_PORT + 1))
    done
    info "starte das Backend testweise auf Port $PROBE_PORT (Kaltstart kann ~30 s dauern)"
    PROBE_LOG="$(mktemp)"
    PYTHONPATH="$ROOT/src" NETZSIM_PORT="$PROBE_PORT" NETZSIM_AUTOSTART=false \
        "$VPY" -m netzsim.main >"$PROBE_LOG" 2>&1 &
    PROBE_PID=$!
    HEALTH=""
    for _ in $(seq 1 60); do
        kill -0 "$PROBE_PID" 2>/dev/null || break
        # --noproxy: ein gesetzter http_proxy (Hochschulnetz) wuerde auch die
        # Anfrage an 127.0.0.1 ueber den Proxy leiten und damit scheitern.
        HEALTH="$(curl -fsS -m 2 --noproxy '*' "http://127.0.0.1:$PROBE_PORT/health" 2>/dev/null || true)"
        case "$HEALTH" in *'"netzsim"'*) break ;; *) HEALTH="" ;; esac
        sleep 1
    done
    kill "$PROBE_PID" 2>/dev/null || true
    wait "$PROBE_PID" 2>/dev/null || true
    if [ -n "$HEALTH" ]; then
        ok "Backend antwortet: $HEALTH"
        rm -f "$PROBE_LOG"
    else
        warn "Das Backend hat im Selbsttest nicht geantwortet. Letzte Zeilen des Protokolls:"
        tail -n 15 "$PROBE_LOG" >&2 || true
        warn "Protokoll: $PROBE_LOG"
    fi
fi

# =============================================================================
#  Fertig
# =============================================================================
cat <<EOF

${B}${G}Installation abgeschlossen.${N}

  Starten:   ${B}./start_netzsim.sh${N}      (oder: bash start_netzsim.sh)
  Beenden:   ${B}./stop_netzsim.sh${N}
  Tests:     ${B}.venv/bin/python -m pytest${N}$([ "$WITH_DEV" -eq 1 ] || echo "   (vorher: bash install.sh --dev)")

Die Oberflaeche oeffnet sich auf http://localhost:5173, das Backend
lauscht auf http://localhost:8000 (freie Ports werden automatisch gewaehlt,
so dass parallel z. B. rtheatflow laufen kann).
EOF
