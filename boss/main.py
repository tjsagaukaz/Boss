from __future__ import annotations

import argparse
import asyncio
import uuid

from agents import RunConfig, Runner
from agents.mcp import MCPServerManager
from rich.console import Console
from rich.panel import Panel

from boss.agents import build_entry_agent
from boss.context.manager import SessionContextManager
from boss.mcp.servers import create_mcp_servers
from boss.models import build_run_execution_options


console = Console()
session_context_manager = SessionContextManager()


async def chat(user_input: str, history: list | None = None):
    agent = build_entry_agent()
    prepared_input = user_input if not history else [*history, {"role": "user", "content": user_input}]
    execution_options = build_run_execution_options(workflow_name="Boss CLI Chat")
    return await Runner.run(
        agent,
        input=prepared_input,
        run_config=execution_options.run_config,
        session=execution_options.session,
    )


async def repl(session_id: str, enable_mcp: bool = False) -> None:
    session_context_manager.load_session_read_only(session_id)
    console.print(Panel.fit(f"Boss Assistant ready. Session: {session_id}"))

    if enable_mcp:
        configured_servers = create_mcp_servers()
        async with MCPServerManager(configured_servers.values()) as manager:
            active_servers = {server.name: server for server in manager.active_servers if server.name}
            await _repl_loop(session_id, build_entry_agent(active_mcp_servers=active_servers))
        return

    await _repl_loop(session_id, build_entry_agent())


async def _repl_loop(session_id: str, agent) -> None:
    while True:
        try:
            user_input = console.input("[bold cyan]You:[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\nExiting.")
            break

        if user_input.strip().lower() in {"quit", "exit"}:
            break

        prepared_input = session_context_manager.prepare_input(session_id, user_input).model_input
        execution_options = build_run_execution_options(
            session_id=session_id,
            workflow_name="Boss CLI",
            trace_metadata={"surface": "cli", "session_id": session_id},
        )
        result = await Runner.run(
            agent,
            input=prepared_input,
            run_config=execution_options.run_config,
            session=execution_options.session,
        )
        console.print(f"[bold green]Assistant:[/bold green] {result.final_output}\n")
        session_context_manager.persist_result(session_id, result.to_input_list())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Boss Assistant CLI")
    parser.add_argument("--session", default=str(uuid.uuid4()), help="Session ID for persisted history")
    parser.add_argument("--enable-mcp", action="store_true", help="Connect configured MCP servers for Apple, filesystem, and memory access")
    return parser.parse_args()


def cli_main() -> None:
    args = parse_args()
    asyncio.run(repl(args.session, enable_mcp=args.enable_mcp))


if __name__ == "__main__":
    cli_main()