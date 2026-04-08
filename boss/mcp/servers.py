from __future__ import annotations

from agents.mcp import MCPServerStdio


def create_apple_mcp() -> MCPServerStdio:
    return MCPServerStdio(
        name="apple",
        params={
            "command": "npx",
            "args": ["-y", "apple-mcp@latest"],
        },
        cache_tools_list=True,
    )


def create_filesystem_mcp() -> MCPServerStdio:
    return MCPServerStdio(
        name="filesystem",
        params={
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "/Users/tj/Documents",
                "/Users/tj/Desktop",
                "/Users/tj/Downloads",
            ],
        },
        cache_tools_list=True,
    )


def create_memory_mcp() -> MCPServerStdio:
    return MCPServerStdio(
        name="memory",
        params={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
        },
        cache_tools_list=True,
    )


def create_mcp_servers() -> dict[str, MCPServerStdio]:
    return {
        "apple": create_apple_mcp(),
        "filesystem": create_filesystem_mcp(),
        "memory": create_memory_mcp(),
    }