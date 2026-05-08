# Release Checklist

This is the operator-facing per-release checklist. Run through every section
before announcing a tagged release as GA. Some sections are automated (CI,
nightly cron) and just need a green-status verification; others are manual
(chat-UI walkthroughs) and need an operator to drive a real client.

The checklist is split into **server-side** (the `mnemos-os/mnemos` repo
itself) and **bridge family** (the `mnemos-bridge-*` repos). Run only the
sections that apply to what's shipping.

---

## Server-side release (`mnemos-os/mnemos` x.y.z)

### Pre-merge

- [ ] `pyproject.toml` `version` and `mnemos/_version.py` `__version__` agree.
- [ ] `CHANGELOG.md` has an entry for the new version under the existing
      house style (`## [x.y.z] — YYYY-MM-DD`, `### Added/Fixed/Removed/...`,
      one bullet per user-visible change).
- [ ] Any new SQL migrations are wired into both loaders:
  - [ ] `mnemos/installer/db.py` (Postgres list)
  - [ ] `mnemos/persistence/sqlite.py` (SQLite list)
  - [ ] SQLite mirror file exists in `db/migrations_sqlite/` if the new
        migration uses Postgres-only features (TIMESTAMPTZ, gen_random_uuid,
        TEXT[]). Mirrors translate per the conventions in v5.2.1.
- [ ] `tests/` + `tests/integration*/` all green locally with both
      `--ignore=tests/integration_nats` and the integration-tier suite
      where applicable.
