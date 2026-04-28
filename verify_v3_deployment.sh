#!/bin/bash
# MNEMOS Deployment Verification Script
# Checks database migrations, endpoints, and deployment hygiene

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

API_URL="${API_URL:-http://localhost:5002}"
AUTH_KEY="${MNEMOS_API_KEY:-test-key}"

echo -e "${YELLOW}MNEMOS v3.0.0 Deployment Verification${NC}\n"

# ==============================================================================
# 1. DATABASE MIGRATIONS
# ==============================================================================
echo -e "${YELLOW}[1] Verifying Database Migrations${NC}"

# Check if psql is available
if ! command -v psql &> /dev/null; then
    echo -e "${RED}✗ psql not found. Cannot verify database schema.${NC}"
    echo "  Install PostgreSQL client tools to verify migrations."
else
    PG_HOST="${PG_HOST:-localhost}"
    PG_DATABASE="${PG_DATABASE:-mnemos}"
    PG_USER="${PG_USER:-postgres}"

    # Check consultation_memory_refs table
    if PGPASSWORD="${PG_PASSWORD}" psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DATABASE" -c \
        "SELECT to_regclass('consultation_memory_refs');" 2>/dev/null | grep -q consultation_memory_refs; then
        echo -e "${GREEN}✓ consultation_memory_refs table exists${NC}"
    else
        echo -e "${RED}✗ consultation_memory_refs table not found${NC}"
        echo "  Run: psql -d mnemos -f db/migrations_v3_graeae_unified.sql"
    fi

    # Check graeae_audit_log table
    if PGPASSWORD="${PG_PASSWORD}" psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DATABASE" -c \
        "SELECT to_regclass('graeae_audit_log');" 2>/dev/null | grep -q graeae_audit_log; then
        echo -e "${GREEN}✓ graeae_audit_log table exists${NC}"
    else
        echo -e "${YELLOW}⚠ graeae_audit_log table not found (check your migrations)${NC}"
    fi
fi

echo ""

# ==============================================================================
# 2. SERVICE HEALTH
# ==============================================================================
echo -e "${YELLOW}[2] Checking Service Health${NC}"

if curl -s "$API_URL/health" > /dev/null 2>&1; then
    HEALTH=$(curl -s "$API_URL/health")
    VERSION=$(echo "$HEALTH" | grep -o '"version"[^,}]*' | cut -d'"' -f4)

    if [ "$VERSION" = "3.0.0" ]; then
        echo -e "${GREEN}✓ Service running, version $VERSION${NC}"
    else
        echo -e "${YELLOW}⚠ Service running, but version is $VERSION (expected 3.0.0)${NC}"
    fi
else
    echo -e "${RED}✗ Service not responding at $API_URL${NC}"
    echo "  Start the service with: python -m mnemos.api.main"
    exit 1
fi

echo ""

# ==============================================================================
# 3. NEW v3.0.0 ENDPOINTS
# ==============================================================================
echo -e "${YELLOW}[3] Testing New /v1/ Endpoints${NC}"

# Test /v1/consultations endpoint exists
CONSULT_CODE=$(curl -s -w "%{http_code}" -X POST "$API_URL/v1/consultations" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"test","task_type":"reasoning"}' \
    -o /dev/null)

if [ "$CONSULT_CODE" = "200" ] || [ "$CONSULT_CODE" = "201" ]; then
    echo -e "${GREEN}✓ POST /v1/consultations responds${NC}"
elif [ "$CONSULT_CODE" = "401" ] || [ "$CONSULT_CODE" = "403" ]; then
    echo -e "${YELLOW}⚠ POST /v1/consultations exists (auth required)${NC}"
else
    echo -e "${RED}✗ POST /v1/consultations returned $CONSULT_CODE${NC}"
fi

# Test /v1/consultations/audit
AUDIT_CODE=$(curl -s -w "%{http_code}" -X GET "$API_URL/v1/consultations/audit" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -o /dev/null)

if [ "$AUDIT_CODE" = "200" ]; then
    echo -e "${GREEN}✓ GET /v1/consultations/audit responds${NC}"
elif [ "$AUDIT_CODE" = "401" ] || [ "$AUDIT_CODE" = "403" ]; then
    echo -e "${YELLOW}⚠ GET /v1/consultations/audit exists (auth required)${NC}"
else
    echo -e "${RED}✗ GET /v1/consultations/audit returned $AUDIT_CODE${NC}"
fi

# Test /v1/providers
PROVIDERS_CODE=$(curl -s -w "%{http_code}" -X GET "$API_URL/v1/providers" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -o /dev/null)

if [ "$PROVIDERS_CODE" = "200" ]; then
    echo -e "${GREEN}✓ GET /v1/providers responds${NC}"
elif [ "$PROVIDERS_CODE" = "401" ] || [ "$PROVIDERS_CODE" = "403" ]; then
    echo -e "${YELLOW}⚠ GET /v1/providers exists (auth required)${NC}"
else
    echo -e "${RED}✗ GET /v1/providers returned $PROVIDERS_CODE${NC}"
fi

# Test /v1/memories
MEMORIES_CODE=$(curl -s -w "%{http_code}" -X GET "$API_URL/v1/memories" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -o /dev/null)

if [ "$MEMORIES_CODE" = "200" ]; then
    echo -e "${GREEN}✓ GET /v1/memories responds${NC}"
elif [ "$MEMORIES_CODE" = "401" ] || [ "$MEMORIES_CODE" = "403" ]; then
    echo -e "${YELLOW}⚠ GET /v1/memories exists (auth required)${NC}"
else
    echo -e "${RED}✗ GET /v1/memories returned $MEMORIES_CODE${NC}"
