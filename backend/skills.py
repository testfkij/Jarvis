from __future__ import annotations
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent / 'skills'

def list_skills() -> list[dict[str, str]]:
    out=[]
    for path in sorted(ROOT.glob('*.md')):
        first=''
        try:
            text=path.read_text(encoding='utf-8')
            first=next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('#')), path.stem)
        except OSError: first=path.stem
        out.append({'id':path.stem,'name':first,'file':path.name})
    return out

def select_skills(text: str, limit: int = 3) -> list[str]:
    query=text.lower(); chosen=[]
    keywords={
        'android':['android','apk','gradle','manifest','permission','mobile'],
        'web':['website','frontend','tsx','react','vite','css','ui','browser'],
        'backend':['backend','fastapi','api','server','database','python'],
        'app-development':['app','application','build','developer','project'],
        'research':['research','latest','compare','look up','docs'],
        'security':['secure','security','ssrf','auth','token','secret'],
    }
    for sid, words in keywords.items():
        score=sum(1 for w in words if re.search(r'\b'+re.escape(w)+r'\b', query))
        if score: chosen.append((score,sid))
    chosen.sort(reverse=True)
    result=[]
    for _,sid in chosen[:limit]:
        path=ROOT/f'{sid}.md'
        if path.is_file(): result.append(path.read_text(encoding='utf-8'))
    return result
