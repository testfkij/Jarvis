from __future__ import annotations

import asyncio
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

from commands import COMMANDS, command_suggestions, parse_done
from history import delete_chat, get_chat, list_chats, upsert_chat
from providers import PROVIDER_PRESETS
from skills import list_skills, select_skills

APP_VERSION = "2.2.0"
WORKSPACE_ROOT = Path(os.getenv("JARVIS_WORKSPACE", "./workspace")).resolve()
PROVIDER_CONFIG = Path(os.getenv("JARVIS_PROVIDER_CONFIG", "./providers.json")).resolve()
MAX_FILE_BYTES = int(os.getenv("JARVIS_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
PROVIDER_TIMEOUT = max(5.0, float(os.getenv("JARVIS_PROVIDER_TIMEOUT", "120")))
MAX_TOOL_ROUNDS = max(0, int(os.getenv("JARVIS_MAX_TOOL_ROUNDS", "0")))  # 0 = uncapped
CONTEXT_MESSAGES = max(8, min(80, int(os.getenv("JARVIS_CONTEXT_MESSAGES", "32"))))
MAX_MESSAGE_CHARS = max(4000, int(os.getenv("JARVIS_MAX_MESSAGE_CHARS", "24000")))
ALLOW_DESTRUCTIVE_TOOLS = os.getenv("JARVIS_ALLOW_DESTRUCTIVE_TOOLS", "false").lower() == "true"
ALLOW_LOCAL_PROVIDERS = os.getenv("JARVIS_ALLOW_LOCAL_PROVIDERS", "false").lower() == "true"
RATE_LIMIT = max(1, int(os.getenv("JARVIS_RATE_LIMIT", "60")))
RATE_WINDOW = 60
REQUEST_COUNT = 0

WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Jarvis API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("JARVIS_CORS_ORIGINS", "*").split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Jarvis-Client"],
)


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=2000)
    scheme: str = Field(default="openai-compatible", max_length=40)
    models_path: str = Field(default="/models", max_length=200)
    chat_path: str = Field(default="/chat/completions", max_length=300)
    request_model_field: str = Field(default="model", max_length=80)
    auth: str = Field(default="bearer", max_length=32)
    kind: str = Field(default="online", max_length=16)
    protocol: str = Field(default="openai", max_length=24)

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
        if value and not value.startswith("/"):
            raise ValueError("API paths must start with /")
        return value

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        allowed = {"bearer", "x-api-key", "google-key", "none"}
        if value not in allowed:
            raise ValueError(f"auth must be one of {sorted(allowed)}")
        return value


class ProviderPublic(BaseModel):
    id: str
    name: str
    base_url: str
    scheme: str
    models_path: str
    chat_path: str
    auth: str
    kind: str
    protocol: str
    configured: bool


class ChatRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    enable_tools: bool = True
    done_mode: bool = False


