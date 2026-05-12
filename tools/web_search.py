from __future__ import annotations

import json
import os

from .registry import Tool

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for up-to-date information. "
            "Use when the user asks about current events, recent facts, "
            "or anything outside the model's training data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific.",
                }
            },
            "required": ["query"],
        },
    },
}


def build_web_search_tool() -> Tool | None:
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return None

    try:
        from tavily import AsyncTavilyClient
    except ImportError:
        return None

    depth = os.getenv("TAVILY_SEARCH_DEPTH", "basic")
    max_results = int(os.getenv("TAVILY_MAX_RESULTS", "5"))
    client = AsyncTavilyClient(api_key=api_key)

    async def handler(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "Error: 'query' is required."

        result = await client.search(
            query=query,
            search_depth=depth,
            max_results=max_results,
        )
        trimmed = {
            "answer": result.get("answer"),
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("content"),
                }
                for r in result.get("results", [])
            ],
        }
        return json.dumps(trimmed, ensure_ascii=False)

    return Tool(schema=SCHEMA, handler=handler)
