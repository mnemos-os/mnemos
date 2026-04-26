#!/usr/bin/env bash
# =============================================================================
# MNEMOS Bootstrap Installer
# Handles system-level prerequisites, then hands off to python -m installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mnemos-os/mnemos/master/install.sh | bash
#   bash install.sh [--agent|--wizard|--unattended|--upgrade|--check]
#
# Supported: Debian/Ubuntu, RHEL/Fedora, macOS (Homebrew)
# Requires:  sudo (for package installation and service setup)
# =============================================================================

set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────────────

MNEMOS_VERSION="3.0.0"
MNEMOS_REPO="https://github.com/mnemos-os/mnemos"
INSTALL_DIR="${MNEMOS_INSTALL_DIR:-/opt/mnemos}"
INSTALLER_ARGS="${*:-}"
MIN_PYTHON_MINOR=11
MIN_DISK_GB=2

# ── Colors ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()  { echo -e "\n${CYAN}${BOLD}==> $*${RESET}"; }
die()   { error "$*"; exit 1; }

# ── Helpers ──────────────────────────────────────────────────────────────────

command_exists() { command -v "$1" &>/dev/null; }

require_sudo() {
    if [[ $EUID -eq 0 ]]; then
        SUDO=""
    elif command_exists sudo; then
        SUDO="sudo"
        # Validate sudo access
        if ! sudo -n true 2>/dev/null; then
            warn "sudo access required for package installation."
            sudo -v || die "Cannot obtain sudo access."
        fi
    else
        die "This installer requires sudo or root access."
    fi
}

detect_os() {
    OS_TYPE=""
    DISTRO=""
    DISTRO_VERSION=""
    PKG_MGR=""

    case "$(uname -s)" in
        Linux)
            OS_TYPE="linux"
            if [[ -f /etc/os-release ]]; then
                . /etc/os-release
                DISTRO="${ID:-unknown}"
                DISTRO_VERSION="${VERSION_ID:-}"
                # Normalize
                case "$DISTRO" in
                    debian|ubuntu|linuxmint|pop)   PKG_MGR="apt"   ;;
                    rhel|centos|rocky|almalinux)   PKG_MGR="dnf"   ;;
                    fedora)                        PKG_MGR="dnf"   ;;
                    arch|manjaro)                  PKG_MGR="pacman" ;;
                    opensuse*|sles)                PKG_MGR="zypper" ;;
                    *)                             PKG_MGR=""       ;;
                esac
            fi
            ;;
        Darwin)
            OS_TYPE="macos"
            DISTRO="macos"
            DISTRO_VERSION="$(sw_vers -productVersion 2>/dev/null || echo '')"
            PKG_MGR="brew"
            ;;
        *)
            die "Unsupported OS: $(uname -s). Use Docker or install manually."
            ;;
    esac

    info "OS: ${DISTRO} ${DISTRO_VERSION} (${OS_TYPE})"
}

find_python() {
    # Try python3.13, python3.12, python3.11, python3 in that order
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command_exists "$cmd"; then
            local minor
            minor=$("$cmd" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)
            local major
            major=$("$cmd" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)
            if [[ "$major" -eq 3 && "$minor" -ge $MIN_PYTHON_MINOR ]]; then
                PYTHON="$cmd"
                info "Python: $("$PYTHON" --version)"
                return 0
            fi
        fi
    done
    return 1
}

check_disk_space() {
    local free_gb
    if [[ "$OS_TYPE" == "macos" ]]; then
        free_gb=$(df -g / | awk 'NR==2 {print $4}')
    else
        free_gb=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')
    fi
    if [[ "${free_gb:-0}" -lt $MIN_DISK_GB ]]; then
        die "Insufficient disk space: ${free_gb}G free, need ${MIN_DISK_GB}G minimum."
    fi
    info "Disk: ${free_gb}G free"
}

# ── Package Installation ──────────────────────────────────────────────────────

