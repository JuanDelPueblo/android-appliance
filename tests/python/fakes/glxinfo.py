"""Fake glxinfo -B: prints the renderer of the scenario on DISPLAY :0."""

import os
import sys

import common


def main() -> int:
    if os.environ.get("DISPLAY") != ":0":
        print("Error: unable to open display", file=sys.stderr)
        return 1
    print("name of display: :0\ndirect rendering: Yes")
    print(f"    OpenGL renderer string: {common.config()['glx_renderer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
