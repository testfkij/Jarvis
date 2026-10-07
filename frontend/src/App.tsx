import { useEffect, useMemo, useRef, useState } from 'react'

type Provider = { id: string; name: string; base_url: string; scheme: string; models_path: string; chat_path: string; auth?: string }
type Preset = { id: string; name: string; base_url: string; models_path: string; chat_path: string; auth: string }
type Model = { id: string; name: string; owned_by?: string }
type Message = { role: 'user' | 'assistant'; content: string }
type EventRow = { type: 'tool_call' | 'tool_result'; name: string; data: unknown }

const DEFAULT_API = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || 'http://localhost:8000'

function App() {
  const [api, setApi] = useState(() => localStorage.getItem('jarvis_api') || DEFAULT_API)
  const [apiDraft, setApiDraft] = useState(api)
  const [providers, setProviders] = useState<Provider[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [activeProvider, setActiveProvider] = useState('')
  const [models, setModels] = useState<Model[]>([])
  const [model, setModel] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [events, setEvents] = useState<EventRow[]>([])
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<'online' | 'offline' | 'checking'>('checking')
  const [notice, setNotice] = useState('')
  const [showSetup, setShowSetup] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [scheme, setScheme] = useState('openai-compatible')
  const [modelsPath, setModelsPath] = useState('/models')
  const [chatPath, setChatPath] = useState('/chat/completions')
  const [auth, setAuth] = useState('bearer')
  const [presetId, setPresetId] = useState('')
  const [listening, setListening] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)

  const active = useMemo(() => providers.find(p => p.id === activeProvider), [providers, activeProvider])
  const canSend = Boolean(activeProvider && model && input.trim() && !busy)

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, events])

  useEffect(() => {
    const load = async () => {
      try {
        const [h, p, presetRes] = await Promise.all([fetch(`${api}/api/health`), fetch(`${api}/api/providers`), fetch(`${api}/api/provider-presets`)])
        setStatus(h.ok ? 'online' : 'offline')
        if (p.ok) {
          const data = await p.json() as Provider[]
          setProviders(data)
          if (data[0]) { setActiveProvider(data[0].id); void loadModels(data[0].id) }
        }
        if (presetRes.ok) setPresets(((await presetRes.json()) as { providers: Preset[] }).providers || [])
      } catch { setStatus('offline') }
    }
    void load()
  }, [api])

  async function loadModels(providerId: string) {
    if (!providerId) return
    setNotice('')
    try {
      const res = await fetch(`${api}/api/providers/${encodeURIComponent(providerId)}/models`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Model discovery failed')
      const discovered = (data.models || []) as Model[]
      setModels(discovered); setModel(discovered[0]?.id || '')
      if (!discovered.length) setNotice('No models were returned. This provider may require a model ID manually.')
    } catch (error) { setModels([]); setModel(''); setNotice(error instanceof Error ? error.message : 'Model discovery failed') }
  }

  function applyPreset(id: string) {
    const p = presets.find(x => x.id === id)
    if (!p) return
    setPresetId(id); setName(p.name); setBaseUrl(p.base_url); setModelsPath(p.models_path); setChatPath(p.chat_path); setAuth(p.auth)
  }

  async function addProvider(event: React.FormEvent) {
    event.preventDefault(); setNotice('')
    try {
      const res = await fetch(`${api}/api/providers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), base_url: baseUrl.trim(), api_key: apiKey.trim(), scheme, models_path: modelsPath.trim(), chat_path: chatPath.trim(), auth }) })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Could not add provider')
      setProviders(prev => [...prev.filter(p => p.id !== data.id), data as Provider]); setActiveProvider(data.id)
      setApiKey(''); setName(''); setPresetId(''); setShowSetup(false); await loadModels(data.id)
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Could not add provider') }
  }

  function saveApi() {
    const next = apiDraft.trim().replace(/\/$/, '')
    if (!/^https?:\/\//i.test(next)) { setNotice('Backend URL must start with http:// or https://'); return }
    localStorage.setItem('jarvis_api', next); setApi(next); setShowSettings(false); setNotice('Backend URL saved.')
  }

  async function send() {
    if (!canSend) return
    const prompt = input.trim(); setInput(''); setBusy(true); setNotice('')
    setMessages(prev => [...prev, { role: 'user', content: prompt }, { role: 'assistant', content: '' }])
    const payload = { provider: activeProvider, model, messages: [...messages, { role: 'user', content: prompt }], enable_tools: true }
    try {
      const res = await fetch(`${api}/api/chat/stream`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      if (!res.ok || !res.body) throw new Error((await res.text()) || `Request failed (${res.status})`)
      const reader = res.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
      for (;;) {
        const { value, done } = await reader.read(); if (done) break
        buffer += decoder.decode(value, { stream: true }); const chunks = buffer.split('\n\n'); buffer = chunks.pop() || ''
        for (const chunk of chunks) {
          const eventMatch = chunk.match(/^event:\s*(.+)$/m); const dataMatch = chunk.match(/^data:\s*(.+)$/m); if (!dataMatch) continue
          const type = eventMatch?.[1] || 'message'; let data: any
          try { data = JSON.parse(dataMatch[1]) } catch { data = { text: dataMatch[1] } }
          if (type === 'delta' && typeof data.text === 'string') setMessages(prev => prev.map((m, i) => i === prev.length - 1 ? { ...m, content: m.content + data.text } : m))
          else if (type === 'tool_call' || type === 'tool_result') setEvents(prev => [...prev, { type, name: data.name || 'tool', data: data.arguments ?? data.result }])
          else if (type === 'error') setNotice(data.error || 'Jarvis request failed')
        }
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : 'Request failed'; setNotice(text)
      setMessages(prev => prev.map((m, i) => i === prev.length - 1 && !m.content ? { ...m, content: 'I could not complete that request.' } : m))
    } finally { setBusy(false) }
  }

  function voice() {
    const Speech = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!Speech) { setNotice('Voice input is not supported by this browser.'); return }
    const recognition = new Speech(); recognition.lang = 'en-US'; recognition.interimResults = false
    recognition.onstart = () => setListening(true); recognition.onend = () => setListening(false); recognition.onerror = () => setListening(false)
    recognition.onresult = (event: any) => setInput(event.results[0][0].transcript); recognition.start()
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">J</div><div><div className="brand-name">JARVIS</div><div className="brand-sub">AI workspace · v2</div></div></div>
      <div className="side-card"><div className="side-label">Connection</div><div><span className={`status-dot ${status}`}></span><span>{status === 'online' ? 'Backend online' : status === 'checking' ? 'Checking backend' : 'Backend offline'}</span></div></div>
      <div className="side-title">Provider</div>
      <select className="field" value={activeProvider} onChange={e => { setActiveProvider(e.target.value); void loadModels(e.target.value) }}><option value="">Choose provider</option>{providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
      <button className="secondary" onClick={() => setShowSetup(true)}>+ Add provider</button>
      <div className="side-title model-title">Model</div>
      <select className="field" value={model} disabled={!models.length || busy} onChange={e => setModel(e.target.value)}><option value="">Select model</option>{models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select>
      <button className="text-btn" disabled={!activeProvider || busy} onClick={() => void loadModels(activeProvider)}>↻ Refresh models</button>
      <div className="tool-box"><div className="tool-title">Built-in workspace</div><div className="chips"><span>26 providers</span><span>Streaming</span><span>Tool loop</span><span>Files</span><span>Folders</span></div><p>Provider keys stay on the backend. Workspace tools are sandboxed.</p></div>
      <div className="side-footer">FastAPI · React · TypeScript<br/><button className="text-btn" onClick={() => setShowSettings(true)}>⚙ Backend settings</button></div>
    </aside>
    <main className="main">
      <header className="topbar"><div><div className="eyebrow">PERSONAL AI SYSTEM</div><h1>What can Jarvis do?</h1></div><div className="top-actions"><button className="icon-btn" title="Voice input" onClick={voice}>{listening ? '◉' : '◌'}</button><button className="secondary small" onClick={() => setShowSettings(true)}>Settings</button><button className="secondary small" onClick={() => setEvents([])}>Clear activity</button></div></header>
      <section className="workspace"><div className="conversation">
        {!messages.length && <div className="welcome"><div className="orb">J</div><h2>Ready when you are.</h2><p>Connect one of 26 provider presets, discover live models, then chat with streaming and sandboxed tools.</p><div className="quick"><button onClick={() => setInput('Explain what you can do in this workspace.')}>Capabilities</button><button onClick={() => setInput('List the files in the workspace.')}>Inspect workspace</button><button onClick={() => setShowSetup(true)}>Connect provider</button></div></div>}
        {messages.map((m, i) => <article key={i} className={`message ${m.role}`}><div className="avatar">{m.role === 'user' ? 'U' : 'J'}</div><div className="bubble"><div className="msg-role">{m.role === 'user' ? 'You' : 'Jarvis'}</div><div className="msg-content">{m.content || (busy && i === messages.length - 1 ? <span className="typing">Thinking<span>.</span><span>.</span><span>.</span></span> : '')}</div></div></article>)}<div ref={bottom}/>
      </div><aside className="activity"><div className="activity-head"><div><div className="side-label">Live activity</div><strong>Tool execution</strong></div><span>{events.length}</span></div>{events.length ? events.map((e, i) => <div className="event" key={i}><div className={`event-icon ${e.type === 'tool_call' ? 'call' : 'result'}`}>{e.type === 'tool_call' ? '→' : '✓'}</div><div><div className="event-name">{e.name}</div><pre>{JSON.stringify(e.data, null, 2)}</pre></div></div>) : <div className="empty-activity">No tool calls yet.<br/>Jarvis will show each call and result here.</div>}</aside></section>
      {notice && <div className="notice">{notice}</div>}
      <div className="composer"><div className="composer-row"><textarea value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} placeholder={activeProvider ? 'Message Jarvis…' : 'Connect a provider to start…'} disabled={busy}/><button className="send" disabled={!canSend} onClick={() => void send()}>{busy ? '…' : 'Send'}<span>↗</span></button></div><div className="composer-foot"><span>Enter to send · Shift+Enter for newline</span><span>{active ? `${active.name} · ${model || 'no model'}` : 'No provider'}</span></div></div>
    </main>

    {showSetup && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setShowSetup(false) }}><form className="modal" onSubmit={addProvider}><div className="modal-head"><div><div className="eyebrow">PROVIDER CONNECTION</div><h3>Add provider</h3></div><button type="button" className="icon-btn" onClick={() => setShowSetup(false)}>×</button></div>
      <label>Provider preset<select className="field" value={presetId} onChange={e => applyPreset(e.target.value)}><option value="">Custom provider</option>{presets.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label>Name<input className="field" value={name} onChange={e => setName(e.target.value)} required placeholder="My Provider"/></label><label>Base URL<input className="field" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} required placeholder="https://api.example.com/v1"/></label><label>API key<input className="field" type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} required placeholder="Stored only in the backend"/></label>
      <div className="path-grid"><label>Models path<input className="field" value={modelsPath} onChange={e => setModelsPath(e.target.value)} /></label><label>Chat path<input className="field" value={chatPath} onChange={e => setChatPath(e.target.value)} /></label></div>
      <label>Authentication<select className="field" value={auth} onChange={e => setAuth(e.target.value)}><option value="bearer">Bearer token</option><option value="x-api-key">x-api-key</option></select></label>
      <div className="modal-note">26 presets are included. Jarvis uses the provider's real model list when available; no fake model catalog is shipped.</div><button className="primary" type="submit">Save & discover models</button></form></div>}
    {showSettings && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setShowSettings(false) }}><form className="modal" onSubmit={e => { e.preventDefault(); saveApi() }}><div className="modal-head"><div><div className="eyebrow">APP SETTINGS</div><h3>Backend connection</h3></div><button type="button" className="icon-btn" onClick={() => setShowSettings(false)}>×</button></div><label>Backend URL<input className="field" value={apiDraft} onChange={e => setApiDraft(e.target.value)} placeholder="https://your-jarvis-api.example.com"/></label><div className="modal-note">The URL is stored locally on this device. Your provider API keys remain on the backend.</div><button className="primary" type="submit">Save backend</button></form></div>}
  </div>
}

export default App
