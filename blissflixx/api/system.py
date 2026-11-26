"""Installation-wide options set from the interface.

Separate from the channel settings and from the player's own file: these are
about how this installation presents itself, and the first of them is the name
on the bar and in the browser tab.
"""

from ..settings import load, save
from .common import ApiError

#: What the bar says when nothing has been set.
DEFAULT_TITLE = "BlissFlixx"

#: The settings file. One dict rather than a file per option: they are all
#: "how this box presents itself", and the form saves them together.
_FILE = "system"

#: Long enough for "blissflixx living room", short enough to still fit the bar.
_MAX_TITLE = 80


def _stored():
    data = load(_FILE)
    return data if isinstance(data, dict) else {}


def settings():
    """The effective system options, with the default filled in."""
    title = str(_stored().get("title") or "").strip()
    return {"title": title or DEFAULT_TITLE, "default_title": DEFAULT_TITLE}


def set_settings(values=None):
    """Save the options given, leaving anything else alone."""
    if not isinstance(values, dict):
        raise ApiError("System settings must be a mapping")
    data = dict(_stored())
    if "title" in values:
        title = str(values.get("title") or "").strip()
        if len(title) > _MAX_TITLE:
            raise ApiError(
                "The title is too long (%d characters at most)" % _MAX_TITLE
            )
        # Empty means "use the default", which is what a cleared field says.
        data["title"] = title
    save(_FILE, data)
    return settings()


def set_title(title=None):
    """The one-option form, for a caller that only has a name."""
    return set_settings({"title": title})
