from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any

HISTORY_FILE = Path(os.getenv('JARVIS_HISTORY_FILE', './history.json')).resolve()
MAX_CHATS = 30
MAX_MESSAGES = 200

def _load() -> list[dict[str, Any]]:
    if not HISTORY_FILE.is_file(): return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError): return []

def _save(items: list[dict[str, Any]]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(items[:MAX_CHATS], ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, HISTORY_FILE)
    try: os.chmod(HISTORY_FILE, 0o600)
    except OSError: pass

def list_chats() -> list[dict[str, Any]]:
    items = _load()
    items.sort(key=lambda x: str(x.get('updated_at','')), reverse=True)
    return items[:MAX_CHATS]

def upsert_chat(item: dict[str, Any]) -> dict[str, Any]:
    chats = [x for x in _load() if x.get('id') != item.get('id')]
    item['messages'] = list(item.get('messages', []))[-MAX_MESSAGES:]
    chats.insert(0, item)
    _save(chats)
    return item

def get_chat(chat_id: str) -> dict[str, Any] | None:
    return next((x for x in _load() if x.get('id') == chat_id), None)

def delete_chat(chat_id: str) -> bool:
    before = _load(); after = [x for x in before if x.get('id') != chat_id]
    if len(before) == len(after): return False
    _save(after); return True