fi

echo ""

# ==============================================================================
# 5. CODE STRUCTURE VERIFICATION
# ==============================================================================
echo -e "${YELLOW}[5] Verifying Code Structure${NC}"

# Check that new handlers exist
if [ -f "api/handlers/consultations.py" ]; then
    echo -e "${GREEN}✓ api/handlers/consultations.py exists${NC}"
else
    echo -e "${RED}✗ api/handlers/consultations.py not found${NC}"
fi

if [ -f "api/handlers/providers.py" ]; then
    echo -e "${GREEN}✓ api/handlers/providers.py exists${NC}"
else
    echo -e "${RED}✗ api/handlers/providers.py not found${NC}"
fi

# Check for migration file
if [ -f "db/migrations_v3_graeae_unified.sql" ]; then
    echo -e "${GREEN}✓ db/migrations_v3_graeae_unified.sql exists${NC}"
else
    echo -e "${RED}✗ db/migrations_v3_graeae_unified.sql not found${NC}"
fi

# Check for .env.example
if [ -f ".env.example" ]; then
    echo -e "${GREEN}✓ .env.example exists${NC}"
else
    echo -e "${RED}✗ .env.example not found${NC}"
fi

# Check for DEPLOYMENT.md
if [ -f "DEPLOYMENT.md" ]; then
    echo -e "${GREEN}✓ DEPLOYMENT.md exists${NC}"
else
    echo -e "${RED}✗ DEPLOYMENT.md not found${NC}"
fi

echo ""

# ==============================================================================
# 6. INTERNAL REFERENCES SANITIZATION
# ==============================================================================
echo -e "${YELLOW}[6] Checking for Internal References (Sanitization)${NC}"

# Check for hardcoded IPs / internal hostnames across every tracked file
# except this script itself (which legitimately enumerates the patterns) and
# CHANGELOG (which has one self-referential bullet describing the scrub).
# Patterns are stored encoded so this literal block doesn't match itself.
#
# The decoder reverses base64 inline; the source-of-truth strings only exist
# as bytes at scan time.
_p_192=$(printf '%s' "MTkyLjE2OC4yMDcu" | base64 -d)   # internal /24 prefix
_p_10=$(printf '%s'  "MTAuMC4w" | base64 -d)            # RFC1918 /24 prefix
_p_py=$(printf '%s'  "UFlUSElB" | base64 -d)            # internal hostname 1
_p_ce=$(printf '%s'  "Q0VSQkVSVVM=" | base64 -d)        # internal hostname 2
_p_pr=$(printf '%s'  "UFJPVEVVUw==" | base64 -d)        # internal hostname 3
_p_ar=$(printf '%s'  "QVJHT05BUw==" | base64 -d)        # internal hostname 4

INTERNAL_PATTERNS=("$_p_192" "$_p_10" "$_p_py" "$_p_ce" "$_p_pr" "$_p_ar")
FOUND_REFS=0

# Files we intentionally skip:
#   - this script (the encoded patterns above would false-match post-decode)
#   - CHANGELOG.md (one self-referential bullet documenting the scrub)
#   - anything under archive/ or .git/
_SELF="$(basename "$0")"

for pat in "${INTERNAL_PATTERNS[@]}"; do
    if git ls-files 2>/dev/null \
        | grep -v -E "^(archive/|\\.git/|CHANGELOG\\.md$|\\.gitignore$|${_SELF}\$)" \
        | xargs -r grep -l -- "$pat" 2>/dev/null \
        | head -1 > /dev/null; then
        echo -e "${RED}✗ Found tracked reference to '$pat'${NC}"
        FOUND_REFS=$((FOUND_REFS + 1))
    fi
done

if [ "$FOUND_REFS" = "0" ]; then
    echo -e "${GREEN}✓ No hardcoded infrastructure references in tracked files${NC}"
else
    echo -e "${YELLOW}⚠ Found $FOUND_REFS internal references (check if intentional)${NC}"
fi

echo ""

# ==============================================================================
# 7. ENVIRONMENT CONFIGURATION
# ==============================================================================
echo -e "${YELLOW}[7] Checking Environment Configuration${NC}"

if [ -z "$MNEMOS_API_KEY" ]; then
    echo -e "${YELLOW}⚠ MNEMOS_API_KEY not set (required for production)${NC}"
else
    echo -e "${GREEN}✓ MNEMOS_API_KEY is set${NC}"
fi

if [ -z "$PG_HOST" ]; then
    echo -e "${YELLOW}⚠ PG_HOST not set (uses default: localhost)${NC}"
else
    echo -e "${GREEN}✓ PG_HOST is set to $PG_HOST${NC}"
fi

if [ -z "$GPU_PROVIDER_HOST" ]; then
    echo -e "${YELLOW}ℹ GPU_PROVIDER_HOST not set (GPU optional, uses default providers)${NC}"
else
    echo -e "${GREEN}✓ GPU_PROVIDER_HOST is set to $GPU_PROVIDER_HOST${NC}"
fi

echo ""

# ==============================================================================
# SUMMARY
# ==============================================================================
echo -e "${YELLOW}[Summary]${NC}"
echo -e "✓ v3.0.0 deployment verification complete"
echo -e "✓ Check results above for any issues"
echo ""
echo -e "Next steps:"
echo -e "  1. Review any ${RED}✗${NC} (errors) above"
echo -e "  2. Review any ${YELLOW}⚠${NC} (warnings) and confirm intentional"
echo -e "  3. Run integration tests: pytest tests/test_v3_integration.py -v"
echo -e "  4. Deploy to production when ready"
echo ""
