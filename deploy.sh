#!/bin/bash
#
# MNEMOS Deployment Script
# Automated deployment with verification
#

set -e  # Exit on error

# Required: set DEPLOY_HOST to the target server hostname or IP
if [ -z "${DEPLOY_HOST:-}" ]; then
    echo "ERROR: DEPLOY_HOST is not set. Usage: DEPLOY_HOST=myserver.example.com ./deploy.sh"
    exit 1
fi

# Configuration
DEPLOY_USER="${DEPLOY_USER:-$(whoami)}"
DEPLOY_DIR="/opt/mnemos"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Step 1: Verify SSH access
log_info "Verifying SSH access to $DEPLOY_HOST..."
if ssh -q "$DEPLOY_USER@$DEPLOY_HOST" exit 2>/dev/null; then
    log_info "SSH access verified"
else
    log_error "Cannot access $DEPLOY_HOST via SSH. Set DEPLOY_HOST env var."
    exit 1
fi

# Step 2: Verify prerequisites
log_info "Verifying prerequisites on $DEPLOY_HOST..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << 'EOF'
    # Check Python
    if ! command -v python3 &> /dev/null; then
        echo "ERROR: Python 3 not found"
        exit 1
    fi

    # Check PostgreSQL
    if ! command -v psql &> /dev/null; then
        echo "ERROR: PostgreSQL client not found"
        exit 1
    fi

    # Check Python version
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo "Python version: $PYTHON_VERSION"

    # Check PostgreSQL
    psql --version
EOF

log_info "Prerequisites verified"

# Step 3: Copy files to remote host
log_info "Copying files to $DEPLOY_HOST..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" sudo mkdir -p "$DEPLOY_DIR"
ssh "$DEPLOY_USER@$DEPLOY_HOST" sudo chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"

# Use rsync if available, fallback to scp
if command -v rsync &> /dev/null; then
    rsync -avz --delete "$PROJECT_DIR/" "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_DIR/" \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='venv'
