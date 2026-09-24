"""Hermes plugin for the Android appliance.

Every tool is a thin wrapper around ``androidctl``. The NixOS module owns the
emulator, systemd units and permissions; this plugin owns nothing else.
"""

from __future__ import annotations

from pathlib import Path

from . import tools


def register(ctx) -> None:
    tools.configure(ctx.get_config("androidctl", ""))
    for name, schema, handler, emoji in tools.TOOLS:
        ctx.register_tool(
            name=name,
            toolset="android",
            schema=schema,
            handler=handler,
            check_fn=tools.available,
            emoji=emoji,
        )
    skill = Path(__file__).parent / "skills" / "android" / "SKILL.md"
    ctx.register_skill("android", skill, description="Operate the Android appliance with androidctl.")
