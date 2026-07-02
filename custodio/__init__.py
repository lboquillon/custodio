# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Custodio: a transparent PII-anonymizing reverse proxy for the Anthropic API."""

from .config import Settings

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("custodio")
except (ImportError, PackageNotFoundError):  # pragma: no cover
    __version__ = "1.0.0"

__all__ = ["Settings", "__version__", "create_app"]


def create_app(settings=None):
    # Imported lazily so `import custodio` doesn't pull in FastAPI/httpx.
    from .proxy import create_app as _create_app

    return _create_app(settings)
