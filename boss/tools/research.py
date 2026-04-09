"""Web search tool — governed Boss-native wrapper around the OpenAI web search API.

This uses a governed_function_tool with ExecutionType.EXTERNAL so that web
searches require approval through the Boss permission model.  The SDK's
built-in WebSearchTool was evaluated but intentionally not adopted because
it bypasses Boss governance (no needs_approval, no scope tracking, no
permission rule integration).

TODO: if the SDK adds a governed/approval-aware hosted tool surface in a
future release, re-evaluate whether to adopt it while preserving approval
and observability behavior.
"""

from boss.config import settings
from boss.execution import ExecutionType, governed_function_tool, web_scope_key, web_scope_label
from boss.models import get_client


@governed_function_tool(
    execution_type=ExecutionType.EXTERNAL,
    title="Web Search",
    describe_call=lambda params: f'Search the web for "{params.get("query", "")}"',
    scope_key=lambda params: web_scope_key(str(params.get("query", ""))),
    scope_label=lambda params: web_scope_label(str(params.get("query", ""))),
)
async def web_search(query: str) -> str:
    client = get_client()
    response = await client.responses.create(
        model=settings.research_model,
        input=(
            "Search the web and answer the user's query concisely. "
            "Prefer factual summaries and include source names or URLs when available.\n\n"
            f"Query: {query}"
        ),
        tools=[{"type": "web_search"}],
        tool_choice={"type": "web_search"},
        include=["web_search_call.action.sources"],
    )

    output_text = getattr(response, "output_text", "")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output_items = getattr(response, "output", []) or []
    lines: list[str] = []
    for item in output_items:
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for entry in content:
            if getattr(entry, "type", None) == "output_text":
                text = getattr(entry, "text", "")
                if text:
                    lines.append(text)

    return "\n".join(lines).strip() or "No web results returned."