install_apt_packages() {
    step "Installing system packages (apt)"
    $SUDO apt-get update -qq

    # Python — install interpreter, venv support, and pip
    if ! find_python 2>/dev/null; then
        info "Installing python3..."
        $SUDO apt-get install -y -qq python3 python3-dev
    fi
    # Always ensure venv and pip are present (Debian splits these into separate packages)
    find_python 2>/dev/null || true
    if [[ -n "${PYTHON:-}" ]]; then
        PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        $SUDO apt-get install -y -qq \
            "python${PY_VER}-venv" python3-venv python3-pip 2>/dev/null || \
        $SUDO apt-get install -y -qq python3-venv python3-pip || true
    fi

    # PostgreSQL
    if ! command_exists psql; then
        info "Installing postgresql and postgresql-contrib..."
        $SUDO apt-get install -y -qq postgresql postgresql-contrib libpq-dev

        # Start PostgreSQL
        if command_exists systemctl; then
            $SUDO systemctl enable postgresql
            $SUDO systemctl start postgresql
            sleep 2
        fi
    fi

    # pgvector extension
    local pg_ver
    pg_ver=$(psql --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1 | cut -d. -f1 || echo "")
    if [[ -n "$pg_ver" ]]; then
        $SUDO apt-get install -y -qq "postgresql-${pg_ver}-pgvector" 2>/dev/null || {
            warn "pgvector apt package not found for PostgreSQL ${pg_ver} — will install from source if needed"
        }
    fi

    # git (needed to clone repo)
    if ! command_exists git; then
        $SUDO apt-get install -y -qq git
    fi

    # Build tools (needed by some Python packages)
    $SUDO apt-get install -y -qq build-essential curl
}

install_dnf_packages() {
    step "Installing system packages (dnf)"
    $SUDO dnf install -y python3 python3-devel python3-pip postgresql-server postgresql-contrib \
        postgresql-devel git gcc make curl 2>/dev/null
    # Initialize PostgreSQL data directory if fresh install
    if [[ ! -d /var/lib/pgsql/data/base ]]; then
        $SUDO postgresql-setup --initdb 2>/dev/null || $SUDO postgresql-setup initdb 2>/dev/null || true
    fi
    $SUDO systemctl enable postgresql
    $SUDO systemctl start postgresql
}

install_brew_packages() {
    step "Installing packages (Homebrew)"
    if ! command_exists brew; then
        die "Homebrew is required on macOS. Install it from https://brew.sh"
    fi
    brew install postgresql@16 pgvector git python3 2>/dev/null || true
    brew services start postgresql@16 2>/dev/null || true
    export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
}

install_system_packages() {
    case "$PKG_MGR" in
        apt)    install_apt_packages ;;
        dnf)    install_dnf_packages ;;
        brew)   install_brew_packages ;;
        pacman) warn "Arch: install postgresql python python-pip pgvector git manually, then re-run" ;;
        *)      warn "Unknown package manager — skipping system package installation" ;;
    esac
}

# ── Repository Setup ──────────────────────────────────────────────────────────

setup_repo() {
    step "Setting up MNEMOS repository"

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "Repository already exists at $INSTALL_DIR"
        return 0
    fi

    if [[ -f "$INSTALL_DIR/api_server.py" ]]; then
        info "MNEMOS already installed at $INSTALL_DIR (non-git copy)"
        return 0
    fi

    # If install.sh is being run from within the repo directory, use it directly
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
    if [[ -f "${script_dir}/api_server.py" ]]; then
        info "Running from repo directory: $script_dir"
        if [[ "$script_dir" != "$INSTALL_DIR" ]]; then
            info "Linking/copying to $INSTALL_DIR"
            $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
            $SUDO cp -r "$script_dir" "$INSTALL_DIR" 2>/dev/null || \
            $SUDO ln -sf "$script_dir" "$INSTALL_DIR" 2>/dev/null || true
        fi
        return 0
    fi

    # Clone from GitHub
    info "Cloning MNEMOS from $MNEMOS_REPO..."
    $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
    $SUDO git clone --depth 1 "$MNEMOS_REPO" "$INSTALL_DIR"
    $SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
}

# ── config.toml bootstrap ─────────────────────────────────────────────────────

setup_config_example() {
    if [[ ! -f "$INSTALL_DIR/config.toml" && -f "$INSTALL_DIR/config.toml.example" ]]; then
        info "Creating initial config.toml from example..."
        cp "$INSTALL_DIR/config.toml.example" "$INSTALL_DIR/config.toml"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    echo -e "\n${BOLD}${CYAN}MNEMOS ${MNEMOS_VERSION} — Bootstrap Installer${RESET}"
    echo -e "${CYAN}$(printf '─%.0s' {1..50})${RESET}\n"

    # Parse args early — --check and --upgrade don't need system packages
    local skip_pkgs=false
    local py_args=""
    for arg in $INSTALLER_ARGS; do
        py_args="$py_args $arg"
        case "$arg" in
            --check|--upgrade) skip_pkgs=true ;;
        esac
    done

    detect_os
    check_disk_space
    require_sudo

    if [[ "$skip_pkgs" == "false" ]]; then
        install_system_packages
    fi

    # Ensure Python is found after package install
    if ! find_python; then
        die "Python 3.${MIN_PYTHON_MINOR}+ not found after package installation. Install manually."
    fi

    setup_repo
    setup_config_example

    step "Launching Python installer"
    cd "$INSTALL_DIR"
    exec "$PYTHON" -m installer $py_args
}

main "$@"
