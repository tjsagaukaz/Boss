"""Boss Assistant package."""

from importlib import metadata

try:
    __version__ = metadata.version("boss-assistant")
except metadata.PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "agents",
    "config",
    "main",
    "models",
    "__version__",
]