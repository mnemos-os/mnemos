#!/bin/bash
# Nightly tier-2 verification of the MNEMOS bridge family.
#
# Runs each model-API integration test (openai, gemini, anthropic) against
# the live PYTHIA MCP HTTP/SSE endpoint, captures structured pass/fail +
# duration for each, and writes a single summary memory back to MNEMOS.
#
# This is operational telemetry — catches SDK drift (Gemini SDK deprecations,
# OpenAI tool-call shape changes, Anthropic tool_use breaking renames) the
# moment they happen instead of waiting for a release cut.
#
# Cost envelope: 6 API calls per night (~$0.005). Negligible at fleet scale.
#
# Schedule (PYTHIA): cron entry `15 6 * * * /usr/local/bin/bridge-tier2-nightly.sh`
# 06:15 UTC fires after the daily MORPHEUS run + before any operator action.
#
# Manual run: just invoke this script. It logs to /tmp/bridge-tier2-<date>.log
# and writes the summary memory regardless of any individual bridge failing.

set -uo pipefail

# Resolve config from /etc/mnemos/api_keys.json if present, falling back to
# already-exported env vars. Both paths land at the same shape.
KEYS_FILE="${MNEMOS_KEYS_FILE:-/home/jasonperlow/.api_keys_master.json}"
if [ -f "$KEYS_FILE" ]; then
    OPENAI_API_KEY="${OPENAI_API_KEY:-$(python3 -c "import json; d=json.load(open('$KEYS_FILE')); print(d['llm_providers']['openai']['api_key'])" 2>/dev/null)}"
    GOOGLE_API_KEY="${GOOGLE_API_KEY:-$(python3 -c "import json; d=json.load(open('$KEYS_FILE')); p=d['llm_providers']; print(p.get('google_gemini',{}).get('api_key') or p.get('google',{}).get('api_key',''))" 2>/dev/null)}"
    ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(python3 -c "import json; d=json.load(open('$KEYS_FILE')); print(d['llm_providers']['anthropic']['api_key'])" 2>/dev/null)}"
fi

export OPENAI_API_KEY GOOGLE_API_KEY ANTHROPIC_API_KEY
export MNEMOS_TEST_BASE="${MNEMOS_TEST_BASE:-http://192.168.207.67:5003/sse}"
export MNEMOS_MCP_TOKEN="${MNEMOS_MCP_TOKEN:-d3a3bc609583005f4a077b6ffd00154b4f03f70104d0cdbfbb019fceb28daca9}"
export MNEMOS_API_BASE="${MNEMOS_API_BASE:-http://192.168.207.67:5002}"
export MNEMOS_API_TOKEN="${MNEMOS_API_TOKEN:-$MNEMOS_MCP_TOKEN}"

VENV_PYTHON="${VENV_PYTHON:-/opt/mnemos-bridges/.venv/bin/python}"

DATE=$(date -u +%Y-%m-%d)
LOG="/tmp/bridge-tier2-${DATE}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date -uIseconds)] bridge-tier2-nightly start"
echo "  MCP base:       $MNEMOS_TEST_BASE"
echo "  API base:       $MNEMOS_API_BASE"

# Each row is "name|adapter-dir|env-var-required". Empty env-var means always run.
ADAPTERS=(
    "openai|/opt/mnemos-bridges/mnemos-bridge-openai|OPENAI_API_KEY"
    "anthropic|/opt/mnemos-bridges/mnemos-bridge-anthropic|ANTHROPIC_API_KEY"
    "gemini|/opt/mnemos-bridges/mnemos-bridge-gemini|GOOGLE_API_KEY"
)

declare -a results
overall_status="pass"

for spec in "${ADAPTERS[@]}"; do
    IFS='|' read -r name dir env_var <<<"$spec"
    if [ -n "$env_var" ] && [ -z "${!env_var}" ]; then
        echo "[$(date -uIseconds)] $name: SKIPPED (no $env_var)"
        results+=("{\"bridge\":\"$name\",\"status\":\"skipped\",\"reason\":\"no_$env_var\"}")
        continue
    fi
    if [ ! -d "$dir" ]; then
        echo "[$(date -uIseconds)] $name: SKIPPED (no $dir)"
        results+=("{\"bridge\":\"$name\",\"status\":\"skipped\",\"reason\":\"no_dir\"}")
        continue
    fi

    echo
    echo "[$(date -uIseconds)] $name: starting"
    t0=$(date +%s)
    if (cd "$dir" && "$VENV_PYTHON" -m pytest tests/integration -q --tb=line) >>"$LOG" 2>&1; then
        dt=$(($(date +%s) - t0))
        echo "[$(date -uIseconds)] $name: PASS in ${dt}s"
        results+=("{\"bridge\":\"$name\",\"status\":\"pass\",\"duration_s\":$dt}")
    else
        dt=$(($(date +%s) - t0))
        echo "[$(date -uIseconds)] $name: FAIL in ${dt}s"
        results+=("{\"bridge\":\"$name\",\"status\":\"fail\",\"duration_s\":$dt}")
        overall_status="fail"
    fi
done

# Compose the summary memory
results_json=$(printf '%s,' "${results[@]}" | sed 's/,$//')
content="Nightly bridge tier-2 summary $DATE — overall=$overall_status. Per-bridge: [$results_json]"

if [ -z "$MNEMOS_API_TOKEN" ]; then
    echo "[$(date -uIseconds)] no MNEMOS_API_TOKEN; skipping memory write"
    exit 0
fi

memory_payload=$(python3 -c "
import json, sys
print(json.dumps({
    'content': '''$content''',
    'category': 'infrastructure',
    'subcategory': 'bridge-tier2',
    'metadata': {
        'date': '$DATE',
        'overall': '$overall_status',
        'results': json.loads('[$results_json]')
    }
}))
")

echo "[$(date -uIseconds)] writing summary memory"
curl -sS -X POST \
    -H "Authorization: Bearer $MNEMOS_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$memory_payload" \
    "$MNEMOS_API_BASE/v1/memories" >/tmp/bridge-tier2-${DATE}-write.json
echo "[$(date -uIseconds)] write response: $(cat /tmp/bridge-tier2-${DATE}-write.json | head -c 100)"

echo
echo "[$(date -uIseconds)] bridge-tier2-nightly done — overall=$overall_status"
[ "$overall_status" = "pass" ] && exit 0 || exit 1