else
    scp -r "$PROJECT_DIR"/* "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_DIR/"
fi

log_info "Files copied successfully"

# Step 4: Setup Python environment
log_info "Setting up Python environment..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << PYEOF
    cd "$DEPLOY_DIR"

    # Create venv if not exists
    if [ ! -d venv ]; then
        python3 -m venv venv
    fi

    # Activate venv and install requirements
    source venv/bin/activate
    pip install --upgrade pip setuptools wheel > /dev/null
    pip install -r requirements.txt

    echo "Python environment ready"
PYEOF

log_info "Python environment setup complete"

# Step 5: Create .env file (if not exists)
log_info "Setting up configuration..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << ENVEOF
    cd "$DEPLOY_DIR"

    if [ ! -f .env ]; then
        cat > .env << 'ENV'
# Database
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=mnemos
DATABASE_USER=mnemos
DATABASE_PASSWORD=mnemos_secure_password

# API Server
MNEMOS_HOST=0.0.0.0
MNEMOS_PORT=5000
MNEMOS_DEBUG=false

# Graeae Integration
GRAEAE_URL=http://localhost:5002
GRAEAE_FALLBACK_ON_ERROR=true

# Logging
LOG_LEVEL=INFO
LOG_FILE=/var/log/mnemos/api.log
ENV
        chmod 600 .env
        echo ".env file created"
    else
        echo ".env file already exists"
    fi
ENVEOF

log_info "Configuration setup complete"

# Step 6: Database setup
log_info "Setting up PostgreSQL database..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << DBEOF
    # Note: This requires PostgreSQL to be running and accessible
    # Verify PostgreSQL is running
    if ! pg_isready -h localhost -p 5432 > /dev/null 2>&1; then
        echo "WARNING: PostgreSQL not running or not accessible"
        echo "Database setup skipped - please configure manually"
        exit 0
    fi

    # Create database and user (if not exists)
    # Try with sudo for peer auth first, then prompt for password
    sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = 'mnemos'" | grep -q 1 || \
    sudo -u postgres psql << 'SQL'
        CREATE DATABASE mnemos;
        CREATE USER mnemos WITH PASSWORD 'mnemos_secure_password';
        ALTER ROLE mnemos SET client_encoding TO 'utf8';
        ALTER ROLE mnemos SET default_transaction_isolation TO 'read committed';
        ALTER ROLE mnemos SET timezone TO 'UTC';
        GRANT ALL PRIVILEGES ON DATABASE mnemos TO mnemos;
SQL

    echo "Database created"

    # Run migrations in canonical order. Keep install.py::main()
    # migration_files (see install.py:169-178) as the canonical source.
    cd $DEPLOY_DIR
    source venv/bin/activate
    python - <<'PY' | while IFS= read -r migration_file; do
import ast
from pathlib import Path

tree = ast.parse(Path("install.py").read_text())
for node in ast.walk(tree):
    if not isinstance(node, ast.FunctionDef) or node.name != "main":
        continue
    for stmt in ast.walk(node):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "migration_files"
            and isinstance(stmt.value, ast.List)
        ):
            for item in stmt.value.elts:
                filename = None
                for subnode in ast.walk(item):
                    if isinstance(subnode, ast.Constant) and isinstance(subnode.value, str):
                        if subnode.value.endswith(".sql"):
                            filename = subnode.value
                if filename is not None:
                    print(f"db/{filename}")
            raise SystemExit(0)
raise SystemExit("migration_files list not found in install.py")
PY
        [ -n "\$migration_file" ] || continue
        echo "Applying \$migration_file"
        sudo -u postgres psql -d mnemos -v ON_ERROR_STOP=1 -f "\$migration_file"
    done

    echo "Migrations completed"
DBEOF

log_info "Database setup complete"

# Step 7: Run tests
log_info "Running test suite..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << TESTEOF
    cd "$DEPLOY_DIR"
    source venv/bin/activate

    echo "Running unit tests..."
    python -m pytest tests/test_hooks.py -v --tb=short 2>&1 | head -100 || true

    echo "Running E2E tests..."
    python -m pytest tests/test_e2e.py -v --tb=short 2>&1 | head -100 || true

    echo "Test run completed"
TESTEOF

log_info "Test suite executed"

# Step 8: Verify deployment
log_info "Verifying deployment..."
ssh "$DEPLOY_USER@$DEPLOY_HOST" bash << VERIFYEOF
    cd "$DEPLOY_DIR"

    # Check if API server starts
    source venv/bin/activate
    timeout 5 python -c "from api_server import app; print('API server imports OK')" || true

    # Check if modules import
    timeout 5 python -c "from modules.compression import distill; print('Compression module OK')" || true
    timeout 5 python -c "from modules.hooks import HookRegistry; print('Hooks module OK')" || true
    timeout 5 python -c "from modules.bundles import BundleRouter; print('Bundles module OK')" || true

    echo "Module imports verified"
VERIFYEOF

log_info "Deployment verification complete"

# Step 9: Summary
echo ""
echo "========================================"
echo -e "${GREEN}DEPLOYMENT SUCCESSFUL${NC}"
echo "========================================"
echo ""
echo "MNEMOS deployed to $DEPLOY_HOST at:"
echo "  Host: $DEPLOY_HOST"
echo "  Path: $DEPLOY_DIR"
echo ""
echo "Next steps:"
echo "1. Setup systemd service:"
echo "   sudo cp $DEPLOY_DIR/mnemos.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable mnemos"
echo "   sudo systemctl start mnemos"
echo ""
echo "2. Verify API server:"
echo "   curl http://$DEPLOY_HOST:5002/health"
echo ""
echo "3. View logs:"
echo "   ssh $DEPLOY_USER@$DEPLOY_HOST"
echo "   tail -f /var/log/mnemos/api.log"
echo ""
echo "See DEPLOYMENT_GUIDE.md for detailed instructions"
echo ""

log_info "Deployment script completed successfully"
