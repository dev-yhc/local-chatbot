import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field

from tools import ToolRegistry, build_registry

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
MCP_CONFIG_PATH = os.getenv("MCP_CONFIG_PATH", "./mcp.json")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "5"))

NO_TOOLS_NOTE = (
    "External tools (web search, MCP) are not configured in this environment. "
    "Answer using only your own knowledge, and be explicit when information "
    "might be out of date or you are unsure."
)

client = AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry, mcp_manager = await build_registry(MCP_CONFIG_PATH)
    app.state.registry = registry
    app.state.mcp_manager = mcp_manager
    try:
        yield
    finally:
        if mcp_manager is not None:
            await mcp_manager.close()


app = FastAPI(title="Local Chatbot", version="0.2.0", lifespan=lifespan)


class Message(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float = 0.7
    stream: bool = False


class ChatResponse(BaseModel):
    model: str
    content: str
    tools_used: list[str] = Field(default_factory=list)


@app.get("/health")
async def health(request: Request) -> dict:
    registry: ToolRegistry = request.app.state.registry
    return {
        "status": "ok",
        "model": OLLAMA_MODEL,
        "base_url": OLLAMA_BASE_URL,
        "tools": registry.names(),
    }


@app.get("/models")
async def list_models() -> dict:
    try:
        models = await client.models.list()
        return {"data": [m.model_dump() for m in models.data]}
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {e}")


# --- OpenAI-compatible endpoints (for Open WebUI, OpenAI SDK clients, ...) ---


class OpenAIChatRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]]
    temperature: float | None = 0.7
    stream: bool = False

    model_config = {"extra": "allow"}


@app.get("/v1/models")
async def v1_list_models() -> dict:
    try:
        models = await client.models.list()
        return {"object": "list", "data": [m.model_dump() for m in models.data]}
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {e}")


@app.post("/v1/chat/completions")
async def v1_chat_completions(req: OpenAIChatRequest, request: Request):
    model = req.model or OLLAMA_MODEL
    registry: ToolRegistry = request.app.state.registry
    messages = _prepare_messages(list(req.messages), registry)
    temperature = req.temperature if req.temperature is not None else 0.7

    if req.stream:
        return StreamingResponse(
            _stream_openai(model, messages, temperature, registry),
            media_type="text/event-stream",
        )

    try:
        content, _ = await _run_tool_loop(model, messages, temperature, registry)
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    model = req.model or OLLAMA_MODEL
    registry: ToolRegistry = request.app.state.registry
    messages = _prepare_messages([m.model_dump() for m in req.messages], registry)

    if req.stream:
        return StreamingResponse(
            _stream_chat(model, messages, req.temperature, registry),
            media_type="text/event-stream",
        )

    try:
        content, tools_used = await _run_tool_loop(model, messages, req.temperature, registry)
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(model=model, content=content, tools_used=tools_used)


def _prepare_messages(messages: list[dict], registry: ToolRegistry) -> list[dict]:
    if not registry.is_empty():
        return messages

    # No tools available — tell the model so it stops trying to "search"
    # in its head and just answers from its own knowledge.
    if messages and messages[0].get("role") == "system":
        head = dict(messages[0])
        head["content"] = f"{head['content']}\n\n{NO_TOOLS_NOTE}"
        return [head, *messages[1:]]
    return [{"role": "system", "content": NO_TOOLS_NOTE}, *messages]


async def _run_tool_loop(
    model: str,
    messages: list[dict],
    temperature: float,
    registry: ToolRegistry,
) -> tuple[str, list[str]]:
    tools_param = registry.schemas() if not registry.is_empty() else None
    tools_used: list[str] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools_param:
            kwargs["tools"] = tools_param

        completion = await client.chat.completions.create(**kwargs)
        msg = completion.choices[0].message

        if not msg.tool_calls:
            return msg.content or "", tools_used

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await registry.call(tc.function.name, args)
            tools_used.append(tc.function.name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    return (
        f"(stopped after {MAX_TOOL_ITERATIONS} tool iterations without a final answer)",
        tools_used,
    )


async def _stream_chat(
    model: str,
    messages: list[dict],
    temperature: float,
    registry: ToolRegistry,
) -> AsyncIterator[bytes]:
    try:
        if registry.is_empty():
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield f"data: {delta}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
            return

        # Tool-calling path: run the loop non-streamed (intermediate tool
        # turns are hard to stream cleanly), then emit the final answer.
        # A future Temporal/ReAct layer can replace this with proper
        # event-by-event streaming.
        content, _ = await _run_tool_loop(model, messages, temperature, registry)
        yield f"data: {content}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    except OpenAIError as e:
        yield f"data: [ERROR] {e}\n\n".encode("utf-8")


async def _stream_openai(
    model: str,
    messages: list[dict],
    temperature: float,
    registry: ToolRegistry,
) -> AsyncIterator[bytes]:
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def envelope(delta: dict, finish_reason: str | None = None) -> bytes:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    try:
        yield envelope({"role": "assistant"})

        if registry.is_empty():
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield envelope({"content": delta})
        else:
            content, _ = await _run_tool_loop(model, messages, temperature, registry)
            if content:
                yield envelope({"content": content})

        yield envelope({}, finish_reason="stop")
        yield b"data: [DONE]\n\n"
    except OpenAIError as e:
        err = {"error": {"message": str(e), "type": "upstream_error"}}
        yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
