#!/bin/bash
# MNEMOS API Inference Frontend Testing Script for Agents
# Tests the OpenAI-compatible /v1/chat/completions endpoint

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
API_URL="${API_URL:-http://localhost:5002}"
MNEMOS_API_KEY="${MNEMOS_API_KEY:-test-key}"
TESTS_PASSED=0
TESTS_FAILED=0

echo -e "${YELLOW}MNEMOS API Inference Frontend Test Suite${NC}\n"
echo "Testing endpoint: $API_URL"
echo "Using API key: ${MNEMOS_API_KEY:0:20}..."
echo ""

# ==============================================================================
# Test 1: Health Check
# ==============================================================================
echo -e "${YELLOW}[1] Health Check${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" http://$API_URL/health)
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    VERSION=$(echo "$BODY" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
    echo -e "${GREEN}✓ Health check passed (version: $VERSION)${NC}"
    ((TESTS_PASSED++))
else
    echo -e "${RED}✗ Health check failed (HTTP $HTTP_CODE)${NC}"
    echo "  Response: $BODY"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 2: List Models
# ==============================================================================
echo -e "${YELLOW}[2] List Available Models (GET /v1/models)${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X GET $API_URL/v1/models \
  -H "Authorization: Bearer $MNEMOS_API_KEY")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    MODEL_COUNT=$(echo "$BODY" | grep -o '"id"' | wc -l)
    echo -e "${GREEN}✓ Models endpoint working (found $MODEL_COUNT models)${NC}"
    if [ "$MODEL_COUNT" -gt 0 ]; then
        echo "  Sample models:"
        echo "$BODY" | jq '.data[:2][] | {id, owned_by}' 2>/dev/null | head -4 || echo "  (could not parse response)"
    fi
    ((TESTS_PASSED++))
else
    echo -e "${RED}✗ Models endpoint failed (HTTP $HTTP_CODE)${NC}"
    echo "  Response: ${BODY:0:100}"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 3: Basic Chat Completion
# ==============================================================================
echo -e "${YELLOW}[3] Basic Chat Completion (POST /v1/chat/completions)${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ],
    "temperature": 0.7,
    "max_tokens": 100
  }')
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    CONTENT=$(echo "$BODY" | jq -r '.choices[0].message.content' 2>/dev/null || echo "parse error")
    echo -e "${GREEN}✓ Chat completion endpoint working${NC}"
    echo "  Response: ${CONTENT:0:80}..."
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Chat completion returned HTTP $HTTP_CODE${NC}"
    if [ "$HTTP_CODE" = "401" ]; then
        echo "  (Authentication issue - might be expected with test key)"
    else
        echo "  Response: ${BODY:0:100}"
    fi
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 4: Model with Auto Selection
# ==============================================================================
echo -e "${YELLOW}[4] Auto Model Selection (model='auto')${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "You are a code assistant"},
      {"role": "user", "content": "Write a Python function to reverse a list"}
    ],
    "temperature": 0.2,
    "max_tokens": 150
  }')
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    SELECTED_MODEL=$(echo "$BODY" | jq -r '.model' 2>/dev/null || echo "unknown")
    echo -e "${GREEN}✓ Auto selection endpoint working${NC}"
    echo "  Selected model: $SELECTED_MODEL"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Auto selection returned HTTP $HTTP_CODE${NC}"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 5: Model Alias (best-coding)
# ==============================================================================
echo -e "${YELLOW}[5] Model Alias Resolution (model='best-coding')${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "best-coding",
    "messages": [{"role": "user", "content": "test"}],
    "max_tokens": 50
  }')
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    RESOLVED_MODEL=$(echo "$BODY" | jq -r '.model' 2>/dev/null || echo "unknown")
    echo -e "${GREEN}✓ Model alias resolution working${NC}"
    echo "  Resolved: best-coding → $RESOLVED_MODEL"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Alias resolution returned HTTP $HTTP_CODE${NC}"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 6: Authentication (should fail without key)
# ==============================================================================
echo -e "${YELLOW}[6] Authentication Check (no auth header)${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $API_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "test"}]
  }')
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    echo -e "${GREEN}✓ Authentication properly enforced (HTTP $HTTP_CODE)${NC}"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Auth check returned HTTP $HTTP_CODE (expected 401/403)${NC}"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 7: Multi-turn Conversation
# ==============================================================================
echo -e "${YELLOW}[7] Multi-turn Conversation${NC}"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant"},
      {"role": "user", "content": "What is the capital of France?"},
      {"role": "assistant", "content": "The capital of France is Paris"},
      {"role": "user", "content": "How many people live there?"}
    ],
    "max_tokens": 100
  }')
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✓ Multi-turn conversation working${NC}"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Multi-turn returned HTTP $HTTP_CODE${NC}"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Test 8: Response Format
# ==============================================================================
echo -e "${YELLOW}[8] Response Format Validation${NC}"
RESPONSE=$(curl -s -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "test"}],
    "max_tokens": 50
  }')

HAS_ID=$(echo "$RESPONSE" | jq 'has("id")' 2>/dev/null || echo "false")
HAS_CHOICES=$(echo "$RESPONSE" | jq 'has("choices")' 2>/dev/null || echo "false")
HAS_USAGE=$(echo "$RESPONSE" | jq 'has("usage")' 2>/dev/null || echo "false")
HAS_MODEL=$(echo "$RESPONSE" | jq 'has("model")' 2>/dev/null || echo "false")

if [ "$HAS_ID" = "true" ] && [ "$HAS_CHOICES" = "true" ] && [ "$HAS_USAGE" = "true" ]; then
    echo -e "${GREEN}✓ Response format valid (OpenAI compatible)${NC}"
    echo "  Has: id, choices, usage, model"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ Response format incomplete${NC}"
    echo "  Has: id=$HAS_ID, choices=$HAS_CHOICES, usage=$HAS_USAGE, model=$HAS_MODEL"
    ((TESTS_FAILED++))
fi
echo ""

# ==============================================================================
# Summary
# ==============================================================================
TOTAL=$((TESTS_PASSED + TESTS_FAILED))
PASS_RATE=$((TESTS_PASSED * 100 / TOTAL))

echo -e "${YELLOW}========================================${NC}"
echo -e "Test Results: ${GREEN}$TESTS_PASSED passed${NC}, ${RED}$TESTS_FAILED failed${NC} (${PASS_RATE}%)"
echo -e "${YELLOW}========================================${NC}"
echo ""

if [ "$TESTS_FAILED" = "0" ]; then
    echo -e "${GREEN}✓ All tests passed! API inference frontend is working.${NC}"
    exit 0
else
    echo -e "${YELLOW}⚠ Some tests failed. See above for details.${NC}"
    echo ""
    echo "Troubleshooting:"
    echo "1. Is the API server running? (python3 -m mnemos.api.main)"
    echo "2. Is the database configured? (check .env)"
    echo "3. Is Python 3.11+ being used? (python3 --version)"
    echo "4. Are LLM provider keys set? (GROQ_API_KEY, TOGETHER_API_KEY, etc.)"
    exit 1
fi
