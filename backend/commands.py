from __future__ import annotations
from difflib import SequenceMatcher

COMMANDS = [
    {'command':'/provider','description':'Switch to a provider'},
    {'command':'/model','description':'Switch to a discovered model'},
    {'command':'/new','description':'Start a new chat'},
    {'command':'/history','description':'Open saved chats'},
    {'command':'/clear','description':'Clear the current chat'},
    {'command':'/settings','description':'Open Jarvis settings'},
    {'command':'/skills','description':'Show available skills'},
    {'command':'/help','description':'Show all commands'},
    {'command':'/done','description':'Keep building until the task is complete'},
]

def command_suggestions(text: str) -> list[dict[str, str]]:
    q = text.strip().split()[0].lower() if text.strip().startswith('/') else ''
    if not q: return []
    scored=[]
    for item in COMMANDS:
        score=SequenceMatcher(None,q,item['command']).ratio()
        if item['command'].startswith(q): score += 0.7
        if score >= 0.35: scored.append((score,item))
    scored.sort(key=lambda x:x[0], reverse=True)
    return [x[1] for x in scored[:6]]

def parse_done(text: str) -> tuple[bool, str]:
    parts=text.strip().split()
    if not parts: return False, text
    markers={p.lower() for p in (parts[0], parts[-1])}
    if '/done' not in markers: return False, text
    clean=[p for p in parts if p.lower() != '/done']
    return True, ' '.join(clean).strip()
