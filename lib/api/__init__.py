# This package is deliberately empty.
#
# Importing submodules eagerly here made `lib.api` an active hub rather than a
# namespace: `channels` instantiated InstalledChannels() at import time, which
# does network I/O and re-imports every installed channel. That produced two
# hard circular imports, both of which broke the natural import order:
#
#   import lib.player  -> pflixproc -> lib.api -> playr -> lib.player (partial)
#   import bfch_eztv   -> lib.api.torrent -> channels -> InstalledChannels()
#                         -> re-imports bfch_eztv while it is still executing
#
# The server entrypoint imports the submodules it needs explicitly, and the
# modules that are imported by workers (lib.api.torrent) now load without
# dragging in cherrypy, the player, or the channel loader.