- [ ] `ruff check .` clean.
- [ ] Author email on all commits in the release range is
      `Jason Perlow <jperlow@gmail.com>` (per `~/.claude/CLAUDE.md`
      directive #2 — never `jperlow@nvidia.com` on public OSS).

### Tag + push

- [ ] `git tag -a vx.y.z -m "vx.y.z: <one-line summary>"`.
- [ ] `git push origin master --tags` (gitlab fires CI first; let it
      finish before the other pushes if a CI green is part of the gate).
- [ ] `git push github master --tags`.
- [ ] `GIT_SSH_COMMAND='sshpass -p "Gumbo@Kona1b" ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no' git push argonas master --tags`.
- [ ] Verify all three remotes converged:
  ```bash
  for r in origin github argonas; do
    case $r in argonas) tip=$(GIT_SSH_COMMAND='sshpass -p "Gumbo@Kona1b" ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no' git ls-remote $r master | awk '{print $1}');;
      *) tip=$(git ls-remote $r master | awk '{print $1}');;
    esac
    printf "  %-8s %s\n" "$r" "${tip:0:12}"
  done
  ```
  All three should show the same SHA as `git rev-parse HEAD`.

### Image build + fleet roll-out

Skip if the release is docs-only.

- [ ] Build the `full-hot` image on PYTHIA from the working-tree checkout:
  ```bash
  rsync -az --delete --exclude='.venv*' --exclude='__pycache__' /tmp/mnemos-work/ jasonperlow@192.168.207.67:/tmp/mnemos-build-x.y.z/
  ssh jasonperlow@192.168.207.67 'cp /tmp/mnemos_hot-0.2.0-cp311-abi3-manylinux_2_34_x86_64.whl /tmp/mnemos-build-x.y.z/ && cd /tmp/mnemos-build-x.y.z && podman build -f Dockerfile.full -t localhost/mnemos-os:x.y.z-full-hot .'
  ```
- [ ] Save + transfer to CERBERUS + PROTEUS:
  ```bash
  ssh jasonperlow@192.168.207.67 'podman save -o /tmp/mnemos-os-x.y.z-full-hot.tar localhost/mnemos-os:x.y.z-full-hot && scp /tmp/mnemos-os-x.y.z-full-hot.tar jasonperlow@192.168.207.96:/tmp/ && scp /tmp/mnemos-os-x.y.z-full-hot.tar jasonperlow@192.168.207.25:/tmp/'
  ```
- [ ] Roll the **canary** (PROTEUS) first:
  - Stop + rename the old container as `_pre<version>` for rollback
  - Start a new container against the same env-file with the new image
  - Verify `/health` returns `version: x.y.z` and `database_connected: true`
  - Check logs for migration apply success (any new SQL should run on first boot)
- [ ] Roll **CERBERUS** (HA standby + federation peer).
- [ ] Roll **PYTHIA primary + MCP sidecar** last.
- [ ] Run smoke checks across the fleet:
  ```bash
  for h in 192.168.207.67 192.168.207.96 192.168.207.25; do
    ssh -n jasonperlow@$h 'curl -s http://localhost:5002/health' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['version'], d['status'], d['database_connected'])"
  done
  ```
- [ ] Cleanup `_pre<version>` containers + transfer tars only after
      24 h soak with no error escalation.

### HA / replication

- [ ] PYTHIA → CERBERUS streaming replication still healthy:
  ```bash
  ssh jasonperlow@192.168.207.67 "podman exec mnemos-v3x-podman_postgres_1 psql -U mnemos_user -d mnemos -c 'SELECT application_name, state, replay_lag FROM pg_stat_replication;'"
  ```
- [ ] If the release added migrations, confirm they replicated to
      CERBERUS automatically:
  ```bash
  ssh jasonperlow@192.168.207.96 'podman exec mnemos-standby psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -c "\dt" | grep <new_table_name>'
  ```

### Bridge tier-2 verification

- [ ] PYTHIA cron `bridge-tier2-nightly.sh` succeeded last night:
  ```bash
  ssh jasonperlow@192.168.207.67 'tail -20 /tmp/bridge-tier2-$(date -u +%Y-%m-%d).log'
  ```
- [ ] Or run it on demand: `ssh jasonperlow@192.168.207.67 '/usr/local/bin/bridge-tier2-nightly.sh'`.
      All three target APIs should pass — if any fail, the bridge or the
      target SDK has drifted and needs a fix before announcing GA.

---

## Bridge family release (`mnemos-bridge-*` x.y.z)

Use this when releasing one of the per-surface adapter packages or the
shared `mnemos-bridge-core`.

### Pre-merge

- [ ] Bumped version in `pyproject.toml`.
- [ ] CHANGELOG entry for the new version.
- [ ] Tier-1 (offline) tests green: `pytest tests/ -q --ignore=tests/integration`.
- [ ] If shipping `mnemos-bridge-core`: re-run all SIX downstream adapter
      tier-1 tests against the updated core to confirm no API drift broke
      anything. Each adapter is its own git tree; the simplest route is
      `pip install -e /tmp/mnemos-bridge-core /tmp/mnemos-bridge-{openai,gemini,anthropic,aider,crewai,claude-connector}` then
      `for d in /tmp/mnemos-bridge-{openai,gemini,anthropic,aider,crewai,claude-connector}; do (cd $d && pytest tests/ --ignore=tests/integration -q); done`.

### Tier-2 (live model API)

Skip if the adapter has no live model API target (aider sidecar CLI,
crewai offline-only, claude-connector OAuth — those are tier-3 only).

- [ ] OpenAI: `OPENAI_API_KEY=... MNEMOS_TEST_BASE=http://192.168.207.67:5003/sse MNEMOS_MCP_TOKEN=... pytest tests/integration -v`
- [ ] Gemini: same with `GOOGLE_API_KEY` set.
- [ ] Anthropic: same with `ANTHROPIC_API_KEY` set.
- [ ] Each tier-2 should complete in <15s. If a test is timing out >30s
      the model API is degraded (or the SDK changed shape); investigate
      before tagging.

### Push to 3 remotes

Same pattern as the server-side push, with the per-bridge gitlab/github
namespace `mnemos-os/mnemos-bridge-<name>`. The `/tmp/publish-bridge.sh`
helper script captures the canonical sequence (init + glab create + push +
gh create + push + argonas init + push).

### PYTHIA refresh (if the cron consumes the new version)

The nightly tier-2 cron uses clones at `/opt/mnemos-bridges/`. If a bridge
just released:

- [ ] `ssh jasonperlow@192.168.207.67 'cd /opt/mnemos-bridges/mnemos-bridge-<name> && git pull --quiet && /opt/mnemos-bridges/.venv/bin/pip install --quiet -e .'`
- [ ] Run the cron once manually:
      `ssh jasonperlow@192.168.207.67 '/usr/local/bin/bridge-tier2-nightly.sh'`
- [ ] Confirm the daily summary memory landed in MNEMOS:
      `curl -s -H 'Authorization: Bearer <token>' "http://192.168.207.67:5002/v1/memories/search?subcategory=bridge-tier2&limit=1"`

---

## Tier-3 manual UI walkthroughs (per release, hand-driven)

These cannot be automated — they prove the integration works in the actual
chat UI an operator or end-user lands in. ~5 min per surface; record any
broken flows in the bug tracker before announcing GA.

### Claude Code

- [ ] In a fresh Claude Code session: `python3 ~/.claude/mnemos search 'infrastructure' 5` returns ≥1 result with content.
- [ ] The MCP server `mnemos` shows up in `/mcp` output as a registered server.
- [ ] Asking Claude "search MNEMOS for memories about <recent topic>" produces a tool-use indicator and a coherent answer that references the search hits.

### Claude Desktop

- [ ] `~/Library/Application Support/Claude/claude_desktop_config.json`
      points at the SSH-spawned MCP server.
- [ ] Restart Claude Desktop; ask it the same search query; verify a
      tool-use indicator + correct answer.

### Cursor / Cline / Continue / Codex CLI / Zed

- [ ] Each surface's MCP server registration is intact; the tool list
      includes the canonical 23 MNEMOS tools.
- [ ] One search query in each surface produces a result. Spot-check.

### ChatGPT (Pro Developer Mode)

- [ ] Settings → Connectors → Add → MCP server → URL =
      `https://<your-public-mnemos-mcp-host>:5003/sse` + Bearer token.
- [ ] New chat → "Search MNEMOS for memories about infrastructure".
- [ ] Verify ChatGPT shows "Calling search_memories..." and the final
      response references actual MNEMOS data.

### Gemini AI Studio + custom Python harness

AI Studio web has no "register my MCP server" UI today (early 2026).
The realistic path is the `mnemos-bridge-gemini` v0.2.0 adapter:

- [ ] `pip install mnemos-bridge-gemini` on a dev box.
- [ ] Run the included example script that builds a `genai.Client.aio`
      session with `await adapter.gemini_tools()`.
- [ ] Send a tool-using prompt; verify the function_call lands and the
      adapter dispatches it to PYTHIA.

### Claude.ai Connectors

- [ ] Deploy `mnemos-bridge-claude-connector` behind TLS at a public hostname
      (e.g. `https://mnemos-connector.example.com`).
- [ ] Set `MNEMOS_BACKEND_URL`, `CONNECTOR_JWT_SECRET`,
      `CONNECTOR_PUBLIC_URL` env vars on the host.
- [ ] In Claude.ai → Connectors UI → Add custom connector → URL =
      the hostname above.
- [ ] Walk through OAuth login (paste a pre-issued MNEMOS API key on the
      consent page).
- [ ] Confirm Claude.ai shows the MNEMOS tools in its tool list.
- [ ] Send a search query; verify a tool-use indicator + correct response.

### CrewAI single-agent Crew

- [ ] In a Python script: `pip install mnemos-bridge-crewai crewai` then
      build a single-agent Crew with `tools=await adapter.crewai_tools()`.
- [ ] Kick off a task that should trigger a `search_memories` call.
- [ ] Verify the agent's run log shows the tool was invoked and the
      result was incorporated into the agent's output.

### Aider

- [ ] In a repo, run `mnemos-aider search "<keyword>"`.
- [ ] Paste the output into an Aider prompt and verify Aider takes the
      context into account.
- [ ] If Aider's plugin API is stable enough on the version under test:
      try the Path B integration as well.

### Local runners (Ollama, LM Studio, vLLM)

For each runner that's part of the operator's stack:

- [ ] Configure the runner with `mnemos-bridge-openai` tool definitions
      (the runner is OpenAI-compat).
- [ ] Send a tool-using prompt to the runner.
- [ ] Verify it calls MNEMOS through the bridge and incorporates the
      result.

---

## Post-release announcements

- [ ] Update `gitlab.com/mnemos-os/mnemos/-/releases/<vx.y.z>` with the
      CHANGELOG entry as the release notes (gitlab pulls these from
      tag annotations by default; double-check formatting).
- [ ] Same for `github.com/mnemos-os/mnemos/releases/<vx.y.z>`.
- [ ] If the release introduces a new dimension, surface, or API,
      cross-link it from `README.md` (the "Works with" or "Bridge family"
      section).
- [ ] If the release closes a 🔵 roadmap item, flip it to ✅ in
      `ROADMAP.md` in the same commit.

---

## Where this lives

- This file: `docs/RELEASE_CHECKLIST.md` in the `mnemos-os/mnemos` repo.
- Per-bridge releases follow the same pattern, scoped to that bridge's
  surface — no separate copy needed.
- The fleet helper script for tier-2 cron lives at
  `/usr/local/bin/bridge-tier2-nightly.sh` on PYTHIA. Source of truth
  is `ops/bridge-tier2-nightly.sh` in this repo. Refresh the deployed
  copy with:
  ```bash
  scp ops/bridge-tier2-nightly.sh jasonperlow@192.168.207.67:/tmp/
  ssh jasonperlow@192.168.207.67 'sudo install -m 755 /tmp/bridge-tier2-nightly.sh /usr/local/bin/bridge-tier2-nightly.sh'
  ```
- The image build artifact for the `-full-hot` images (every release
  since v5.0.6) is `Dockerfile.full` at the repo root. It pulls all
  optional extras (morpheus + persephone + pantheon + kronos +
  knossos + apollo + artemis + nats + edge) and the local
  `mnemos_hot` Rust wheel, on top of the base `Dockerfile`. The
  base `Dockerfile` is still the slim/edge image; `Dockerfile.full`
  is what the fleet runs.

*Last updated: 2026-05-05*
