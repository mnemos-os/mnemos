#!/usr/bin/env python3
"""mnemos-tunnel-setup — interactive helper for connecting MNEMOS to
ChatGPT Pro Developer Mode, Claude Desktop, Cursor, or Codex CLI.

⚠️ ASPIRATIONAL HELPER — currently inert as of v5.0.1. The
daemon-side ``/admin/tunnels/*`` REST surface and the
``mnemos.tunnels.ngrok_bridge`` module that this script depends on
have not shipped yet. Running the script today will fail at the
``/admin/tunnels/start`` HTTP call. Use the manual `mnemos serve
mcp-http` + ngrok path documented per-agent under
``docs/connectors/`` until the daemon-side endpoints land. The
docstring + script body are preserved as the contract these
endpoints should implement when they do ship.

Designed flow (when the contract ships):

  1. Confirm MNEMOS is reachable.
  2. If no ngrok authtoken on file: open the signup page in their
     browser and prompt for paste.
  3. Open the tunnel via the MNEMOS admin API.
  4. Pretty-print connector-ready snippets for each agent surface.
     Copy the relevant one to the system clipboard if available.

The "easy button" for end users who don't want to know what ngrok
or SSE or bearer auth means.

Designed to be runnable as:
    python3 scripts/mnemos_tunnel_setup.py
or installed via pyproject as a console script:
    mnemos-tunnel-setup [chatgpt | claude | cursor | codex | all]

Depends only on stdlib + httpx (already a MNEMOS runtime dep). No
ngrok-python SDK needed in this script — the SDK lives inside the
(planned) MNEMOS daemon's `mnemos.tunnels.ngrok_bridge` module;
this script calls the admin API once that module + the
``/admin/tunnels/*`` routes are wired in.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import webbrowser
from pathlib import Path
from typing import Optional

import httpx

CONFIG_DIR = Path.home() / ".mnemos"
CONFIG_FILE = CONFIG_DIR / "tunnel.toml"
NGROK_SIGNUP_URL = "https://dashboard.ngrok.com/signup"
NGROK_AUTHTOKEN_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"

DEFAULT_MNEMOS_BASE = os.getenv("MNEMOS_BASE", "http://localhost:5002")


def _say(msg: str) -> None:
    """User-facing print. Matches the `mnemos` CLI's output style."""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _err(msg: str) -> None:
    sys.stderr.write(f"error: {msg}\n")
    sys.stderr.flush()


