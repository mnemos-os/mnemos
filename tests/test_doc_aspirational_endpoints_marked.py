"""Slice #198: pin the historical-note callouts on aspirational
endpoint references that are NOT live REST routes.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- ``docs/DREAM_STATE_DESIGN.md`` describes
  ``/v1/dreams/{version_id}/promote``,
  ``/v1/dreams/{version_id}/acknowledge``, and
  ``/admin/dreams/run`` — none of those exist. The shipped
  MORPHEUS subsystem uses ``/v1/morpheus/runs*`` and
  ``/admin/morpheus/runs`` (`mnemos/api/routes/morpheus.py`).
- ``docs/connectors/chatgpt-pro-developer-mode.md`` and
  ``docs/connectors/README.md`` reference an experimental
  ``mnemos-tunnel-setup`` helper that calls daemon-side
  ``/admin/tunnels/*`` endpoints. As of v5.3.2 the
  ``mnemos.tunnels.ngrok_bridge`` module + the daemon-side
  endpoints are NOT implemented; the script is aspirational
  contract.

This test does NOT remove the design content — it pins that the
historical-note callouts stay near the aspirational endpoint
names so future readers don't grep for non-existent routes
without finding the explanation immediately.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_dream_endpoints_marked_did_not_ship():
    """`docs/DREAM_STATE_DESIGN.md` must keep the "did not ship
    as designed" callout near the section that introduces
    `/v1/dreams/*` and `/admin/dreams/run` endpoint names."""
    src = (REPO / "docs" / "DREAM_STATE_DESIGN.md").read_text()
    # Only enforce if the design names still appear (someone
    # could rewrite the section to drop them — that's also fine).
    if "/v1/dreams/" not in src and "/admin/dreams/" not in src:
        return
    assert "did not ship as designed" in src, (
        "docs/DREAM_STATE_DESIGN.md still references "
        "`/v1/dreams/*` or `/admin/dreams/run` but lost the "
        "historical-note callout. Add a note pointing readers at "
        "the live `mnemos/api/routes/morpheus.py` routes."
    )
    assert "/admin/morpheus/runs" in src, (
        "docs/DREAM_STATE_DESIGN.md historical-note callout no "
        "longer names the live `/admin/morpheus/runs` endpoint."
    )


def test_admin_tunnels_marked_inert_in_chatgpt_doc():
    """`docs/connectors/chatgpt-pro-developer-mode.md` must keep
    a clear "currently inert" / "not implemented" warning near
    the `/admin/tunnels/*` reference, since the daemon-side
    endpoints + the ngrok-bridge module don't exist."""
    src = (REPO / "docs" / "connectors"
           / "chatgpt-pro-developer-mode.md").read_text()
    if "/admin/tunnels/" not in src and "mnemos-tunnel-setup" not in src:
        return
    has_warning = (
        "currently inert" in src
        or "not implemented" in src
        or "has not shipped" in src
        or "neither has shipped" in src
    )
    assert has_warning, (
        "chatgpt-pro-developer-mode.md still describes the "
        "mnemos-tunnel-setup helper / `/admin/tunnels/*` but "
        "lost the inert/not-implemented warning. Operators copy-"
        "pasting from this doc would hit a broken contract."
    )


def test_admin_tunnels_marked_not_implemented_in_connectors_readme():
    """`docs/connectors/README.md` stability commitments must
    note that `/admin/tunnels/*` is not implemented yet."""
    src = (REPO / "docs" / "connectors" / "README.md").read_text()
    if "/admin/tunnels/" not in src:
        return
    assert "not implemented" in src, (
        "docs/connectors/README.md describes "
        "`/admin/tunnels/*` without the not-implemented "
        "warning. Surface that the assisted-tunnel path is "
        "aspirational so operators don't expect it to work."
    )


def test_tunnel_script_refuses_without_force_flag():
    """Codex round-1 of #198 caught that the
    `scripts/mnemos_tunnel_setup.py` script presented itself as
    live, so a user could run it and hit /admin/tunnels/start
    (404) before reading the docs. The script is now gated
    behind a `--force` flag and prints an "inert" warning. Pin
    the gate's presence."""
    src = (REPO / "scripts" / "mnemos_tunnel_setup.py").read_text()
    assert 'if "--force" not in sys.argv' in src \
        or '"--force"' in src, (
        "scripts/mnemos_tunnel_setup.py no longer gates execution "
        "behind --force. Until /admin/tunnels/* + "
        "mnemos.tunnels.ngrok_bridge ship, the script must refuse "
        "to run by default so users don't hit a non-existent "
        "endpoint."
    )
    # Pin the warning text so tone of voice doesn't drift.
    assert "currently inert" in src, (
        "The 'currently inert' warning in mnemos_tunnel_setup.py "
        "is gone — without it, --force would silently mask the "
        "missing-endpoint state."
    )


def test_morpheus_routes_actually_exist():
    """Sanity-check: pin that the live morpheus routes named in
    the dream-state historical-note callout actually exist."""
    morpheus = (REPO / "mnemos" / "api" / "routes"
                / "morpheus.py").read_text()
    assert '"/admin/morpheus/runs"' in morpheus, (
        "/admin/morpheus/runs route disappeared from "
        "mnemos/api/routes/morpheus.py. Update the dream-state "
        "historical-note callout."
    )
    assert '"/v1/morpheus/runs"' in morpheus, (
        "/v1/morpheus/runs route disappeared. Update the "
        "dream-state historical-note callout."
    )
