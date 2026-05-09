"""Local shim for simmer_sdk imports used by the weather trader scripts.

The repo inserts `scripts/` at the front of `sys.path`, which would otherwise
shadow the installed `simmer_sdk` package. This shim delegates to the real
package so `from simmer_sdk import SimmerClient` still works.
"""

from __future__ import annotations

import sys
from importlib.util import spec_from_file_location
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()


def _find_external_init() -> Path:
    for entry in sys.path:
        if not isinstance(entry, str):
            continue
        candidate = Path(entry) / "simmer_sdk" / "__init__.py"
        try:
            if candidate.exists() and candidate.resolve() != _THIS_FILE:
                return candidate
        except Exception:
            continue
    raise ImportError("Unable to locate installed simmer_sdk package")


_external_init = _find_external_init()
_spec = spec_from_file_location(
    __name__,
    _external_init,
    submodule_search_locations=[str(_external_init.parent)],
)
if _spec is None or _spec.loader is None:
    raise ImportError("Unable to load installed simmer_sdk package")

_module = sys.modules[__name__]
_module.__file__ = str(_external_init)
_module.__path__ = [str(_external_init.parent)]
_spec.loader.exec_module(_module)
