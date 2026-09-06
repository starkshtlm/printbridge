#!/usr/bin/env bash
# printbridge installer — one command from a bare Linux server to a printed test page.
#
#   curl -fsSL https://raw.githubusercontent.com/starkshtlm/printbridge/main/install.sh | bash
#
# It checks the OS, installs Docker if it is missing, clones the repo and runs `printbridge init`,
# which asks three questions and prints the link you paste into your assistant.
#
# It never edits anything outside its own directory except installing Docker (with your consent)
# and, if you ask for it later, one systemd timer named printbridge-doctor.
set -euo pipefail

REPO_URL="${PRINTBRIDGE_REPO:-https://github.com/starkshtlm/printbridge}"
TARGET_DIR="${PRINTBRIDGE_DIR:-$HOME/printbridge}"
BRANCH="${PRINTBRIDGE_BRANCH:-main}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_sudo() {
    if [ "$(id -u)" = "0" ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || die "This step needs root and sudo is not installed."
        SUDO="sudo"
    fi
}

say "printbridge installer"

# --- 1. the operating system --------------------------------------------------------------
[ "$(uname -s)" = "Linux" ] || die "printbridge installs on Linux. On macOS or Windows, run it on a small Linux server or a Raspberry Pi."
if [ -r /etc/os-release ]; then . /etc/os-release; info "system: ${PRETTY_NAME:-unknown}"; fi
case "$(uname -m)" in
    x86_64|aarch64|arm64) info "architecture: $(uname -m)" ;;
    *) info "architecture: $(uname -m) — untested, the build may take a while" ;;
esac

# --- 2. git -------------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    say "Installing git"
    need_sudo
    if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y git
    elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git
    elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git
    else die "Could not install git automatically. Install it and run this again."
    fi
fi

# --- 3. docker ----------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker"
    info "printbridge runs in two containers; Docker is the only dependency."
    need_sudo
    curl -fsSL https://get.docker.com | $SUDO sh
    $SUDO usermod -aG docker "$(id -un)" || true
    info "added $(id -un) to the docker group — log out and back in if the next step says 'permission denied'"
fi
if ! docker compose version >/dev/null 2>&1; then
    die "Docker is installed but 'docker compose' is not available. Install the compose plugin and run this again."
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
info "docker: $($DOCKER --version)"

# --- 4. disk ------------------------------------------------------------------------------
FREE_MB=$(df -Pm . | awk 'NR==2 {print $4}')
[ "${FREE_MB:-0}" -gt 3000 ] || die "Less than 3 GB free here; the images need about 1.5 GB."

# --- 5. the repository ---------------------------------------------------------------------
if [ -d "$TARGET_DIR/.git" ]; then
    say "Updating $TARGET_DIR"
    git -C "$TARGET_DIR" pull --ff-only
else
    say "Cloning into $TARGET_DIR"
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET_DIR"
fi
cd "$TARGET_DIR"

# --- 6. build and start ---------------------------------------------------------------------
say "Building (a few minutes the first time)"
nice -n 10 $DOCKER compose build

if [ ! -f .env ]; then
    say "First-run setup"
    info "Three questions, then a test page."
    cp .env.example .env
    chmod 600 .env
    # init rewrites .env from the answers; run it in a throwaway container so the app is not
    # started with a half-written configuration.
    $DOCKER compose run --rm --no-deps -T printbridge init --force || true
fi

say "Starting"
$DOCKER compose up -d

say "Checking"
sleep 5
$DOCKER compose exec -T printbridge printbridge doctor || true

say "Done"
cat <<'DONE'
  Next:
    1. Paste the connector link above into Claude, ChatGPT or your assistant of choice.
       (Lost it? `docker compose exec printbridge printbridge keys rotate <person>` prints a new one.)
    2. Say: "print a test page on my printer".

  Useful later:
    docker compose exec printbridge printbridge status     who is connected, what has been printed
    docker compose exec printbridge printbridge doctor     what is wrong and how to fix it
    docker compose exec printbridge printbridge keys add <person>

  The full guide, including the printer and the router: docs/SETUP.md
DONE
