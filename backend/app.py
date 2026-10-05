from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_VERSION = "1.0.0"
WORKSPACE_ROOT = Path(os.getenv("JARVIS_WORKSPACE", "./workspace")).resolve()
MAX_FILE_BYTES = int(os.getenv("JARVIS_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
PROVIDER_TIMEOUT = float(os.getenv("JARVIS_PROVIDER_TIMEOUT", "60"))
MAX_TOOL_ROUNDS = max(1, min(int(os.getenv("JARVIS_MAX_TOOL_ROUNDS", "8")), 12))
ALLOW_DESTRUCTIVE_TOOLS = os.getenv("JARVIS_ALLOW_DESTRUCTIVE_TOOLS", "false").lower() == "true"
ALLOW_LOCAL_PROVIDERS = os.getenv("JARVIS_ALLOW_LOCAL_PROVIDERS", "false").lower() == "true"
RATE_LIMIT = max(1, int(os.getenv("JARVIS_RATE_LIMIT", "60")))
RATE_WINDOW = 60

WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Jarvis API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("JARVIS_CORS_ORIGINS", "http://localhost:5173").split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    scheme: str = Field(default="openai-compatible", max_length=40)
    models_path: str = Field(default="/models", max_length=200)
    chat_path: str = Field(default="/chat/completions", max_length=200)
    request_model_field: str = Field(default="model", max_length=80)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("models_path", "chat_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("API paths must start with /")
        return value


class ProviderPublic(BaseModel):
    id: str
    name: str
    base_url: str
    scheme: str
    models_path: str
    chat_path: str


class ChatRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    enable_tools: bool = True


PROVIDERS: dict[str, Provider] = {}
REQUEST_TIMES: dict[str, deque[float]] = defaultdict(deque)


def provider_id(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return clean or secrets.token_hex(4)


def public_provider(provider: Provider) -> ProviderPublic:
    return ProviderPublic(id=provider_id(provider.name), name=provider.name, base_url=provider.base_url, scheme=provider.scheme, models_path=provider.models_path, chat_path=provider.chat_path)


def rate_limit(request: Request) -> None:
    now = time.time()
    key = request.client.host if request.client else "unknown"
    bucket = REQUEST_TIMES[key]
    while bucket and bucket[0] <= now - RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded")
    bucket.append(now)


def is_private_host(host: str) -> bool:
    if ALLOW_LOCAL_PROVIDERS:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return True
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def check_provider_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    if is_private_host(host):
        raise HTTPException(400, "Local/private provider addresses are disabled")


def safe_path(raw: str) -> Path:
    candidate = (WORKSPACE_ROOT / raw).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("Path escapes the Jarvis workspace") from exc
    current = WORKSPACE_ROOT
    for part in candidate.relative_to(WORKSPACE_ROOT).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink paths are not allowed")
    return candidate


def tool_schemas() -> list[dict[str, Any]]:
    tools = [
        {"type":"function","function":{"name":"list_files","description":"List files and directories inside the Jarvis workspace.","parameters":{"type":"object","properties":{"path":{"type":"string","default":"."}},"additionalProperties":False}}},
        {"type":"function","function":{"name":"read_file","description":"Read a UTF-8 text file inside the Jarvis workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}}},
        {"type":"function","function":{"name":"write_file","description":"Create or replace a UTF-8 text file inside the Jarvis workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"],"additionalProperties":False}}},
        {"type":"function","function":{"name":"make_directory","description":"Create a directory inside the Jarvis workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}}},
    ]
    if ALLOW_DESTRUCTIVE_TOOLS:
        tools.append({"type":"function","function":{"name":"delete_path","description":"Delete a file or empty directory inside the Jarvis workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}}})
    return tools


async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "list_files":
            path = safe_path(str(args.get("path", ".")))
            if not path.is_dir():
                return {"ok": False, "error": "Not a directory"}
            entries = []
            for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
                stat = item.stat()
                entries.append({"name": item.name, "type": "directory" if item.is_dir() else "file", "size": stat.st_size if item.is_file() else None})
            relative = str(path.relative_to(WORKSPACE_ROOT)) if path != WORKSPACE_ROOT else "."
            return {"ok": True, "path": relative, "entries": entries}
        if name == "read_file":
            path = safe_path(str(args["path"]))
            if not path.is_file():
                return {"ok": False, "error": "File not found"}
            if path.stat().st_size > MAX_FILE_BYTES:
                return {"ok": False, "error": f"File exceeds {MAX_FILE_BYTES} bytes"}
            return {"ok": True, "path": str(path.relative_to(WORKSPACE_ROOT)), "content": path.read_text(encoding="utf-8")}
        if name == "write_file":
            path = safe_path(str(args["path"]))
            content = str(args.get("content", ""))
            data = content.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                return {"ok": False, "error": f"Content exceeds {MAX_FILE_BYTES} bytes"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return {"ok": True, "path": str(path.relative_to(WORKSPACE_ROOT)), "bytes": len(data)}
        if name == "make_directory":
            path = safe_path(str(args["path"]))
            path.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "path": str(path.relative_to(WORKSPACE_ROOT))}
        if name == "delete_path" and ALLOW_DESTRUCTIVE_TOOLS:
            path = safe_path(str(args["path"]))
            if path == WORKSPACE_ROOT or not path.exists():
                return {"ok": False, "error": "Path cannot be deleted"}
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            return {"ok": True, "path": str(path.relative_to(WORKSPACE_ROOT))}
        return {"ok": False, "error": f"Unknown or disabled tool: {name}"}
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


def extract_models(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("data") or payload.get("models") or payload.get("items") or []
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    models: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            models.append({"id": item, "name": item})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if model_id:
                models.append({"id": str(model_id), "name": str(item.get("name") or model_id), "owned_by": item.get("owned_by")})
    return models


def parse_sse(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    value = line[5:].strip()
    if not value or value == "[DONE]":
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def append_tool_delta(store: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index", 0))
    current = store.setdefault(index, {"id": delta.get("id") or secrets.token_hex(8), "type": "function", "function": {"name": "", "arguments": ""}})
    if delta.get("id"):
        current["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        current["function"]["name"] += str(function["name"])
    if function.get("arguments"):
        current["function"]["arguments"] += str(function["arguments"])


def delta_text(obj: dict[str, Any]) -> str:
    for choice in obj.get("choices", []) or []:
        content = (choice.get("delta") or {}).get("content")
        if isinstance(content, str):
            return content
    return ""


def extract_tool_calls(obj: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for choice in obj.get("choices", []) or []:
        for call in (choice.get("message") or {}).get("tool_calls", []) or []:
            fn = call.get("function") or {}
            result.append({"id": call.get("id") or secrets.token_hex(8), "type":"function", "function":{"name":fn.get("name", ""),"arguments":fn.get("arguments", "{}")}})
    return result


def build_request(request: ChatRequest, messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
    provider = PROVIDERS[request.provider]
    body: dict[str, Any] = {provider.request_model_field: request.model, "messages": messages, "stream": stream}
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.enable_tools:
        body["tools"] = tool_schemas()
    return body


async def stream_provider(provider: Provider, request: ChatRequest, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    check_provider_url(provider.base_url)
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT, connect=10), follow_redirects=False) as client:
        async with client.stream("POST", f"{provider.base_url}{provider.chat_path}", headers=headers, json=build_request(request, messages, True)) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="replace")[:3000]
                raise RuntimeError(f"Provider returned HTTP {response.status_code}: {detail}")
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                payload = json.loads((await response.aread()).decode("utf-8"))
                yield {"json_response": payload}
                return
            async for line in response.aiter_lines():
                obj = parse_sse(line)
                if obj is not None:
                    yield obj


async def complete_provider(provider: Provider, request: ChatRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
    check_provider_url(provider.base_url)
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT, connect=10), follow_redirects=False) as client:
        response = await client.post(f"{provider.base_url}{provider.chat_path}", headers=headers, json=build_request(request, messages, False))
        if response.status_code >= 400:
            raise RuntimeError(f"Provider returned HTTP {response.status_code}: {response.text[:3000]}")
        return response.json()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"success": True, "status": "online", "version": APP_VERSION}


@app.get("/api/providers", response_model=list[ProviderPublic])
async def list_providers() -> list[ProviderPublic]:
    return [public_provider(provider) for provider in PROVIDERS.values()]


@app.post("/api/providers", response_model=ProviderPublic)
async def register_provider(provider: Provider, request: Request) -> ProviderPublic:
    rate_limit(request)
    check_provider_url(provider.base_url)
    PROVIDERS[provider_id(provider.name)] = provider
    return public_provider(provider)


@app.delete("/api/providers/{name}")
async def remove_provider(name: str, request: Request) -> dict[str, Any]:
    rate_limit(request)
    PROVIDERS.pop(name, None)
    return {"success": True}


@app.get("/api/providers/{name}/models")
async def get_models(name: str, request: Request) -> dict[str, Any]:
    rate_limit(request)
    provider = PROVIDERS.get(name)
    if not provider:
        raise HTTPException(404, "Provider not found")
    check_provider_url(provider.base_url)
    headers = {"Authorization": f"Bearer {provider.api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT, connect=10), follow_redirects=False) as client:
        response = await client.get(f"{provider.base_url}{provider.models_path}", headers=headers)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, f"Provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(502, "Provider returned invalid JSON") from exc
    return {"success": True, "models": extract_models(payload)}


@app.get("/api/tools")
async def get_tools() -> dict[str, Any]:
    return {"success": True, "tools": tool_schemas(), "workspace": "."}


async def chat_events(chat_request: ChatRequest, request_id: str) -> AsyncIterator[str]:
    started = time.perf_counter()
    provider = PROVIDERS.get(chat_request.provider)
    if not provider:
        yield f"event: error\ndata: {json.dumps({'error':'Provider not found','request_id':request_id})}\n\n"
        return
    messages = [dict(message) for message in chat_request.messages]
    for round_index in range(MAX_TOOL_ROUNDS + 1):
        tool_store: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        try:
            async for payload in stream_provider(provider, chat_request, messages):
                if "json_response" in payload:
                    result = payload["json_response"]
                    text = extract_text(result)
                    if text:
                        yield f"event: delta\ndata: {json.dumps({'text':text})}\n\n"
                    tool_calls = extract_tool_calls(result)
                    if tool_calls:
                        for index, call in enumerate(tool_calls):
                            tool_store[index] = call
                    break
                piece = delta_text(payload)
                if piece:
                    text_parts.append(piece)
                    yield f"event: delta\ndata: {json.dumps({'text':piece})}\n\n"
                for delta in (payload.get("choices", [{}])[0].get("delta", {}).get("tool_calls", []) or []):
                    append_tool_delta(tool_store, delta)

            tool_calls = [tool_store[index] for index in sorted(tool_store)]
            if not tool_calls:
                break

            assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None, "tool_calls": tool_calls}
            messages.append(assistant_message)
            for call in tool_calls:
                name = str(call["function"].get("name", ""))
                raw_args = str(call["function"].get("arguments") or "{}")
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError):
                    args = {}
                yield f"event: tool_call\ndata: {json.dumps({'name':name,'arguments':args}, ensure_ascii=False)}\n\n"
                tool_result = await run_tool(name, args)
                messages.append({"role":"tool","tool_call_id":call["id"],"content":json.dumps(tool_result, ensure_ascii=False)})
                yield f"event: tool_result\ndata: {json.dumps({'name':name,'result':tool_result}, ensure_ascii=False)}\n\n"
        except HTTPException as exc:
            yield f"event: error\ndata: {json.dumps({'error':str(exc.detail)[:3000],'request_id':request_id})}\n\n"
            return
        except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            yield f"event: error\ndata: {json.dumps({'error':str(exc)[:3000],'request_id':request_id})}\n\n"
            return
    elapsed = round(time.perf_counter() - started, 3)
    yield f"event: done\ndata: {json.dumps({'success':True,'request_id':request_id,'time_taken':elapsed,'tool_rounds':round_index})}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(chat_request: ChatRequest, request: Request) -> StreamingResponse:
    rate_limit(request)
    request_id = secrets.token_urlsafe(12)
    return StreamingResponse(chat_events(chat_request, request_id), media_type="text/event-stream", headers={"Cache-Control":"no-cache, no-transform","Connection":"keep-alive","X-Request-ID":request_id,"X-Accel-Buffering":"no"})


@app.post("/api/chat")
async def chat(chat_request: ChatRequest, request: Request) -> JSONResponse:
    rate_limit(request)
    request_id = secrets.token_urlsafe(12)
    provider = PROVIDERS.get(chat_request.provider)
    if not provider:
        raise HTTPException(404, "Provider not found")
    result = await complete_provider(provider, chat_request, chat_request.messages)
    choices = result.get("choices") or []
    message = choices[0].get("message", {}) if choices else {"role":"assistant","content":""}
    return JSONResponse({"success":True,"request_id":request_id,"model":chat_request.model,"provider":provider.name,"message":message,"usage":result.get("usage")})
