"""Tools and library for authoring catalog packages (TAI-ADR-0062)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("package-sdk")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

__all__ = ["__version__"]
