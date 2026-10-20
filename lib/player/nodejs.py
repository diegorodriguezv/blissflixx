"""
The JavaScript runtime yt-dlp and peerflix use.

yt-dlp needs one to run YouTube's JavaScript challenges, and it refuses node
below version 22 -- the node in Raspberry Pi OS is 20, so asking for "node"
bought nothing and said so only in a warning. configure.sh unpacks a pinned LTS
into data/node, and this is the one place that knows about it.

Deliberately not installed system-wide. Swapping the system node would move the
runtime that npm and peerflix were installed with, which is a change to the
machine rather than to this project; a checkout that has its own runtime, and
names it explicitly, installs and uninstalls with `rm -rf data/node`.
"""

import os

from ..locations import NODE_PATH

#: The binary, when the checkout has its own runtime.
_BUNDLED = os.path.join(NODE_PATH, "bin", "node")


def node_binary():
    """
    What to run something with: ours when it is there, else the one on PATH.

    peerflix goes through this too, so that a machine with no distribution node
    still has a node to run it -- see pflixproc.
    """
    return _BUNDLED if os.path.isfile(_BUNDLED) else "node"


def node_runtime():
    """
    yt-dlp's --js-runtimes value.

    A path rather than a bare name when the checkout has its own runtime,
    because a bare "node" would find the distribution's and be rejected as too
    old. Bare "node" otherwise, which is right on a machine whose node is new
    enough and is what a development checkout without the download gets.
    """
    if os.path.isfile(_BUNDLED):
        return "node:" + os.path.dirname(_BUNDLED)
    return "node"