class HistoryItem(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    provider: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    updated_at: str = Field(min_length=1, max_length=60)


PROVIDERS: dict[str, Provider] = {}
REQUEST_TIMES: dict[str, deque[float]] = defaultdict(deque)


def provider_id(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return clean or secrets.token_hex(4)


def public_provider(provider: Provider) -> ProviderPublic:
    return ProviderPublic(
        id=provider_id(provider.name), name=provider.name, base_url=provider.base_url,
        scheme=provider.scheme, models_path=provider.models_path, chat_path=provider.chat_path,
        auth=provider.auth, kind=provider.kind, protocol=provider.protocol,
        configured=(provider.kind == "local" or bool(provider.api_key)),
    )


def provider_headers(provider: Provider, accept: str = "application/json") -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": accept, "User-Agent": "Jarvis/2.2"}
    if provider.auth == "x-api-key":
        headers["x-api-key"] = provider.api_key
        headers["anthropic-version"] = os.getenv("JARVIS_ANTHROPIC_VERSION", "2023-06-01")
    elif provider.auth == "google-key":
        headers["x-goog-api-key"] = provider.api_key
    elif provider.auth == "bearer" and provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def load_provider_config() -> None:
    if not PROVIDER_CONFIG.is_file():
        return
    try:
        raw = json.loads(PROVIDER_CONFIG.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                provider = Provider.model_validate(item)
                PROVIDERS[provider_id(provider.name)] = provider
    except (OSError, ValueError, TypeError):
        return


def save_provider_config() -> None:
    try:
        PROVIDER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROVIDER_CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps([p.model_dump() for p in PROVIDERS.values()], indent=2), encoding="utf-8")
        os.replace(tmp, PROVIDER_CONFIG)
        os.chmod(PROVIDER_CONFIG, 0o600)
    except OSError:
        pass


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


def check_provider_url(provider: Provider) -> None:
    host = urlparse(provider.base_url).hostname or ""
    local_allowed = ALLOW_LOCAL_PROVIDERS or provider.kind == "local"
    if is_private_host(host) and not local_allowed:
        raise HTTPException(400, "Local/private provider addresses are disabled for online providers")


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
        {"type":"function","function":{"name":"search_files","description":"Search file names and UTF-8 text inside the Jarvis workspace.","parameters":{"type":"object","properties":{"query":{"type":"string"},"path":{"type":"string","default":"."}},"required":["query"],"additionalProperties":False}}},
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
            if not path.is_dir(): return {"ok": False, "error": "Not a directory"}
            entries=[]
            for item in sorted(path.iterdir(), key=lambda x:(not x.is_dir(), x.name.lower()))[:500]:
                stat=item.stat(); entries.append({"name":item.name,"type":"directory" if item.is_dir() else "file","size":stat.st_size if item.is_file() else None})
            return {"ok":True,"path":str(path.relative_to(WORKSPACE_ROOT)) if path != WORKSPACE_ROOT else ".","entries":entries}
        if name == "search_files":
            query=str(args.get("query",""))[:200].lower(); root=safe_path(str(args.get("path",".")))
            if not query: return {"ok":False,"error":"Query is required"}
            matches=[]
            for item in root.rglob("*"):
                if len(matches)>=100: break
                if item.is_symlink(): continue
                rel=str(item.relative_to(WORKSPACE_ROOT))
                if query in item.name.lower(): matches.append({"path":rel,"match":"filename"}); continue
                if item.is_file() and item.stat().st_size <= MAX_FILE_BYTES:
                    try:
                        text=item.read_text(encoding="utf-8",errors="ignore")
                        if query in text.lower(): matches.append({"path":rel,"match":"content"})
                    except (OSError,UnicodeError): pass
            return {"ok":True,"query":query,"matches":matches}
        if name == "read_file":
            path=safe_path(str(args["path"]))
            if not path.is_file(): return {"ok":False,"error":"File not found"}
            if path.stat().st_size>MAX_FILE_BYTES: return {"ok":False,"error":f"File exceeds {MAX_FILE_BYTES} bytes"}
            return {"ok":True,"path":str(path.relative_to(WORKSPACE_ROOT)),"content":path.read_text(encoding="utf-8")}
        if name == "write_file":
            path=safe_path(str(args["path"])); data=str(args.get("content","")).encode("utf-8")
            if len(data)>MAX_FILE_BYTES: return {"ok":False,"error":f"Content exceeds {MAX_FILE_BYTES} bytes"}
            path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_bytes(data); os.replace(tmp,path)
            return {"ok":True,"path":str(path.relative_to(WORKSPACE_ROOT)),"bytes":len(data)}
        if name == "make_directory":
            path=safe_path(str(args["path"])); path.mkdir(parents=True,exist_ok=True)
            return {"ok":True,"path":str(path.relative_to(WORKSPACE_ROOT))}
        if name == "delete_path" and ALLOW_DESTRUCTIVE_TOOLS:
            path=safe_path(str(args["path"]))
            if path == WORKSPACE_ROOT or not path.exists(): return {"ok":False,"error":"Path cannot be deleted"}
            if path.is_dir(): path.rmdir()
            else: path.unlink()
            return {"ok":True,"path":str(path.relative_to(WORKSPACE_ROOT))}
        return {"ok":False,"error":f"Unknown or disabled tool: {name}"}
    except (OSError,UnicodeError,ValueError,KeyError) as exc:
        return {"ok":False,"error":str(exc)}


def compact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system=[dict(m) for m in messages if m.get("role")=="system"]
    rest=[dict(m) for m in messages if m.get("role")!="system"][-CONTEXT_MESSAGES:]
    out=[]
    for msg in system+rest:
        clone=dict(msg)
        content=clone.get("content")
        if isinstance(content,str) and len(content)>MAX_MESSAGE_CHARS:
            clone["content"]=content[:MAX_MESSAGE_CHARS]+"\n[context trimmed by Jarvis]"
        if clone.get("role")=="tool" and isinstance(clone.get("content"),str) and len(clone["content"])>12000:
            clone["content"]=clone["content"][:12000]+"\n[tool output trimmed]"
        out.append(clone)
    return out


def extract_models(payload: Any) -> list[dict[str, Any]]:
    raw=[]
    if isinstance(payload,dict): raw=payload.get("data") or payload.get("models") or payload.get("items") or []
    elif isinstance(payload,list): raw=payload
    if not isinstance(raw,list): return []
    result=[]
    for item in raw:
        if isinstance(item,str): result.append({"id":item,"name":item})
        elif isinstance(item,dict):
            model_id=item.get("id") or item.get("name") or item.get("model")
            if model_id:
                mid=str(model_id); result.append({"id":mid.removeprefix("models/"),"name":str(item.get("displayName") or item.get("name") or mid.removeprefix("models/")),"owned_by":item.get("owned_by") or item.get("publisher")})
    return result


def extract_text(obj: dict[str, Any]) -> str:
    for choice in obj.get("choices",[]) or []:
        content=(choice.get("message") or {}).get("content")
        if isinstance(content,str): return content
    return ""


def delta_text(obj: dict[str, Any]) -> str:
    for choice in obj.get("choices",[]) or []:
        content=(choice.get("delta") or {}).get("content")
        if isinstance(content,str): return content
    return ""


def extract_tool_calls(obj: dict[str, Any]) -> list[dict[str, Any]]:
    result=[]
    for choice in obj.get("choices",[]) or []:
        for call in (choice.get("message") or {}).get("tool_calls",[]) or []:
            fn=call.get("function") or {}
            result.append({"id":call.get("id") or secrets.token_hex(8),"type":"function","function":{"name":fn.get("name", ""),"arguments":fn.get("arguments", "{}")}})
    return result


def openai_body(request: ChatRequest, messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
    body={"model":request.model,"messages":messages,"stream":stream}
    if request.temperature is not None: body["temperature"]=request.temperature
    if request.max_tokens is not None: body["max_tokens"]=request.max_tokens
    if request.enable_tools: body["tools"]=tool_schemas()
    return body


async def openai_stream(provider: Provider, request: ChatRequest, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str,Any]]:
    check_provider_url(provider)
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
        async with client.stream("POST",f"{provider.base_url}{provider.chat_path}",headers=provider_headers(provider,"text/event-stream"),json=openai_body(request,messages,True)) as response:
            if response.status_code>=400: raise RuntimeError(f"Provider returned HTTP {response.status_code}: {(await response.aread()).decode('utf-8','replace')[:3000]}")
            if "text/event-stream" not in response.headers.get("content-type",""):
                yield {"json_response":json.loads((await response.aread()).decode("utf-8"))}; return
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    raw=line[5:].strip()
                    if raw and raw!="[DONE]":
                        try: yield json.loads(raw)
                        except json.JSONDecodeError: pass


async def anthropic_stream(provider: Provider, request: ChatRequest, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str,Any]]:
    check_provider_url(provider)
    system=[]; converted=[]
    for m in compact_messages(messages):
        role=m.get("role")
        if role=="system": system.append(str(m.get("content","")))
        elif role in {"user","assistant"}: converted.append({"role":role,"content":m.get("content","")})
    body={"model":request.model,"max_tokens":request.max_tokens or 4096,"messages":converted,"stream":True}
    if system: body["system"]="\n\n".join(system)
    if request.temperature is not None: body["temperature"]=request.temperature
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
        async with client.stream("POST",f"{provider.base_url}/v1/messages",headers=provider_headers(provider,"text/event-stream"),json=body) as response:
            if response.status_code>=400: raise RuntimeError(f"Anthropic returned HTTP {response.status_code}: {(await response.aread()).decode('utf-8','replace')[:3000]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"): continue
                try: obj=json.loads(line[5:].strip())
                except json.JSONDecodeError: continue
                if obj.get("type")=="content_block_delta":
                    delta=obj.get("delta") or {}
                    if delta.get("type")=="text_delta": yield {"choices":[{"delta":{"content":delta.get("text","")}}]}


async def gemini_stream(provider: Provider, request: ChatRequest, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str,Any]]:
    check_provider_url(provider)
    contents=[]; system_parts=[]
    for m in compact_messages(messages):
        role=m.get("role")
        text=str(m.get("content", ""))
        if role=="system": system_parts.append(text); continue
        contents.append({"role":"model" if role=="assistant" else "user","parts":[{"text":text}]})
    body={"contents":contents}
    if system_parts: body["systemInstruction"]={"parts":[{"text":"\n\n".join(system_parts)}]}
    if request.temperature is not None or request.max_tokens is not None:
        body["generationConfig"]={}
        if request.temperature is not None: body["generationConfig"]["temperature"]=request.temperature
        if request.max_tokens is not None: body["generationConfig"]["maxOutputTokens"]=request.max_tokens
    url=f"{provider.base_url}/v1beta/models/{request.model}:streamGenerateContent?alt=sse"
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
        async with client.stream("POST",url,headers=provider_headers(provider,"text/event-stream"),json=body) as response:
            if response.status_code>=400: raise RuntimeError(f"Gemini returned HTTP {response.status_code}: {(await response.aread()).decode('utf-8','replace')[:3000]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"): continue
                try: obj=json.loads(line[5:].strip())
                except json.JSONDecodeError: continue
                parts=((obj.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                text="".join(str(p.get("text","")) for p in parts if isinstance(p,dict))
                if text: yield {"choices":[{"delta":{"content":text}}]}


async def provider_stream(provider: Provider, request: ChatRequest, messages: list[dict[str,Any]]) -> AsyncIterator[dict[str,Any]]:
    if provider.protocol=="anthropic":
        async for x in anthropic_stream(provider,request,messages): yield x
    elif provider.protocol=="gemini":
        async for x in gemini_stream(provider,request,messages): yield x
    else:
        async for x in openai_stream(provider,request,messages): yield x


async def provider_models(provider: Provider) -> list[dict[str,Any]]:
    check_provider_url(provider)
    if provider.protocol=="gemini":
        url=f"{provider.base_url}{provider.models_path or '/v1beta/models'}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
            response=await client.get(url,headers=provider_headers(provider))
            if response.status_code>=400: raise HTTPException(response.status_code,f"Provider returned HTTP {response.status_code}: {response.text[:1000]}")
            return extract_models(response.json())
    if provider.protocol=="anthropic":
        url=f"{provider.base_url}{provider.models_path or '/v1/models'}"
    else:
        url=f"{provider.base_url}{provider.models_path}"
    if not provider.models_path: return []
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
        response=await client.get(url,headers=provider_headers(provider))
        if response.status_code>=400: raise HTTPException(response.status_code,f"Provider returned HTTP {response.status_code}: {response.text[:1000]}")
        return extract_models(response.json())


async def complete_provider(provider: Provider, request: ChatRequest, messages: list[dict[str,Any]]) -> dict[str,Any]:
    check_provider_url(provider)
    if provider.protocol=="anthropic":
        system=[]; converted=[]
        for m in compact_messages(messages):
            if m.get("role")=="system": system.append(str(m.get("content","")))
            elif m.get("role") in {"user","assistant"}: converted.append({"role":m.get("role"),"content":m.get("content","")})
        body={"model":request.model,"max_tokens":request.max_tokens or 4096,"messages":converted,"stream":False}
        if system: body["system"]="\n\n".join(system)
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
            response=await client.post(f"{provider.base_url}/v1/messages",headers=provider_headers(provider),json=body)
            if response.status_code>=400: raise RuntimeError(f"Anthropic returned HTTP {response.status_code}: {response.text[:3000]}")
            payload=response.json(); text="".join(str(x.get("text","")) for x in payload.get("content",[]) if isinstance(x,dict))
            return {"choices":[{"message":{"role":"assistant","content":text}}],"usage":payload.get("usage")}
    if provider.protocol=="gemini":
        contents=[]; system_parts=[]
        for m in compact_messages(messages):
            if m.get("role")=="system": system_parts.append(str(m.get("content",""))); continue
            contents.append({"role":"model" if m.get("role")=="assistant" else "user","parts":[{"text":str(m.get("content",""))}]})
        body={"contents":contents}
        if system_parts: body["systemInstruction"]={"parts":[{"text":"\n\n".join(system_parts)}]}
        url=f"{provider.base_url}/v1beta/models/{request.model}:generateContent"
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
            response=await client.post(url,headers=provider_headers(provider),json=body)
            if response.status_code>=400: raise RuntimeError(f"Gemini returned HTTP {response.status_code}: {response.text[:3000]}")
            payload=response.json(); parts=((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            text="".join(str(p.get("text","")) for p in parts if isinstance(p,dict))
            return {"choices":[{"message":{"role":"assistant","content":text}}],"usage":payload.get("usageMetadata")}
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROVIDER_TIMEOUT,connect=10),follow_redirects=False) as client:
        response=await client.post(f"{provider.base_url}{provider.chat_path}",headers=provider_headers(provider),json=openai_body(request,compact_messages(messages),False))
        if response.status_code>=400: raise RuntimeError(f"Provider returned HTTP {response.status_code}: {response.text[:3000]}")
        return response.json()


def sse(event: str, data: dict[str,Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data,ensure_ascii=False,separators=(',',':'))}\n\n"


async def chat_events(chat_request: ChatRequest, request_id: str) -> AsyncIterator[str]:
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    started=time.perf_counter(); provider=PROVIDERS.get(chat_request.provider)
    if not provider:
        yield sse("error",{"error":"Provider not found","request_id":request_id}); return
    messages=compact_messages(chat_request.messages)
    # /done works as a modifier at either end of the latest user message for API clients too.
    latest_user=next((m for m in reversed(messages) if m.get("role")=="user" and isinstance(m.get("content"),str)),None)
    marker_done=False
    if latest_user is not None:
        marker_done, cleaned=parse_done(str(latest_user.get("content","")))
        if marker_done:
            latest_user["content"]=cleaned
    done_mode=chat_request.done_mode or marker_done
    matched_skills=select_skills("\n".join(str(m.get("content","")) for m in messages if m.get("role")=="user"))
    runtime=["You are Jarvis, an implementation-focused AI workspace agent.","Use available workspace tools when they materially advance the task. Keep responses concise while working."]
    if matched_skills:
        runtime.append("Relevant developer skills:\n"+"\n\n".join(matched_skills))
    if done_mode:
        runtime.append("BUILD COMPLETION MODE: continue using tools and iterative steps until the user's build task is genuinely complete and verified. Do not stop merely because one step succeeded. When the entire task is actually complete, end the final response with [JARVIS_DONE].")
    messages=[{"role":"system","content":"\n\n".join(runtime)}]+messages
    round_index=0
    while True:
        if await asyncio.sleep(0) is None:
            yield sse("status",{"stage":"model","round":round_index+1,"done_mode":done_mode})
        tool_store={}; text_parts=[]
        try:
            async for payload in provider_stream(provider,chat_request,messages):
                if "json_response" in payload:
                    result=payload["json_response"]; text=extract_text(result)
                    if text: text_parts.append(text); yield sse("delta",{"text":text})
                    for idx,call in enumerate(extract_tool_calls(result)): tool_store[idx]=call
                    break
                piece=delta_text(payload)
                if piece: text_parts.append(piece); yield sse("delta",{"text":piece})
                for delta in (payload.get("choices", [{}])[0].get("delta",{}).get("tool_calls",[]) or []): append_tool_delta(tool_store,delta)
            tool_calls=[tool_store[i] for i in sorted(tool_store)]
            round_text="".join(text_parts)
            if not tool_calls:
                if done_mode and "[JARVIS_DONE]" not in round_text:
                    messages.append({"role":"assistant","content":round_text})
                    messages.append({"role":"user","content":"Continue the build. Inspect or change whatever is still required, verify it, and do not give a final answer yet. When everything is genuinely complete, end with [JARVIS_DONE]."})
                    round_index+=1
                    if MAX_TOOL_ROUNDS and round_index>=MAX_TOOL_ROUNDS:
                        yield sse("status",{"stage":"limit","message":f"Reached JARVIS_MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS}. Set it to 0 for uncapped completion mode."})
                        break
                    continue
                break
            if provider.protocol!="openai":
                yield sse("status",{"stage":"tools","message":"Native provider adapter completed without tool calls; continuing with normal response."})
                break
            messages.append({"role":"assistant","content":"".join(text_parts) or None,"tool_calls":tool_calls})
            for call in tool_calls:
                name=str(call["function"].get("name","")); raw_args=str(call["function"].get("arguments") or "{}")
                try: args=json.loads(raw_args); args=args if isinstance(args,dict) else {}
                except json.JSONDecodeError: args={}
                yield sse("tool_call",{"name":name,"arguments":args,"round":round_index+1})
                result=await run_tool(name,args)
                messages.append({"role":"tool","tool_call_id":call["id"],"content":json.dumps(result,ensure_ascii=False,separators=(",",":"))})
                yield sse("tool_result",{"name":name,"result":result,"round":round_index+1})
            round_index+=1
            if not done_mode: break
            if MAX_TOOL_ROUNDS and round_index>=MAX_TOOL_ROUNDS:
                yield sse("status",{"stage":"limit","message":f"Jarvis reached the configured tool-round limit ({MAX_TOOL_ROUNDS}). Set JARVIS_MAX_TOOL_ROUNDS=0 for uncapped completion mode."})
                break
            await asyncio.sleep(0)
        except HTTPException as exc:
            yield sse("error",{"error":str(exc.detail)[:3000],"request_id":request_id}); return
        except (httpx.HTTPError,RuntimeError,ValueError,KeyError) as exc:
            yield sse("error",{"error":str(exc)[:3000],"request_id":request_id}); return
    elapsed=round(time.perf_counter()-started,3)
    yield sse("done",{"success":True,"request_id":request_id,"time_taken":elapsed,"tool_rounds":round_index,"done_mode":done_mode})


@app.get("/api/health")
async def health() -> dict[str,Any]:
    return {"success":True,"status":"online","version":APP_VERSION,"providers":len(PROVIDER_PRESETS),"configured_providers":len(PROVIDERS)}


@app.get("/api/service")
async def service() -> dict[str,Any]:
    return {"success":True,"name":"Jarvis","version":APP_VERSION,"features":["provider discovery","online + local providers","custom endpoints","live SSE","tool loop","done mode","30-chat history","skills","sandboxed workspace","Android-ready web app"]}


@app.get("/api/provider-presets")
async def provider_presets() -> dict[str,Any]:
    return {"success":True,"count":len(PROVIDER_PRESETS),"providers":PROVIDER_PRESETS}


@app.get("/api/providers",response_model=list[ProviderPublic])
async def list_providers() -> list[ProviderPublic]:
    return [public_provider(p) for p in PROVIDERS.values()]


@app.post("/api/providers",response_model=ProviderPublic)
async def register_provider(provider: Provider, request: Request) -> ProviderPublic:
    rate_limit(request); check_provider_url(provider)
    PROVIDERS[provider_id(provider.name)]=provider; save_provider_config()
    return public_provider(provider)


@app.delete("/api/providers/{name}")
async def remove_provider(name: str, request: Request) -> dict[str,Any]:
    rate_limit(request); PROVIDERS.pop(name,None); save_provider_config(); return {"success":True}


@app.get("/api/providers/{name}/models")
async def get_models(name: str, request: Request) -> dict[str,Any]:
    rate_limit(request); provider=PROVIDERS.get(name)
    if not provider: raise HTTPException(404,"Provider not found")
    return {"success":True,"models":await provider_models(provider)}


@app.get("/api/tools")
async def get_tools() -> dict[str,Any]:
    return {"success":True,"tools":tool_schemas(),"workspace":"."}


@app.get("/api/commands")
async def get_commands() -> dict[str,Any]:
    return {"success":True,"commands":COMMANDS}


@app.get("/api/commands/suggest")
async def suggest_commands(q: str = "") -> dict[str,Any]:
    return {"success":True,"suggestions":command_suggestions(q)}


@app.get("/api/skills")
async def get_skills() -> dict[str,Any]:
    return {"success":True,"skills":list_skills()}


@app.get("/api/skills/context")
async def skill_context(q: str = "") -> dict[str,Any]:
    return {"success":True,"skills":select_skills(q)}


@app.get("/api/history")
async def history_list() -> dict[str,Any]:
    return {"success":True,"limit":30,"chats":list_chats()}


@app.get("/api/history/{chat_id}")
async def history_get(chat_id: str) -> dict[str,Any]:
    item=get_chat(chat_id)
    if not item: raise HTTPException(404,"Chat not found")
    return {"success":True,"chat":item}


@app.post("/api/history")
async def history_save(item: HistoryItem, request: Request) -> dict[str,Any]:
    rate_limit(request)
    saved=upsert_chat(item.model_dump()); return {"success":True,"chat":saved,"limit":30}


@app.delete("/api/history/{chat_id}")
async def history_delete(chat_id: str, request: Request) -> dict[str,Any]:
    rate_limit(request); return {"success":delete_chat(chat_id)}


@app.post("/api/chat/stream")
async def chat_stream(chat_request: ChatRequest, request: Request) -> StreamingResponse:
    rate_limit(request); request_id=secrets.token_urlsafe(12)
    return StreamingResponse(chat_events(chat_request,request_id),media_type="text/event-stream",headers={"Cache-Control":"no-cache, no-transform","Connection":"keep-alive","X-Request-ID":request_id,"X-Accel-Buffering":"no"})


@app.post("/api/chat")
async def chat(chat_request: ChatRequest, request: Request) -> JSONResponse:
    rate_limit(request); request_id=secrets.token_urlsafe(12); provider=PROVIDERS.get(chat_request.provider)
    if not provider: raise HTTPException(404,"Provider not found")
    started=time.perf_counter(); result=await complete_provider(provider,chat_request,chat_request.messages); elapsed=round(time.perf_counter()-started,3)
    choices=result.get("choices") or []; message=choices[0].get("message",{}) if choices else {"role":"assistant","content":""}
    return JSONResponse({"success":True,"request_id":request_id,"model":chat_request.model,"provider":provider.name,"message":message,"usage":result.get("usage"),"time_taken":elapsed})


load_provider_config()
