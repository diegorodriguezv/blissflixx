"""
The frontend's control surface must match the server's.

Every action the UI can send has to exist in the backend key map, and every
action the server accepts has to be reachable from the UI. A mismatch in either
direction means a button that does nothing, or a control nobody can use.

This was originally recorded as a finding — "14 of 17 actions unreachable" — on
the strength of a grep that missed html/tags/playbar.html's remote panel.
Parsing the markup properly shows the two sets match exactly. The test exists so
that claim cannot rot back into a false one.
"""

import ast
import inspect
import pathlib
import re
import textwrap

from lib.player.omxproc2 import OmxplayerProcess2

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAYBAR = REPO_ROOT / "html" / "tags" / "playbar.html"

# Action names the key backend's control() compares against. Parsed rather than
# grepped so mpv/VLC property names cannot be mistaken for actions.
_control_source = textwrap.dedent(inspect.getsource(OmxplayerProcess2.control))
_control_tree = ast.parse(_control_source)
SERVER_ACTIONS = {
    comparator.value
    for node in ast.walk(_control_tree)
    if isinstance(node, ast.Compare)
    and isinstance(node.left, ast.Name)
    and node.left.id == "action"
    for comparator in node.comparators
    if isinstance(comparator, ast.Constant)
}


def ui_actions():
    """Every action name the playbar sends, from both call shapes it uses."""
    html = PLAYBAR.read_text()
    sent = set(re.findall(r"doAction\('([a-z0-9_]+)'\)", html))
    sent |= set(re.findall(r"self\.control\('([a-z0-9_]+)'\)", html))
    return sent


class TestControlSurfaceMatches:
    def test_playbar_exists(self):
        assert PLAYBAR.is_file(), PLAYBAR

    def test_every_ui_action_is_implemented(self):
        assert ui_actions() - SERVER_ACTIONS == set()

    def test_every_server_action_is_reachable(self):
        """
        The regression guard for the original finding. If this fails, a control
        exists in the key map with no button behind it.
        """
        assert SERVER_ACTIONS - ui_actions() == set()

    def test_the_sets_are_the_expected_seventeen(self):
        assert len(SERVER_ACTIONS) == 17

    def test_the_remote_panel_is_where_they_live(self):
        """
        Sanity check on the original mistake: most of these are sent from the
        remote panel via doAction, not from the three top-level buttons.
        """
        html = PLAYBAR.read_text()
        assert "remoteModal" in html
        assert len(set(re.findall(r"doAction\('([a-z0-9_]+)'\)", html))) > 10


class TestBackendCapabilityReporting:
    """
    The UI renders all 17 buttons whatever backend is active, so a backend that
    implements fewer has to declare that, or actions are dropped silently.
    """

    def test_key_backend_declares_every_capability(self):
        from lib.player.backend import ALL_CAPABILITIES

        assert OmxplayerProcess2().supports(*ALL_CAPABILITIES)

    def test_ui_has_more_actions_than_there_are_capabilities(self):
        """
        Capability flags are coarser than actions: one 'seek' capability covers
        four seek actions. This records that the two vocabularies are not
        interchangeable, so nobody tries to compare them directly later.
        """
        from lib.player.backend import ALL_CAPABILITIES

        assert len(SERVER_ACTIONS) > len(ALL_CAPABILITIES)
        assert len(ALL_CAPABILITIES) == 6
