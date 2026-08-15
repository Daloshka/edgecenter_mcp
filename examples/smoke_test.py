# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""Exercise the server against a live account, read-only.

Lists the tools, calls the safe ones, and verifies that mutating tools refuse to
act without confirmation. Needs a working token (see README).

    uv run --script examples/smoke_test.py [node-name-or-ip]

Pass a node name, UUID or IP as the argument to also test the per-node tools;
without it, the first instance found in the inventory is used.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parent.parent / "server.py")


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


async def call(session: ClientSession, name: str, args: dict, expect_refusal: bool = False):
    result = await session.call_tool(name, args)
    body = text_of(result)
    if expect_refusal:
        verdict = "OK (refused)" if result.is_error else "FAIL — should have refused!"
    else:
        verdict = "FAIL" if result.is_error else "OK"
    print(f"\n=== {name}({args}) → {verdict}")
    print(body[:600])
    return result


async def main() -> None:
    params = StdioServerParameters(command="uv", args=["run", "--script", SERVER])
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tools = await session.list_tools()
        print(f"=== {len(tools.tools)} tools ===")
        for tool in tools.tools:
            hint = getattr(tool.annotations, "read_only_hint", None) if tool.annotations else None
            print(f"  {'read-only' if hint else 'MUTATING '}  {tool.name}")

        await call(session, "whoami", {})
        await call(session, "regions", {})
        inventory = await call(session, "servers", {})

        target = sys.argv[1] if len(sys.argv) > 1 else None
        if not target:
            for line in text_of(inventory).splitlines()[2:]:
                if line.strip() and not line.startswith("-"):
                    target = line.split()[0]
                    break
        if target:
            await call(session, "server", {"query": target})
            await call(session, "console", {"query": target})

        await call(session, "costs", {})
        await call(session, "fleet_health", {})
        await call(session, "tasks", {"limit": 5})
        await call(session, "api_tokens", {})

        # guardrails: both must be refused
        if target:
            await call(
                session,
                "server_action",
                {"query": target, "action": "reboot"},
                expect_refusal=True,
            )
        await call(
            session,
            "api_request",
            {"path": "/cloud/v1/nonexistent", "method": "DELETE"},
            expect_refusal=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
