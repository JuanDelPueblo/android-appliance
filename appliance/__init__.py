"""Android appliance v2: the control daemon of the container."""

from pathlib import Path


def _version() -> str:
    # VERSION sits next to the package, in the repository and in the image.
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


__version__ = _version()
