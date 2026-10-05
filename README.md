# Jarvis

A fast, secure AI workspace built around real provider APIs.

## Included
- React + TypeScript + Vite frontend
- Python + FastAPI backend
- Live provider model discovery from `/models`-style APIs
- OpenAI-compatible JSON by default plus configurable generic JSON paths
- Streaming chat by default with server-sent events
- Multi-round AI tool loop: prompt → model → tool call → tool result → model → response
- Sandboxed workspace tools for listing, reading, writing and directory creation
- Optional destructive file tool behind an explicit backend environment flag
- Server-side provider API keys; keys are never returned to the frontend
- SSRF protection for provider targets, request rate limiting, size limits and timeouts

## Run

### Backend
```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python run.py
```

### Frontend
```bash
cd frontend
npm install
npm run build
npm run dev
```

Set `VITE_API_BASE` when the backend is not on `http://localhost:8000`.

## Provider compatibility

Jarvis does not hard-code a fake model catalog. Add a provider with its base URL and key, then Jarvis asks that provider for its actual model list. The default scheme uses OpenAI-style `/models` and `/chat/completions` JSON endpoints; custom paths can be supplied for other JSON-compatible APIs.

Local/private provider targets are blocked by default. Set `JARVIS_ALLOW_LOCAL_PROVIDERS=true` only for trusted development environments.

## Tool safety

AI tools are confined to `JARVIS_WORKSPACE`. Path traversal and symlinks are rejected, text files are size-limited, and arbitrary host shell execution is not exposed. Destructive deletion is disabled unless explicitly enabled with `JARVIS_ALLOW_DESTRUCTIVE_TOOLS=true`.
