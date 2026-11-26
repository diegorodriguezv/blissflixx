"""Compatibility alias: ``lib`` is the package now called ``blissflixx``.

Third-party channels are separate repositories that import the application by
its old name (``from lib.chanutils import ...``, ``from lib.playitem import
...``). They are installed under ``plugins/``, outside this repository, and
updated on their own schedule, so the old name keeps resolving for one
release. Stage 3 of the restructure loads channels from entry points; this
file goes at the same time.

The alias has to share module objects rather than re-import the package under
a second name: settings, the player singleton and the channel registry are
module-level state, and two copies would be two states. The finder below hands
``lib.<name>`` imports back as the already-imported ``blissflixx.<name>``.
"""

import importlib
import importlib.abc
import importlib.util
import sys

_REAL = "blissflixx"
_OLD = "lib"


class _AliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return importlib.import_module(_REAL + spec.name[len(_OLD) :])

    def exec_module(self, module):
        # The import machinery stamps its own metadata on whatever module the
        # loader returns (__spec__, __package__). This object is the real
        # module, shared with blissflixx.*, so put its own metadata back.
        alias = module.__spec__
        real = getattr(alias, "_real_spec", None)
        if real is not None:
            module.__spec__ = real
            module.__package__ = real.parent


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        real_name = _REAL + fullname[len(_OLD) :]
        try:
            module = importlib.import_module(real_name)
        except ModuleNotFoundError as exc:
            if exc.name != real_name:
                raise
            return None
        spec = importlib.util.spec_from_loader(
            fullname, _AliasLoader(), is_package=hasattr(module, "__path__")
        )
        spec._real_spec = module.__spec__
        if hasattr(module, "__path__"):
            spec.submodule_search_locations = module.__path__
        return spec


sys.meta_path.insert(0, _AliasFinder())

# `import lib` hands back the real package, attributes and all.
sys.modules[__name__] = importlib.import_module(_REAL)
