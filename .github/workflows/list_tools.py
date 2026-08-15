# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""CI check: the server boots over stdio and advertises its tools, no token needed.

Also asserts the safety annotations, so a tool can never quietly lose its
destructive marking.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parents[2] / "server.py")

MUTATING = {"server_action", "api_request"}


async def main() -> int:
    params = StdioServerParameters(command="uv", args=["run", "--script", SERVER])
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}

    print(f"server advertises {len(tools)} tools: {', '.join(sorted(tools))}")

    problems: list[str] = []
    if not tools:
        problems.append("no tools advertised")

    for name, tool in tools.items():
        annotations = tool.annotations
        read_only = getattr(annotations, "read_only_hint", None) if annotations else None
        if name in MUTATING and read_only:
            problems.append(f"{name} is mutating but marked read-only")
        if name not in MUTATING and not read_only:
            problems.append(f"{name} lacks a read-only annotation")
        if not tool.description:
            problems.append(f"{name} has no description")

    for missing in MUTATING - tools.keys():
        problems.append(f"expected tool {missing} is absent")

    if problems:
        print("FAILED:", *problems, sep="\n  - ")
        return 1
    print("all tools carry descriptions and correct safety annotations")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