def _prompt(label: str) -> str:
    sys.stdout.write(f"{label}: ")
    sys.stdout.flush()
    return input().strip()


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard set. Returns True if it worked.
    Mac / Linux / Windows — tries the right tool per platform.
    """
    candidates = []
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("linux"):
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except Exception:
                continue
    return False


def _check_mnemos_reachable(base: str, api_key: str) -> bool:
    try:
        r = httpx.get(f"{base}/health", timeout=5)
        if r.status_code == 200:
            return True
        _err(f"MNEMOS at {base}/health returned HTTP {r.status_code}")
        return False
    except httpx.HTTPError as exc:
        _err(f"could not reach MNEMOS at {base}: {exc}")
        return False


def _load_authtoken() -> Optional[str]:
    if not CONFIG_FILE.exists():
        return None
    # Tiny TOML reader — avoid pulling in tomli for a 1-key file.
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("authtoken"):
            _, _, value = line.partition("=")
            return value.strip().strip('"').strip("'")
    return None


def _save_authtoken(token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f'authtoken = "{token}"\n')
    # 0600 — secret, owner-readable only
    os.chmod(CONFIG_FILE, 0o600)


def _ensure_authtoken_or_walk_signup() -> str:
    """Returns the configured ngrok authtoken; prompts the user
    through signup + paste if none is found.
    """
    token = _load_authtoken()
    if token:
        _say(f"✓ Using ngrok authtoken from {CONFIG_FILE}")
        return token

    _say(textwrap.dedent(f"""
    First-time setup — connecting MNEMOS to a public URL via ngrok.

    1. I'll open ngrok's signup page in your browser. Free tier is
       fine for testing; the paid tier ($10/mo) gives you a stable
       subdomain that doesn't change on every restart.

    2. After signup, ngrok will show you an authtoken on the
       "Your Authtoken" page. Copy it.

    3. Come back here and paste it when prompted.

    Press Enter to continue (or Ctrl-C to abort).
    """).strip())
    input()

    try:
        webbrowser.open(NGROK_SIGNUP_URL)
        _say(f"  opened {NGROK_SIGNUP_URL}")
        _say(f"  if your browser didn't open, paste the URL manually.")
    except Exception:
        _say(f"  please open {NGROK_SIGNUP_URL} in your browser.")

    _say("")
    _say(f"After signup, your authtoken is at:")
    _say(f"  {NGROK_AUTHTOKEN_URL}")
    _say("")

    while True:
        token = _prompt("Paste your ngrok authtoken")
        if len(token) >= 20 and "_" in token:
            break
        _err("That doesn't look like an ngrok authtoken (expected ~40 "
             "chars including an underscore). Try again.")

    _save_authtoken(token)
    _say(f"✓ Saved authtoken to {CONFIG_FILE} (mode 0600)")
    return token


def _start_tunnel(base: str, api_key: str, authtoken: str) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"backend": "ngrok", "authtoken": authtoken, "target_port": 5004}
    try:
        r = httpx.post(f"{base}/admin/tunnels/start",
                       headers=headers, json=body, timeout=30)
    except httpx.HTTPError as exc:
        _err(f"tunnel start failed: {exc}")
        sys.exit(1)
    if r.status_code != 200:
        _err(f"tunnel start failed: HTTP {r.status_code} — {r.text[:200]}")
        sys.exit(1)
    return r.json()


def _emit_connector_block(surface: str, url: str, token: str) -> str:
    """Per-surface paste-ready snippet. Each agent has a different
    config shape; we emit the one the user actually needs."""
    if surface == "chatgpt":
        return textwrap.dedent(f"""
        ─── ChatGPT Pro Developer Mode ───
        Settings → Developer Mode → Connectors → Add custom

          Name:                MNEMOS
          Connector URL:       {url}/sse
          Authentication:      Bearer Token
          Token:               {token}
          Description:         Memory across conversations
        """).strip()
    if surface == "claude":
        return textwrap.dedent(f"""
        ─── Claude Desktop ───
        Settings → Developer → Edit Config

          Add to mcpServers:
          {{
            "mnemos-remote": {{
              "url": "{url}/sse",
              "transport": "sse",
              "headers": {{
                "Authorization": "Bearer {token}"
              }}
            }}
          }}
        """).strip()
    if surface == "cursor":
        return textwrap.dedent(f"""
        ─── Cursor ───
        Settings → MCP → Add new MCP server

          Name:        mnemos
          Type:        sse
          URL:         {url}/sse
          Headers:     Authorization: Bearer {token}
        """).strip()
    if surface == "codex":
        return textwrap.dedent(f"""
        ─── Codex CLI (OpenAI) ───
        codex mcp add mnemos --transport sse \\
          --url {url}/sse \\
          --header "Authorization: Bearer {token}"
        """).strip()
    return ""


def _list_surfaces(arg: str) -> list[str]:
    if arg == "all":
        return ["chatgpt", "claude", "cursor", "codex"]
    return [arg]


def main() -> int:
    _err(
        "⚠️  mnemos-tunnel-setup is currently inert as of v5.0.1.\n"
        "    The daemon-side /admin/tunnels/* REST routes and the\n"
        "    `mnemos.tunnels.ngrok_bridge` module this script depends\n"
        "    on have not shipped yet. The next /admin/tunnels/start\n"
        "    HTTP call below will return 404. Use the manual\n"
        "    `mnemos serve mcp-http` + ngrok path documented in\n"
        "    docs/connectors/<agent>.md until the daemon endpoints\n"
        "    land. Continuing with --force; pass nothing to abort."
    )
    if "--force" not in sys.argv:
        _err("    Aborting. Re-run with --force if you want to try anyway.")
        return 2

    p = argparse.ArgumentParser(
        prog="mnemos-tunnel-setup",
        description="Connect MNEMOS to a public URL for ChatGPT, Claude, Cursor, or Codex.",
    )
    p.add_argument("--force", action="store_true",
                   help="Acknowledge that the daemon-side tunnel API "
                        "is not implemented yet and run anyway.")
    p.add_argument("surface", nargs="?", default="all",
                   choices=["chatgpt", "claude", "cursor", "codex", "all"],
                   help="Which agent surface to emit connector config for "
                        "(default: all).")
    p.add_argument("--mnemos", default=DEFAULT_MNEMOS_BASE,
                   help=f"MNEMOS base URL (default: {DEFAULT_MNEMOS_BASE}).")
    p.add_argument("--api-key", default=os.getenv("MNEMOS_API_KEY", ""),
                   help="MNEMOS bearer token (or set MNEMOS_API_KEY env).")
    p.add_argument("--no-clipboard", action="store_true",
                   help="Don't copy the ChatGPT URL+token to system clipboard.")
    args = p.parse_args()

    if not args.api_key:
        _err("MNEMOS_API_KEY not set. Pass --api-key or export the env var.")
        return 2

    _say(f"MNEMOS at: {args.mnemos}")
    if not _check_mnemos_reachable(args.mnemos, args.api_key):
        return 1
    _say("✓ MNEMOS is reachable")
    _say("")

    authtoken = _ensure_authtoken_or_walk_signup()
    _say("")
    _say("Opening ngrok tunnel...")

    result = _start_tunnel(args.mnemos, args.api_key, authtoken)
    url = result.get("url")
    token = result.get("token")
    if not url or not token:
        _err(f"tunnel API response missing url/token: {result}")
        return 1

    _say(f"✓ Tunnel open: {url}")
    _say("")

    for surface in _list_surfaces(args.surface):
        block = _emit_connector_block(surface, url, token)
        _say(block)
        _say("")

    # Copy ChatGPT's URL+token block specifically — that's the one
    # users paste into a web form most often. Other surfaces want
    # the user to edit a JSON config file or run a CLI, where
    # clipboard isn't the right primitive.
    if not args.no_clipboard and args.surface in ("all", "chatgpt"):
        # Just URL + token, not the formatted block — easier to paste
        # into individual fields one at a time.
        clip = f"{url}/sse\n{token}"
        if _copy_to_clipboard(clip):
            _say("(URL + token copied to clipboard.)")
        else:
            _say("(Install pbcopy/wl-copy/xclip/xsel for auto-clipboard.)")

    _say("")
    _say("To close the tunnel: DELETE " + args.mnemos + "/admin/tunnels/stop")
    _say("To check status:    GET    " + args.mnemos + "/admin/tunnels/status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
