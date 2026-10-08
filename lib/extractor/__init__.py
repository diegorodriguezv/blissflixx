# This package is unused.
#
# lib/api/playr.py used to do `import extractor` and call into it to resolve
# ITV streams. That import was removed when lib/ became a package, which left
# this package with zero importers. The module body is kept commented out
# rather than deleted so the ITV stream resolution logic is still recoverable.
#
# Note it targets mercury.itv.com / a Flash player, so it is stale regardless.

# from . import itv
