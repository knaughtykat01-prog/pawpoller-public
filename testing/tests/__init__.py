"""Concrete test modules.

Importing this package triggers @register_test decorators in every
submodule, populating testing.registry.REGISTRY. Importing happens
once at server startup (see dashboard.py).

Adding a new test category: drop a new module here and add an import
line below.
"""

from __future__ import annotations

# Imports below intentionally trigger decorator side-effects. The
# order doesn't matter for correctness — REGISTRY is a dict — but
# matches CATEGORY_ORDER in testing.registry so file changes feel
# predictable.
from testing.tests import infra        # noqa: F401
from testing.tests import auth         # noqa: F401
from testing.tests import platforms    # noqa: F401
from testing.tests import editor       # noqa: F401
from testing.tests import story_reader  # noqa: F401
from testing.tests import posting      # noqa: F401
from testing.tests import external     # noqa: F401
from testing.tests import scheduling   # noqa: F401
from testing.tests import notifications  # noqa: F401
from testing.tests import archive      # noqa: F401
from testing.tests import pytest_runner  # noqa: F401
