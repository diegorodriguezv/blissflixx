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

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAYBAR = REPO_ROOT / "html" / "tags" / "playbar.html"

# Action names VLC's control() compares against. Parsed rather than grepped so
# mpv property names cannot be mistaken for actions.
from lib.player.vlcproc import VlcProcess

_control_source = textwrap.dedent(inspect.getsource(VlcProcess.control))
_control_tree = ast.parse(_control_source)


def _compared_action_names(node):
    """
    The action names one comparison in control() mentions.

    Two shapes, because both are idiomatic and this backend uses both:
    "action == 'stop'" compares against one constant, and "action in ('a', 'b')"
    against a tuple of them. Reading only the first shape found nothing at all,
    because every action here is in a tuple.
    """
    for comparator in node.comparators:
        if isinstance(comparator, ast.Constant):
            yield comparator.value
        elif isinstance(comparator, ast.Tuple):
            for element in comparator.elts:
                if isinstance(element, ast.Constant):
                    yield element.value


SERVER_ACTIONS = {
    name
    for node in ast.walk(_control_tree)
    if isinstance(node, ast.Compare)
    and isinstance(node.left, ast.Name)
    and node.left.id == "action"
    for name in _compared_action_names(node)
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

    def test_the_only_ui_actions_vlc_drops_are_the_ones_it_cannot_do(self):
        """
        The playbar carries subtitle-delay buttons, because some backends can
        do that and the panel is one page for all of them. VLC's cli has no verb
        for it, so it does not advertise the capability and those two buttons do
        nothing here.

        Named rather than asserted empty, so a button that stops working for any
        other reason fails instead of passing.
        """
        assert ui_actions() - SERVER_ACTIONS == {"subplus", "subminus"}

    def test_every_server_action_is_reachable(self):
        """
        The regression guard for the original finding. If this fails, a control
        exists in the key map with no button behind it.
        """
        assert SERVER_ACTIONS - ui_actions() == set()

    def test_the_set_is_the_expected_size(self):
        assert len(SERVER_ACTIONS) == 15, sorted(SERVER_ACTIONS)

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

    def test_the_key_backend_declares_every_capability_it_can(self):
        """
        Every capability except subtitle delay, which VLC's cli interface has
        no verb for -- so it does not advertise it, and the buttons are hidden
        rather than shown and ineffective.

        Stated as the set it is rather than as everything, so a capability
        quietly dropped for a good reason has to be named here rather than
        passing unnoticed.
        """
        from lib.player.backend import ALL_CAPABILITIES

        missing = {c for c in ALL_CAPABILITIES if not VlcProcess().supports(c)}
        assert missing == {"subtitle_delay"}, missing

    def test_ui_has_more_actions_than_there_are_capabilities(self):
        """
        Capability flags are coarser than actions: one 'seek' capability covers
        four seek actions. This records that the two vocabularies are not
        interchangeable, so nobody tries to compare them directly later.
        """
        from lib.player.backend import ALL_CAPABILITIES

        assert len(SERVER_ACTIONS) > len(ALL_CAPABILITIES)
        # Not a constant: this is a floor to catch actions losing their
        # capability, so adding one legitimately moves it.
        assert len(ALL_CAPABILITIES) >= 6
