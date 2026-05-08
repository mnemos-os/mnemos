"""MNEMOS agentic installer — LLM-guided installation conversation.

Tries backends in order: GRAEAE → Ollama → Anthropic API → None (wizard fallback).
Uses only stdlib (urllib, json, os, sys, socket).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SystemInfo:
    """Detected environment snapshot."""
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""
    hostname: str = ""
    postgres_running: bool = False
    postgres_version: str = ""
    disk_free_gb: float = 0.0
    has_sudo: bool = False
    graeae_reachable: bool = False
    ollama_reachable: bool = False
    ollama_models: list[str] = field(default_factory=list)
    anthropic_key_set: bool = False

    def to_text(self) -> str:
        """Render to human-readable paragraph."""
        lines = [
            f"Host: {self.hostname}",
            f"OS: {self.os_name} {self.os_version}",
            f"Python: {self.python_version}",
            f"Disk free: {self.disk_free_gb:.1f} GB",
            f"Sudo: {'available' if self.has_sudo else 'not available'}",
            f"PostgreSQL: {'running' + (' (' + self.postgres_version + ')' if self.postgres_version else '') if self.postgres_running else 'not detected'}",
            f"GRAEAE: {'reachable' if self.graeae_reachable else 'not reachable'}",
            f"Ollama: {'running, models: ' + ', '.join(self.ollama_models[:5]) if self.ollama_reachable else 'not running'}",
            f"Anthropic API key: {'set' if self.anthropic_key_set else 'not set'}",
        ]
        return "\n".join(lines)


@dataclass
class Config:
    """Installation configuration collected from the conversation."""
    profile: str = "personal"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "mnemos"
    db_user: str = "mnemos_user"
    db_password: str = ""
    listen_port: int = 5002
    service_user: str = "mnemos"
    auth_enabled: bool = False
    rls_enabled: bool = False
    graeae_providers: dict[str, Any] = field(default_factory=dict)
    inference_embed_host: str = "http://localhost:11434"
    install_docling: bool = False
    create_service: bool = True
    embedding_dim: int = 768         # vec0/embedding dimension; honors MNEMOS_EMBEDDING_DIM


# ── Environment detection ─────────────────────────────────────────────────────

def detect_environment() -> SystemInfo:
    """Probe the local system and return a SystemInfo snapshot."""
    info = SystemInfo()

    # Basic system
    info.hostname = socket.gethostname()
    info.os_name = platform.system()
    info.os_version = platform.version()
    info.python_version = sys.version.split()[0]

    # Disk free on /
    try:
        stat = shutil.disk_usage("/")
        info.disk_free_gb = stat.free / (1024 ** 3)
    except Exception:
        info.disk_free_gb = 0.0

    # Sudo
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=3,
        )
        info.has_sudo = result.returncode == 0
    except Exception:
        info.has_sudo = False

    # PostgreSQL
    try:
        result = subprocess.run(
            ["pg_isready", "-q"],
            capture_output=True,
            timeout=5,
        )
        info.postgres_running = result.returncode == 0
        if info.postgres_running:
            ver_result = subprocess.run(
                ["psql", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ver_result.returncode == 0:
                info.postgres_version = ver_result.stdout.strip().split()[-1]
    except Exception:
        info.postgres_running = False

    # GRAEAE reachability
    try:
        graeae_url = os.environ.get("MNEMOS_GRAEAE_URL", "http://localhost:5002")
        req = urllib.request.Request(
            f"{graeae_url}/health",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3):
            info.graeae_reachable = True
    except Exception:
        info.graeae_reachable = False

    # Ollama reachability + models
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/tags",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            info.ollama_reachable = True
            info.ollama_models = models
    except Exception:
        info.ollama_reachable = False

    # Anthropic API key
    info.anthropic_key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    return info


# ── Agent installer ───────────────────────────────────────────────────────────

class AgentInstaller:
    """LLM-backed conversational installer for MNEMOS."""

    MAX_TURNS = 10

    def __init__(self, info: SystemInfo) -> None:
        self.info = info
        self.backend: str | None = self._detect_backend()
        self.history: list[dict] = []
        self.config: Config | None = None
        self._ollama_model: str = self._pick_ollama_model()

    # ── Backend detection ─────────────────────────────────────────────────────

    GRAEAE_URL: str = os.environ.get(
        "MNEMOS_GRAEAE_URL", "http://localhost:5002"
    )

    def _detect_backend(self) -> str | None:
        if self.info.graeae_reachable:
            return "graeae"
        if self.info.ollama_reachable and self.info.ollama_models:
            return "ollama"
        if self.info.anthropic_key_set:
            return "anthropic"
        return None

    def _pick_ollama_model(self) -> str:
        """Pick the best available Ollama model, preferring llama/gemma."""
        if not self.info.ollama_models:
            return "llama3"
        for model in self.info.ollama_models:
            name = model.lower()
            if "llama" in name or "gemma" in name:
                return model
        return self.info.ollama_models[0]

    # ── System prompt ─────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        env_summary = self.info.to_text()
        return (
            "You are an AI installer assistant for MNEMOS, a FastAPI + PostgreSQL "
            "memory and knowledge system with a GRAEAE multi-provider LLM consensus "
            "engine. Your job is to guide the user through installation by asking "
            "clear, friendly questions. Collect: profile (personal/team/enterprise), "
            "db_name, db_user, db_password, listen_port (default 5002), and "
            "optionally: db_host, db_port, service_user, auth_enabled, rls_enabled, "
            "inference_embed_host, install_docling, create_service. "
            "Ask one or two questions at a time. When you have all required values "
            "(profile, db_name, db_user, db_password, listen_port), output ONLY a "
            "JSON block wrapped in ```json ... ``` with all collected Config fields. "
            "Required JSON keys: profile, db_name, db_user, db_password, listen_port. "
            "Do not output the JSON block until you have confirmed all required fields.\n\n"
            f"Detected environment:\n{env_summary}"
        )

    # ── LLM backends ─────────────────────────────────────────────────────────

    def _llm(self, prompt: str, system: str | None = None) -> str:
        """Send a prompt to the active backend, return response text."""
        if self.backend == "graeae":
            return self._llm_graeae(prompt, system)
        if self.backend == "ollama":
            return self._llm_ollama(prompt, system)
        if self.backend == "anthropic":
            return self._llm_anthropic(prompt, system)
        return ""

    def _llm_graeae(self, prompt: str, system: str | None = None) -> str:
        """Call GRAEAE consensus engine."""
        # Build a context-enriched prompt that includes conversation history
        context = ""
        if self.history:
            context = "\n".join(
                f"{msg['role'].upper()}: {msg['content']}"
                for msg in self.history[-6:]  # last 6 turns for context
            )
            prompt = f"[Conversation so far]\n{context}\n\n[New user message]\n{prompt}"
        if system:
            prompt = f"[System]\n{system}\n\n{prompt}"

        payload = {"prompt": prompt, "task_type": "reasoning"}
        try:
            req = urllib.request.Request(
                f"{self.GRAEAE_URL}/graeae/consult",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            all_resp = data.get("all_responses", {})
            if not all_resp:
                # Try flat response_text field
                return data.get("response_text", "")
            best = max(
                all_resp.values(),
                key=lambda x: x.get("final_score", 0),
                default={},
            )
            return best.get("response_text", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"\n[AGENT] GRAEAE timeout/error ({exc}), trying fallback...")
            # Try to fall back to ollama or anthropic
            if self.info.ollama_reachable and self.info.ollama_models:
                self.backend = "ollama"
                return self._llm_ollama(prompt, system)
            if self.info.anthropic_key_set:
                self.backend = "anthropic"
                return self._llm_anthropic(prompt, system)
            self.backend = None
            return ""

    def _llm_ollama(self, prompt: str, system: str | None = None) -> str:
        """Call local Ollama /api/chat endpoint."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        # Include recent history
        messages.extend(self.history[-8:])
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self._ollama_model,
            "messages": messages,
            "stream": False,
        }
        try:
            req = urllib.request.Request(
                "http://localhost:11434/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data.get("message", {}).get("content", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"\n[AGENT] Ollama error ({exc}), trying Anthropic fallback...")
            if self.info.anthropic_key_set:
                self.backend = "anthropic"
                return self._llm_anthropic(prompt, system)
            self.backend = None
            return ""

    def _llm_anthropic(self, prompt: str, system: str | None = None) -> str:
        """Call Anthropic Messages API using urllib (no httpx/requests)."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self.backend = None
            return ""

        # Build messages from history + current prompt
        messages: list[dict] = list(self.history[-8:])
        messages.append({"role": "user", "content": prompt})

        model_name = os.environ.get("MNEMOS_INSTALLER_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": 1024,
            "messages": messages,
        }
        if system:
            payload["system"] = system

        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            content = data.get("content", [])
            if content and isinstance(content, list):
                return content[0].get("text", "")
            return ""
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"\n[AGENT] Anthropic API error ({exc})")
            self.backend = None
            return ""

    # ── Config extraction ─────────────────────────────────────────────────────

    def _extract_config(self, text: str) -> Config | None:
        """Parse a ```json ... ``` block from text and return Config, or None."""
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                raw = json.loads(match.group(1))
                cfg = Config()
                if "profile" in raw:
                    cfg.profile = str(raw["profile"])
                if "db_host" in raw:
                    cfg.db_host = str(raw["db_host"])
                if "db_port" in raw:
                    cfg.db_port = int(raw["db_port"])
                if "db_name" in raw:
                    cfg.db_name = str(raw["db_name"])
                if "db_user" in raw:
                    cfg.db_user = str(raw["db_user"])
                if "db_password" in raw:
                    cfg.db_password = str(raw["db_password"])
                if "listen_port" in raw:
                    cfg.listen_port = int(raw["listen_port"])
                if "service_user" in raw:
                    cfg.service_user = str(raw["service_user"])
                if "auth_enabled" in raw:
                    cfg.auth_enabled = bool(raw["auth_enabled"])
                if "rls_enabled" in raw:
                    cfg.rls_enabled = bool(raw["rls_enabled"])
                if "graeae_providers" in raw:
                    cfg.graeae_providers = dict(raw["graeae_providers"])
                if "inference_embed_host" in raw:
                    cfg.inference_embed_host = str(raw["inference_embed_host"])
                if "install_docling" in raw:
                    cfg.install_docling = bool(raw["install_docling"])
                if "create_service" in raw:
                    cfg.create_service = bool(raw["create_service"])
                # Validate safety-critical fields before returning
                _safe_id = re.compile(r'[A-Za-z_][A-Za-z0-9_\-]{0,62}')
                for field_name, val in [
                    ("db_user", cfg.db_user),
                    ("db_name", cfg.db_name),
                    ("service_user", cfg.service_user),
                ]:
                    if not _safe_id.fullmatch(val):
                        print(
                            f"[AGENT] LLM returned unsafe {field_name}='{val}' — "
                            "falling back to wizard.",
                            file=sys.stderr,
                        )
                        return None
                # Validate port range
                if not (1024 <= cfg.listen_port <= 65535):
                    print(
                        f"[AGENT] LLM returned unsafe listen_port={cfg.listen_port} — "
                        "falling back to wizard.",
                        file=sys.stderr,
                    )
                    return None
                # Validate db_host: allow hostname, IPv4, localhost
                _safe_host = re.compile(
                    r'localhost|127\.0\.0\.1|'
                    r'[A-Za-z0-9][A-Za-z0-9\-\.]{0,253}'
                )
                if not _safe_host.fullmatch(cfg.db_host):
                    print(
                        f"[AGENT] LLM returned unsafe db_host='{cfg.db_host}' — "
                        "falling back to wizard.",
                        file=sys.stderr,
                    )
                    return None
                return cfg
            except (json.JSONDecodeError, ValueError, TypeError):
                return None
        return None

    def _heuristic_extract(self, text: str) -> Config | None:
        """Fallback: extract config values from plain conversational text."""
        text_lower = text.lower()
        cfg = Config()
        changed = False

        # Profile
        if any(w in text_lower for w in ["personal use", "just me", "personal"]):
            cfg.profile = "personal"
            changed = True
        elif "enterprise" in text_lower:
            cfg.profile = "enterprise"
            changed = True
        elif "team" in text_lower:
            cfg.profile = "team"
            changed = True

        # DB name
        match = re.search(r"\bdb(?:_?name)?\s*[=:]\s*(\w+)", text_lower)
        if match:
            cfg.db_name = match.group(1)
            changed = True
        elif "mnemos" in text_lower:
            cfg.db_name = "mnemos"
            changed = True

        # DB user
        match = re.search(r"\b(?:db_?user|username)\s*[=:]\s*(\w+)", text_lower)
        if match:
            cfg.db_user = match.group(1)
            changed = True

        # Port (1024-65535, not a year like 2024)
        port_matches = re.findall(r"\b((?:50\d\d|80\d\d|443\d|[1-9]\d{3,4}))\b", text)
        for candidate in port_matches:
            p = int(candidate)
            if 1024 <= p <= 65535 and p not in (2024, 2025, 2026):
                cfg.listen_port = p
                changed = True
                break

        return cfg if changed else None

    # ── Main conversational loop ──────────────────────────────────────────────

    def run(self) -> Config | None:
        """
        Main conversational loop.

        Returns Config if successfully collected, or None to fall back to wizard.
        """
        if self.backend is None:
            return None

        print(f"\n[AGENT] Using LLM backend: {self.backend}")
        print("[AGENT] Type 'manual' or 'exit' at any time to fall back to wizard mode.\n")

        system_prompt = self._build_system_prompt()
        env_summary = self.info.to_text()

        # Opening message from agent
        opening = (
            f"I've detected the following environment:\n\n{env_summary}\n\n"
            "I can guide you through installing MNEMOS. What would you like to do? "
            "You can describe your use case in plain English — "
            "for example: 'I want a personal memory server' or "
            "'set up a team installation with authentication'."
        )
        print(f"[AGENT] {opening}\n")

        # Seed history with the opening
        self.history.append({"role": "assistant", "content": opening})

        turn = 0
        forced_extraction = False

        while turn < self.MAX_TURNS:
            # --- User input ---
            try:
                user_input = input("[YOU]   ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[AGENT] Cancelled.")
                return None

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "manual"):
                print("[AGENT] Falling back to wizard mode.")
                return None

            self.history.append({"role": "user", "content": user_input})
            turn += 1

            # Force config extraction on last turn
            if turn >= self.MAX_TURNS - 1 and not forced_extraction:
                forced_extraction = True
                user_input = (
                    user_input + "\n\n[System: Maximum turns reached. "
                    "Please output the JSON config block now with all collected values.]"
                )

            # --- Thinking indicator ---
            print("[AGENT] thinking...", end="\r", flush=True)

            # --- LLM call ---
            response = self._llm(user_input, system=system_prompt)

            # Clear thinking indicator
            print("                    ", end="\r", flush=True)

            if not response:
                # Backend failed entirely
                print("[AGENT] LLM backend unavailable. Falling back to wizard mode.")
                return None

            self.history.append({"role": "assistant", "content": response})

            # --- Print response ---
            print(f"[AGENT] {response}\n")

            # --- Try to extract config from this response ---
            cfg = self._extract_config(response)
            if cfg is not None:
                return self._confirm_config(cfg)

            # --- Heuristic fallback after several turns ---
            if turn >= 5:
                # Aggregate all text from conversation for heuristics
                all_text = " ".join(
                    msg["content"]
                    for msg in self.history
                    if msg["role"] == "user"
                )
                heuristic_cfg = self._heuristic_extract(all_text)
                if heuristic_cfg is not None and turn >= self.MAX_TURNS - 2:
                    print(
                        "[AGENT] Extracting config from conversation using heuristics...\n"
                    )
                    return self._confirm_config(heuristic_cfg)

        # Max turns exhausted without a JSON block
        print("[AGENT] Could not extract a complete configuration. Falling back to wizard.")
        return None

    def _confirm_config(self, cfg: Config) -> Config | None:
        """Show summary and ask for confirmation. Returns cfg or None."""
        print("\n" + "=" * 60)
        print("  Here's what I'll install:")
        print("=" * 60)
        print(f"  Profile:       {cfg.profile}")
        print(f"  Database:      {cfg.db_user}@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")
        print(f"  Password:      {'(set)' if cfg.db_password else '(empty)'}")
        print(f"  Listen port:   {cfg.listen_port}")
        print(f"  Service user:  {cfg.service_user}")
        print(f"  Auth enabled:  {cfg.auth_enabled}")
        print(f"  RLS enabled:   {cfg.rls_enabled}")
        print(f"  Create service:{cfg.create_service}")
        if cfg.graeae_providers:
            print(f"  GRAEAE providers: {', '.join(cfg.graeae_providers.keys())}")
        print("=" * 60)

        for _ in range(3):  # bounded — avoid unbounded recursion
            try:
                answer = input("\nProceed? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if answer in ("", "y", "yes"):
                return cfg
            if answer in ("n", "no"):
                print("[AGENT] Installation cancelled.")
                return None
            print("[AGENT] Please enter Y or n.")
        print("[AGENT] No valid response after 3 attempts — cancelling.")
        return None


# ── Module-level helper ───────────────────────────────────────────────────────

def run_agent_installer() -> Config | None:
    """Entry point: detect environment, run agent, return Config or None."""
    print("[AGENT] Detecting environment...")
    info = detect_environment()
    installer = AgentInstaller(info)
    return installer.run()


# ── Standalone execution ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = run_agent_installer()
    if cfg is None:
        print("\n[INFO] No config produced — falling back to wizard mode.")
        sys.exit(1)
    print("\n[INFO] Config collected successfully.")
    sys.exit(0)
