import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  collection, onSnapshot, addDoc, updateDoc, deleteDoc,
  doc, serverTimestamp, writeBatch
} from 'firebase/firestore'
import { signInWithPopup, signOut, onAuthStateChanged } from 'firebase/auth'
import { db, auth, provider } from './firebase.js'
import { csvEscape, safeUrl, normalizeSuggestionUrl } from './security.js'
import {
  Search, Download, Upload, BarChart2, Bot,
  ChevronRight, X, LogIn, LogOut, Pencil, Trash2,
  CheckCircle, Send, PlusCircle, Inbox, Link2, UserRound, Globe, ShieldAlert
} from 'lucide-react'

// ── Constants ──────────────────────────────────────────────────────────────

const NOVELTY_COLORS = {
  'highly novel':   'bg-emerald-900 text-emerald-300',
  'novel':          'bg-blue-900 text-blue-300',
  'possibly novel': 'bg-sky-900 text-sky-300',
  'common':         'bg-gray-700 text-gray-400',
}
const novColor = n => {
  if (!n) return 'bg-gray-800 text-gray-400'
  const key = Object.keys(NOVELTY_COLORS).find(k => n.toLowerCase().startsWith(k))
  return NOVELTY_COLORS[key] || 'bg-gray-800 text-gray-400'
}
const UNC_COLORS = { low: 'text-emerald-400', medium: 'text-amber-400', high: 'text-red-400' }
const uncColor = u => UNC_COLORS[u] || 'text-gray-400'
const ALL_FIELDS = ['category','sourceUser','description','refUrls','tweetDate','searchDate','notes','uncertainty','novelty']
const FUNCTIONS_BASE_URL = `https://us-central1-${import.meta.env.VITE_FIREBASE_PROJECT_ID}.cloudfunctions.net`

// ── Search helpers ─────────────────────────────────────────────────────────

function parseSearchTokens(q) {
  const tokens = []
  for (const m of q.matchAll(/"([^"]*)"|(\S+)/g)) {
    if (m[1] !== undefined) tokens.push({ text: m[1], phrase: true })
    else                    tokens.push({ text: m[2], phrase: false })
  }
  return tokens
}

function normalize(s) {
  return s.toLowerCase().replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim()
}

function matchRecord(rec, tokens, mode) {
  if (!tokens.length) return true
  const haystack = normalize(ALL_FIELDS.map(f => String(rec[f] ?? '')).join(' '))
  const check = tok => {
    const norm = normalize(tok.text)
    if (tok.phrase) {
      return haystack.includes(norm)
    } else {
      // treat hyphenated bare words as separate terms, all must appear
      return norm.split(' ').filter(Boolean).every(w => haystack.includes(w))
    }
  }
  return mode === 'or' ? tokens.some(check) : tokens.every(check)
}

function downloadCsv(records) {
  const header = ['id','category','sourceUser','description','refUrls','tweetDate','searchDate','notes','uncertainty','novelty']
  const rows = [header, ...records.map(r => header.map(h => csvEscape(r[h])))]
  const blob = new Blob([rows.map(r => r.join(',')).join('\n')], { type: 'text/csv' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'openclaw-usecases.csv'
  a.click()
}

const CSV_COL_MAP = {
  'category': 'category',
  'source user': 'sourceUser', 'sourceuser': 'sourceUser',
  'description': 'description', 'one-sentence description': 'description',
  'reference urls': 'refUrls', 'refurls': 'refUrls',
  'tweet date': 'tweetDate', 'tweetdate': 'tweetDate',
  'search date': 'searchDate', 'searchdate': 'searchDate',
  'notes': 'notes',
  'uncertainty': 'uncertainty', 'uncertainty / confidence': 'uncertainty',
  'novelty': 'novelty', 'novelty / already documented': 'novelty',
}

function parseCSV(text) {
  const lines = text.split('\n').filter(Boolean)
  if (!lines.length) return []
  const headers = lines[0].split(',').map(h => h.trim().toLowerCase().replace(/"/g, ''))
  return lines.slice(1).map(line => {
    const vals = []; let cur = '', inQ = false
    for (const ch of line) {
      if (ch === '"') inQ = !inQ
      else if (ch === ',' && !inQ) { vals.push(cur); cur = '' }
      else cur += ch
    }
    vals.push(cur)
    const row = {}
    headers.forEach((h, i) => {
      const k = CSV_COL_MAP[h] || h
      row[k] = (vals[i] ?? '').trim()
    })
    return row
  }).filter(r => r.category || r.description)
}

async function callSuggestionEndpoint(path, body, bearerToken) {
  const response = await fetch(`${FUNCTIONS_BASE_URL}/${path}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(bearerToken ? { authorization: `Bearer ${bearerToken}` } : {}),
    },
    body: JSON.stringify(body),
  })
  let data = {}
  try {
    data = await response.json()
  } catch {
    data = {}
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || response.statusText || 'Request failed')
  }
  return data
}

// ── Main App ───────────────────────────────────────────────────────────────

export default function App() {
  const [user,       setUser]       = useState(undefined)
  const [records,    setRecords]    = useState([])
  const [q,          setQ]          = useState('')
  const [matchMode,  setMatchMode]  = useState('and')
  const [filterCat,  setFilterCat]  = useState('')
  const [filterUnc,  setFilterUnc]  = useState('')
  const [filterNov,  setFilterNov]  = useState('')
  const [sort,       setSort]       = useState({ field: 'tweetDate', dir: 'desc' })
  const [selected,   setSelected]   = useState(null)
  const [editing,    setEditing]    = useState(null)
  const [triageOpen, setTriageOpen] = useState(false)
  const [statsOpen,  setStatsOpen]  = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [suggestionOpen, setSuggestionOpen] = useState(false)
  const [selIds,     setSelIds]     = useState(new Set())
  const [toast,      setToast]      = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const canWrite = user?.email === 'david@prismism.com'

  useEffect(() => onAuthStateChanged(auth, setUser), [])

  useEffect(() => {
    const unsub = onSnapshot(collection(db, 'use_cases'), snap => {
      const docs = snap.docs.map(d => ({ id: d.id, ...d.data() }))
      // Assign stable sequential IDs sorted by tweetDate then createdAt
      docs.sort((a, b) => {
        const td = (a.tweetDate ?? '').localeCompare(b.tweetDate ?? '')
        if (td !== 0) return td
        const ca = a.createdAt?.toMillis?.() ?? 0
        const cb = b.createdAt?.toMillis?.() ?? 0
        return ca - cb
      })
      docs.forEach((d, i) => { d.seqId = i + 1 })
      setRecords(docs)
    })
    return unsub
  }, [])

  useEffect(() => {
    const unsub = onSnapshot(collection(db, 'suggestion_queue'), snap => {
      const docs = snap.docs.map(d => ({ id: d.id, ...d.data() }))
      docs.sort((a, b) => {
        const ca = a.createdAt?.toMillis?.() ?? 0
        const cb = b.createdAt?.toMillis?.() ?? 0
        return cb - ca
      })
      setSuggestions(docs)
    })
    return unsub
  }, [])

  const showToast = useCallback((msg, type = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }, [])

  const categories = useMemo(() => [...new Set(records.map(r => r.category).filter(Boolean))].sort(), [records])
  const novelties  = useMemo(() => [...new Set(records.map(r => r.novelty).filter(Boolean))].sort(), [records])

  const filtered = useMemo(() => {
    const tokens = parseSearchTokens(q.trim())
    let r = records
    if (tokens.length) r = r.filter(rec => matchRecord(rec, tokens, matchMode))
    if (filterCat) r = r.filter(rec => rec.category === filterCat)
    if (filterUnc) r = r.filter(rec => rec.uncertainty === filterUnc)
    if (filterNov) r = r.filter(rec => rec.novelty === filterNov)
    const { field, dir } = sort
    return [...r].sort((a, b) => {
      const av = a[field] ?? '', bv = b[field] ?? ''
      // Numeric sort for seqId and any number fields
      if (typeof av === 'number' || typeof bv === 'number' || field === 'seqId') {
        const an = Number(av), bn = Number(bv)
        return dir === 'asc' ? an - bn : bn - an
      }
      return dir === 'asc'
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av))
    })
  }, [records, q, matchMode, filterCat, filterUnc, filterNov, sort])

  function toggleSort(field) {
    setSort(s => s.field === field
      ? { field, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { field, dir: 'asc' })
  }

  function toggleSelId(id) {
    setSelIds(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  }

  function toggleSelectAll() {
    if (selIds.size === filtered.length && filtered.length > 0) {
      setSelIds(new Set())
    } else {
      setSelIds(new Set(filtered.map(r => r.id)))
    }
  }

  async function batchDelete() {
    if (!canWrite || selIds.size === 0) return
    if (!confirm(`Delete ${selIds.size} record${selIds.size > 1 ? 's' : ''}? This cannot be undone.`)) return
    const batch = writeBatch(db)
    selIds.forEach(id => batch.delete(doc(db, 'use_cases', id)))
    await batch.commit()
    if (selected && selIds.has(selected.id)) setSelected(null)
    setSelIds(new Set())
    showToast(`Deleted ${selIds.size} records`, 'error')
  }

  async function saveRecord(data) {
    if (!canWrite) return
    const { id, seqId, createdAt, ...payload } = data
    payload.updatedAt = serverTimestamp()
    if (id) {
      await updateDoc(doc(db, 'use_cases', id), payload)
      showToast('Saved')
    } else {
      payload.createdAt = serverTimestamp()
      await addDoc(collection(db, 'use_cases'), payload)
      showToast('Created')
    }
    setEditing(null)
  }

  async function deleteRecord(id) {
    if (!canWrite || !confirm('Delete this record?')) return
    await deleteDoc(doc(db, 'use_cases', id))
    if (selected?.id === id) setSelected(null)
    showToast('Deleted', 'error')
  }

  const signIn = () => signInWithPopup(auth, provider).catch(() => {})
  const signOutUser = () => signOut(auth)

  if (user === undefined) return (
    <div className="flex h-screen items-center justify-center text-gray-500">Loading…</div>
  )

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-950 text-gray-100">

      {/* Navbar */}
      <nav className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800 shrink-0">
        <span className="font-bold text-blue-400 text-sm">🦞 OpenClaw Explorer</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-600">{filtered.length}/{records.length}</span>
          <button onClick={() => setSuggestionOpen(true)}
            className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-800 hover:bg-emerald-700 text-xs font-medium">
            <PlusCircle size={12}/> Suggest URL
          </button>
          {selIds.size > 0 && canWrite && (
            <button onClick={batchDelete}
              className="flex items-center gap-1 px-2 py-1 rounded bg-red-800 hover:bg-red-700 text-xs font-medium">
              <Trash2 size={12}/> Delete {selIds.size}
            </button>
          )}
          {selIds.size > 0 && (
            <button onClick={() => setSelIds(new Set())}
              className="text-xs text-gray-500 hover:text-gray-300 px-1">
              Clear
            </button>
          )}
          <IconBtn onClick={() => setStatsOpen(true)}   title="Stats"><BarChart2 size={14}/></IconBtn>
          {canWrite && <IconBtn onClick={() => setImportOpen(true)}  title="Import CSV"><Upload size={14}/></IconBtn>}
          <IconBtn onClick={() => downloadCsv(filtered)} title="Export CSV"><Download size={14}/></IconBtn>
          <button onClick={() => setTriageOpen(t => !t)}
            className="flex items-center gap-1 px-2 py-1 rounded bg-blue-800 hover:bg-blue-700 text-xs font-medium">
            <Bot size={12}/> Triage
          </button>
          {user
            ? <IconBtn onClick={signOutUser} title={user.email}><LogOut size={14}/></IconBtn>
            : <button onClick={signIn}
                className="flex items-center gap-1 px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-xs">
                <LogIn size={12}/> Login
              </button>
          }
        </div>
      </nav>

      <div className="flex flex-1 overflow-hidden">

        {/* Sidebar */}
        <aside className="w-44 shrink-0 border-r border-gray-800 p-3 flex flex-col gap-3 overflow-y-auto bg-gray-900">
          <FilterSelect label="Category"    value={filterCat} onChange={setFilterCat} options={categories}/>
          <FilterSelect label="Uncertainty" value={filterUnc} onChange={setFilterUnc}
            options={['low','medium','high']}/>
          <FilterSelect label="Novelty"     value={filterNov} onChange={setFilterNov} options={novelties}/>
          <div>
            <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Sort</label>
            <select value={sort.field}
              onChange={e => setSort(s => ({ ...s, field: e.target.value }))}
              className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs mb-1">
              {[['tweetDate','Tweet date'],['searchDate','Search date'],['category','Category'],
                ['uncertainty','Uncertainty'],['novelty','Novelty'],['sourceUser','Source']
              ].map(([v,l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <select value={sort.dir}
              onChange={e => setSort(s => ({ ...s, dir: e.target.value }))}
              className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs">
              <option value="desc">↓ Desc</option>
              <option value="asc">↑ Asc</option>
            </select>
          </div>
          <button
            onClick={() => { setFilterCat(''); setFilterUnc(''); setFilterNov(''); setQ('') }}
            className="text-xs text-gray-600 hover:text-gray-400 text-left">
            Clear filters
          </button>
          {canWrite && (
            <button
              onClick={() => { setEditing({ category:'', sourceUser:'', description:'', refUrls:'', tweetDate:'', searchDate:'', notes:'', uncertainty:'medium', novelty:'novel' }); setSelected(null) }}
              className="mt-auto px-2 py-1.5 rounded bg-emerald-900 hover:bg-emerald-800 text-xs text-center font-medium">
              + New record
            </button>
          )}
        </aside>

        {/* Main area */}
        <div className="flex flex-col flex-1 overflow-hidden">

          {/* Search */}
          <div className="px-3 py-2 border-b border-gray-800 bg-gray-900 shrink-0 flex items-center gap-2">
            <div className="relative flex-1">
              <Search size={12} className="absolute left-2.5 top-2 text-gray-500"/>
              <input value={q} onChange={e => setQ(e.target.value)}
                placeholder='Search… ("phrase" or words)'
                className="w-full pl-7 pr-3 py-1.5 bg-gray-800 rounded border border-gray-700 text-xs focus:outline-none focus:border-blue-600"/>
            </div>
            <div className="flex rounded overflow-hidden border border-gray-700 shrink-0 text-[10px] font-medium">
              {['and','or'].map(m => (
                <button key={m} onClick={() => setMatchMode(m)}
                  className={`px-2 py-1 uppercase ${matchMode === m ? 'bg-blue-700 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'}`}>
                  {m}
                </button>
              ))}
            </div>
          </div>

          {/* Table + detail */}
          <div className="flex flex-1 overflow-hidden">
            <div className="flex-1 overflow-auto">
              <table className="w-full text-xs border-collapse">
                <thead className="sticky top-0 bg-gray-900 z-10">
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="pl-2 pr-1 py-2 w-14 whitespace-nowrap">
                      <div className="flex items-center gap-1">
                        <input type="checkbox" className="accent-blue-500 cursor-pointer"
                          checked={selIds.size === filtered.length && filtered.length > 0}
                          onChange={toggleSelectAll}/>
                        <button onClick={() => toggleSort('seqId')}
                          className="text-gray-500 hover:text-gray-300 font-medium">
                          #{sort.field === 'seqId' ? (sort.dir === 'asc' ? '↑' : '↓') : ''}
                        </button>
                      </div>
                    </th>
                    <Th label="Category" field="category" sort={sort} toggle={toggleSort} cls="w-36"/>
                    <Th label="Source"  field="sourceUser" sort={sort} toggle={toggleSort} cls="w-32"/>
                    <th className="px-2 py-2 text-left">Description</th>
                    <Th label="Tweet"  field="tweetDate"  sort={sort} toggle={toggleSort} cls="w-20"/>
                    <Th label="Unc."   field="uncertainty" sort={sort} toggle={toggleSort} cls="w-12"/>
                    <Th label="Novelty" field="novelty"   sort={sort} toggle={toggleSort} cls="w-28"/>
                    <th className="w-6"/>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 && (
                    <tr><td colSpan={9} className="text-center py-16 text-gray-700">No records</td></tr>
                  )}
                  {filtered.map(r => (
                    <tr key={r.id}
                      onClick={() => { setSelected(r); setEditing(null) }}
                      className={`border-b border-gray-800/50 cursor-pointer hover:bg-gray-800/30 transition-colors
                        ${selected?.id === r.id ? 'bg-blue-950/40' : ''}
                        ${selIds.has(r.id) ? 'bg-blue-950/20' : ''}`}>
                      <td className="pl-2 pr-1 py-1.5 whitespace-nowrap" onClick={e => e.stopPropagation()}>
                        <div className="flex items-center gap-1">
                          <input type="checkbox" className="accent-blue-500 cursor-pointer"
                            checked={selIds.has(r.id)}
                            onChange={() => toggleSelId(r.id)}/>
                          <span className="font-mono text-blue-500/70">{r.seqId}</span>
                        </div>
                      </td>
                      <td className="px-2 py-1.5 text-gray-300 max-w-[144px] truncate">{r.category}</td>
                      <td className="px-2 py-1.5 text-gray-400 max-w-[128px] truncate">{r.sourceUser}</td>
                      <td className="px-2 py-1.5 text-gray-300 max-w-xs truncate">{r.description}</td>
                      <td className="px-2 py-1.5 text-gray-600 whitespace-nowrap">{r.tweetDate}</td>
                      <td className={`px-2 py-1.5 font-medium ${uncColor(r.uncertainty)}`}>{r.uncertainty}</td>
                      <td className="px-2 py-1.5">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${novColor(r.novelty)}`}>
                          {r.novelty}
                        </span>
                      </td>
                      <td className="pr-1">
                        <button onClick={e => { e.stopPropagation(); setSelected(r); setEditing(null) }}
                          className="text-gray-700 hover:text-gray-400 p-0.5">
                          <ChevronRight size={12}/>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Detail / edit panel */}
            {(selected && !editing) && (
              <DetailPanel key={selected.id} record={selected}
                onEdit={() => setEditing({ ...selected })}
                onDelete={deleteRecord}
                onClose={() => setSelected(null)}
                onToggleSel={() => toggleSelId(selected.id)}
                isSelected={selIds.has(selected.id)}
                canWrite={canWrite}/>
            )}
            {editing && (
              <EditPanel key={editing.id ?? 'new'} record={editing}
                onSave={saveRecord}
                onCancel={() => setEditing(null)}/>
            )}
          </div>

          {/* Triage */}
          {triageOpen && (
            <TriagePanel
              records={filtered}
              selIds={selIds}
              onClose={() => setTriageOpen(false)}
              showToast={showToast}/>
          )}
        </div>
      </div>

      {statsOpen  && <StatsModal  records={records} onClose={() => setStatsOpen(false)}/>}
      {suggestionOpen && (
        <SuggestionModal
          user={user}
          suggestions={suggestions}
          onClose={() => setSuggestionOpen(false)}
          showToast={showToast}
        />
      )}
      {importOpen && <ImportModal onClose={() => setImportOpen(false)} onImport={async rows => {
        if (!canWrite) { showToast('Sign in as owner to import', 'error'); return }
        const batch = writeBatch(db)
        rows.forEach(r => batch.set(doc(collection(db, 'use_cases')), {
          ...r, createdAt: serverTimestamp(), updatedAt: serverTimestamp()
        }))
        await batch.commit()
        showToast(`Imported ${rows.length} records`)
        setImportOpen(false)
      }}/>}

      {toast && (
        <div className={`fixed bottom-5 right-5 px-3 py-2 rounded shadow-xl text-xs z-50 font-medium
          ${toast.type === 'error' ? 'bg-red-800 text-red-100' : 'bg-emerald-800 text-emerald-100'}`}>
          {toast.msg}
        </div>
      )}
    </div>
  )
}

// ── Shared components ──────────────────────────────────────────────────────

function IconBtn({ onClick, title, children }) {
  return (
    <button onClick={onClick} title={title}
      className="p-1.5 rounded text-gray-400 hover:text-gray-200 hover:bg-gray-800">
      {children}
    </button>
  )
}

function Th({ label, field, sort, toggle, cls = '' }) {
  const active = sort.field === field
  return (
    <th onClick={() => toggle(field)}
      className={`px-2 py-2 text-left cursor-pointer select-none hover:text-gray-300
        ${active ? 'text-blue-400' : ''} ${cls}`}>
      {label}{active && (sort.dir === 'asc' ? ' ↑' : ' ↓')}
    </th>
  )
}

function FilterSelect({ label, value, onChange, options }) {
  return (
    <div>
      <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">{label}</label>
      <select value={value} onChange={e => onChange(e.target.value)}
        className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs">
        <option value="">All</option>
        {options.map(o => <option key={o}>{o}</option>)}
      </select>
    </div>
  )
}

// ── Detail Panel ───────────────────────────────────────────────────────────

function DetailPanel({ record: r, onEdit, onDelete, onClose, onToggleSel, isSelected, canWrite }) {
  const urls = (r.refUrls ?? '').split(',').map(u => u.trim()).filter(Boolean)
  return (
    <div className="w-96 shrink-0 border-l border-gray-800 overflow-y-auto bg-gray-900 p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-blue-400">#{r.seqId}</span>
        <div className="flex gap-1">
          <IconBtn onClick={onToggleSel} title="Toggle triage scope">
            <CheckCircle size={13} className={isSelected ? 'text-blue-400' : ''}/>
          </IconBtn>
          {canWrite && <IconBtn onClick={onEdit}><Pencil size={13}/></IconBtn>}
          {canWrite && (
            <IconBtn onClick={() => onDelete(r.id)}>
              <Trash2 size={13} className="text-red-500"/>
            </IconBtn>
          )}
          <IconBtn onClick={onClose}><X size={13}/></IconBtn>
        </div>
      </div>

      <div className="flex items-center gap-2 mb-3">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${novColor(r.novelty)}`}>{r.novelty}</span>
        <span className={`text-xs font-medium ${uncColor(r.uncertainty)}`}>{r.uncertainty}</span>
      </div>

      <h2 className="font-semibold text-gray-100 mb-1">{r.category}</h2>
      <p className="text-gray-400 text-xs mb-3">{r.sourceUser}</p>
      <p className="text-gray-200 text-sm leading-relaxed mb-4">{r.description}</p>

      {r.notes && (
        <div className="mb-4">
          <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-1.5 font-medium">Notes</p>
          <p className="text-gray-300 text-xs leading-relaxed whitespace-pre-wrap">{r.notes}</p>
        </div>
      )}

      <div className="flex gap-2 flex-wrap mb-4">
        <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 text-[10px]">tweet: {r.tweetDate || '?'}</span>
        <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 text-[10px]">found: {r.searchDate || '?'}</span>
      </div>

      {urls.map(u => {
        const href = safeUrl(u)
        return href ? (
          <a key={u} href={href} target="_blank" rel="noopener noreferrer"
            className="block text-blue-400 hover:text-blue-300 text-xs truncate mb-1">{u}</a>
        ) : (
          <span key={u} className="block text-gray-500 text-xs truncate mb-1">{u}</span>
        )
      })}
    </div>
  )
}

// ── Edit Panel ─────────────────────────────────────────────────────────────

function EditPanel({ record, onSave, onCancel }) {
  const [form, setForm] = useState({ ...record })
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="w-96 shrink-0 border-l border-gray-800 overflow-y-auto bg-gray-900 p-4">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold text-sm">{record.id ? `Edit #${record.seqId}` : 'New Record'}</h2>
        <IconBtn onClick={onCancel}><X size={13}/></IconBtn>
      </div>
      <div className="space-y-2.5">
        <FormField label="Category"    value={form.category}    onChange={v => set('category', v)}/>
        <FormField label="Source user" value={form.sourceUser}  onChange={v => set('sourceUser', v)}/>
        <FormField label="Description" value={form.description} onChange={v => set('description', v)} multiline rows={3}/>
        <FormField label="Reference URLs (comma-separated)" value={form.refUrls} onChange={v => set('refUrls', v)}/>
        <FormField label="Tweet date (YYYY-MM-DD)"  value={form.tweetDate}  onChange={v => set('tweetDate', v)}/>
        <FormField label="Search date (YYYY-MM-DD)" value={form.searchDate} onChange={v => set('searchDate', v)}/>
        <FormField label="Notes" value={form.notes} onChange={v => set('notes', v)} multiline rows={4}/>
        <div>
          <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Uncertainty</label>
          <select value={form.uncertainty} onChange={e => set('uncertainty', e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs">
            <option>low</option><option>medium</option><option>high</option>
          </select>
        </div>
        <FormField label="Novelty" value={form.novelty} onChange={v => set('novelty', v)}/>
      </div>
      <div className="flex gap-2 mt-4">
        <button onClick={() => onSave(form)}
          className="flex-1 py-1.5 rounded bg-blue-700 hover:bg-blue-600 text-xs font-medium">
          Save
        </button>
        <button onClick={onCancel}
          className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-xs">
          Cancel
        </button>
      </div>
    </div>
  )
}

function SuggestionModal({ user, suggestions, onClose, showToast }) {
  const [activeTab, setActiveTab] = useState('submit')
  const [browserId] = useState(() => {
    const existing = localStorage.getItem('oc_suggestion_browser_id')
    if (existing) return existing
    const created = crypto.randomUUID()
    localStorage.setItem('oc_suggestion_browser_id', created)
    return created
  })
  const [session, setSession] = useState(null)
  const [sessionRequested, setSessionRequested] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [form, setForm] = useState({
    url: '',
    displayName: user?.displayName ?? '',
    creditMode: user ? 'profile' : 'nickname',
    honeypot: '',
  })
  const [openedAt] = useState(() => Date.now())

  useEffect(() => {
    setForm(f => ({
      ...f,
      displayName: user?.displayName ?? f.displayName,
      creditMode: user ? (f.creditMode === 'anonymous' ? 'nickname' : f.creditMode) : f.creditMode,
    }))
  }, [user])

  useEffect(() => {
    let cancelled = false
    async function loadSession() {
      if (user || session || sessionLoading || sessionRequested) return
      setSessionLoading(true)
      try {
        const res = await callSuggestionEndpoint('requestSuggestionSessionHttp', { browserId })
        if (!cancelled) setSession(res)
      } catch (error) {
        if (!cancelled) showToast(error?.message || 'Anonymous session unavailable', 'error')
      } finally {
        if (!cancelled) {
          setSessionLoading(false)
          setSessionRequested(true)
        }
      }
    }
    loadSession()
    return () => { cancelled = true }
  }, [browserId, session, sessionLoading, sessionRequested, showToast, user])

  const queueCount = suggestions.length
  const publicName = form.creditMode === 'anonymous'
    ? 'Anonymous'
    : form.creditMode === 'profile'
      ? (user?.displayName || 'Contributor')
      : (form.displayName.trim() || user?.displayName || 'Contributor')

  async function ensureAnonymousSession() {
    if (user) return true
    if (session?.sessionId && session?.sessionToken) return session
    const res = await callSuggestionEndpoint('requestSuggestionSessionHttp', { browserId })
    setSession(res)
    return res
  }

  async function submit() {
    if (submitting) return
    const normalizedUrl = normalizeSuggestionUrl(form.url)
    if (!normalizedUrl) {
      showToast('Enter a valid http(s) URL', 'error')
      return
    }
    if (form.honeypot.trim()) {
      showToast('Submission blocked', 'error')
      return
    }

    setSubmitting(true)
    try {
      const anonymousSession = user ? null : await ensureAnonymousSession()
      const bearerToken = user ? await user.getIdToken() : null
      const res = await callSuggestionEndpoint('submitSuggestionHttp', {
        url: normalizedUrl,
        displayName: publicName,
        creditMode: form.creditMode,
        browserId,
        sessionId: anonymousSession?.sessionId ?? session?.sessionId ?? null,
        sessionToken: anonymousSession?.sessionToken ?? session?.sessionToken ?? null,
        formAgeMs: Date.now() - openedAt,
        honeypot: form.honeypot,
      }, bearerToken)
      showToast('Suggestion added to queue')
      setForm({
        url: '',
        displayName: user?.displayName ?? '',
        creditMode: user ? 'profile' : 'nickname',
        honeypot: '',
      })
      setActiveTab('queue')
      setSession(null)
    } catch (error) {
      showToast(error?.message ?? 'Submission failed', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl p-5 mx-4 max-h-[85vh] overflow-y-auto shadow-2xl max-w-3xl w-full"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold flex items-center gap-2"><Link2 size={14}/> Suggest a URL</h2>
          <IconBtn onClick={onClose}><X size={14}/></IconBtn>
        </div>

        <div className="flex gap-2 mb-4 text-xs">
          {['submit', 'queue'].map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3 py-1.5 rounded font-medium border ${activeTab === tab
                ? 'bg-blue-700 border-blue-500 text-white'
                : 'bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700'}`}>
              {tab === 'submit' ? 'Submit' : `Queue (${queueCount})`}
            </button>
          ))}
        </div>

        {activeTab === 'submit' ? (
          <div className="space-y-3">
            <p className="text-xs text-gray-400">
              Share a URL for review. Contributors can sign in for attribution, or submit anonymously with a rate-limited session.
            </p>

            <FormField label="URL" value={form.url} onChange={v => setForm(f => ({ ...f, url: v }))} />

            <div>
              <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Public credit</label>
              <select
                value={form.creditMode}
                onChange={e => setForm(f => ({ ...f, creditMode: e.target.value }))}
                className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs">
                <option value="profile">Use my account name</option>
                <option value="nickname">Use a nickname</option>
                <option value="anonymous">Anonymous</option>
              </select>
            </div>

            {form.creditMode === 'nickname' && (
              <FormField
                label="Nickname"
                value={form.displayName}
                onChange={v => setForm(f => ({ ...f, displayName: v }))}
              />
            )}

            <div className="flex items-center gap-2 text-xs text-gray-500">
              <ShieldAlert size={13} className="text-amber-400"/>
              <span>{user ? 'Signed-in submissions are verified by your login.' : sessionLoading ? 'Preparing anonymous session…' : 'Anonymous submissions are session-limited to reduce spam.'}</span>
            </div>

            <input
              value={form.honeypot}
              onChange={e => setForm(f => ({ ...f, honeypot: e.target.value }))}
              tabIndex={-1}
              autoComplete="off"
              aria-hidden="true"
              className="absolute left-[-9999px] opacity-0 pointer-events-none"
            />

            <div className="flex gap-2">
              <button
                onClick={submit}
                disabled={submitting}
                className="flex-1 py-1.5 rounded bg-blue-700 hover:bg-blue-600 text-xs font-medium disabled:opacity-40">
                {submitting ? 'Submitting…' : 'Submit suggestion'}
              </button>
              <button onClick={onClose} className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-xs">
                Close
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-gray-400">Public queue, read-only for everyone except the owner.</p>
            {suggestions.length === 0 ? (
              <div className="rounded border border-dashed border-gray-700 p-6 text-center text-xs text-gray-500">
                No suggestions yet.
              </div>
            ) : suggestions.map(item => (
              <div key={item.id} className="rounded border border-gray-800 bg-gray-950/40 p-3 text-xs flex gap-3 items-start">
                <div className="mt-0.5 text-emerald-400"><Inbox size={14}/></div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium text-gray-200 truncate">{item.displayName || 'Anonymous'}</span>
                    <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 uppercase text-[10px]">{item.status || 'pending'}</span>
                    {item.creditMode === 'anonymous' && <span className="text-gray-500">anonymous</span>}
                  </div>
                  <a
                    href={safeUrl(item.url) ?? item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="block mt-1 text-blue-400 hover:text-blue-300 break-all">
                    {item.url}
                  </a>
                  <div className="mt-1 text-[10px] text-gray-500 flex items-center gap-2">
                    <span>{item.createdAt?.toDate?.()?.toLocaleString?.() ?? 'pending timestamp'}</span>
                    {item.normalizedUrl && <span className="truncate">{item.normalizedUrl}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function FormField({ label, value, onChange, multiline, rows = 2 }) {
  const cls = "w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500 text-gray-100 resize-none"
  return (
    <div>
      <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">{label}</label>
      {multiline
        ? <textarea value={value ?? ''} onChange={e => onChange(e.target.value)} rows={rows} className={cls}/>
        : <input   value={value ?? ''} onChange={e => onChange(e.target.value)} className={cls}/>
      }
    </div>
  )
}

// ── Triage Panel ───────────────────────────────────────────────────────────

function TriagePanel({ records, selIds, onClose, showToast }) {
  const [prompt,  setPrompt]  = useState('')
  const [apiKey,  setApiKey]  = useState(() => localStorage.getItem('oc_api_key') ?? '')
  const [loading, setLoading] = useState(false)
  const [result,  setResult]  = useState(null)
  const [actions, setActions] = useState([])

  function saveKey(k) { setApiKey(k); localStorage.setItem('oc_api_key', k) }

  const scope = selIds.size ? records.filter(r => selIds.has(r.id)) : records

  async function submit() {
    if (!prompt.trim() || loading) return
    if (!apiKey.trim()) { showToast('Enter Anthropic API key', 'error'); return }
    setLoading(true); setResult(null); setActions([])

    const recordsText = scope.map(r =>
      `#${r.seqId} [${r.category}] ${r.sourceUser} | ${r.tweetDate} | unc=${r.uncertainty} novelty=${r.novelty}\n` +
      `  DESC: ${r.description}\n  NOTES: ${r.notes ?? ''}\n  URLS: ${r.refUrls ?? ''}`
    ).join('\n\n')

    const system = `You are an analyst triaging a research database of OpenClaw (open-source browser-automation agent) use cases.
Each record: #seqID [Category] SourceUser | TweetDate | unc=X novelty=Y, then DESC/NOTES/URLS.
When suggesting changes, respond with plain English then a JSON block in \`\`\`json ... \`\`\` with an array of actions:
  { "action": "merge"|"reclassify"|"flag"|"delete"|"update_field", "ids": [seqId...], "reason": "...",
    "new_category"?: "...", "field"?: "...", "new_value"?: "...", "flag_reason"?: "..." }
Reference records by their #seqID numbers. Be concise and specific.`

    try {
      const res = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          model: 'claude-sonnet-4-6',
          max_tokens: 2048,
          system,
          messages: [{ role: 'user', content: `${prompt}\n\n---\nRECORDS (${scope.length}):\n${recordsText}` }]
        })
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error?.message || res.statusText)
      const text = data.content[0].text
      const jsonMatch = text.match(/```json\s*([\s\S]*?)```/)
      setResult(text.replace(/```json[\s\S]*?```/g, '').trim())
      if (jsonMatch) { try { setActions(JSON.parse(jsonMatch[1])) } catch {} }
    } catch (e) {
      setResult(`Error: ${e.message}`)
    }
    setLoading(false)
  }

  return (
    <div className="border-t border-gray-800 bg-gray-900 p-3 shrink-0 max-h-64 overflow-y-auto">
      <div className="flex items-center gap-2 mb-2">
        <Bot size={13} className="text-blue-400"/>
        <span className="text-xs font-medium">Triage</span>
        <span className="text-[10px] text-gray-600">
          scope: {selIds.size ? [...selIds].map(i => '#'+i).join(', ') : `all ${scope.length}`}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <label className="text-[10px] text-gray-500">API key</label>
          <input type="password" value={apiKey} onChange={e => saveKey(e.target.value)}
            placeholder="sk-ant-…"
            className="w-40 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-xs focus:outline-none"/>
          <IconBtn onClick={onClose}><X size={12}/></IconBtn>
        </div>
      </div>
      <div className="flex gap-2">
        <textarea value={prompt} onChange={e => setPrompt(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit() }}
          placeholder="e.g. find duplicates · reclassify #5 as Finance · flag anything before 2026-01-29…"
          rows={2}
          className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs resize-none focus:outline-none"/>
        <button onClick={submit} disabled={loading}
          className="self-end px-2 py-1.5 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-40">
          {loading ? <span className="text-xs">…</span> : <Send size={13}/>}
        </button>
      </div>
      {result && (
        <div className="mt-2 text-xs text-gray-300 whitespace-pre-wrap leading-relaxed pt-2 border-t border-gray-800">
          {result}
        </div>
      )}
      {actions.length > 0 && (
        <div className="mt-2 space-y-1">
          <p className="text-[10px] text-gray-500 uppercase tracking-wide">Suggested actions</p>
          {actions.map((a, i) => (
            <div key={i} className="flex items-center gap-2 bg-gray-800 rounded px-2 py-1 text-xs">
              <span className="text-amber-400 font-medium shrink-0">{a.action}</span>
              <span className="text-blue-400 shrink-0">{(a.ids ?? []).map(id => '#' + id).join(' ')}</span>
              {a.new_category && <span className="text-emerald-400">→ {a.new_category}</span>}
              <span className="text-gray-500 flex-1 truncate">{a.reason}</span>
              <button className="text-emerald-500 hover:text-emerald-300 text-[10px] font-medium shrink-0">
                Apply
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Stats Modal ────────────────────────────────────────────────────────────

function StatsModal({ records, onClose }) {
  const count = (field) => Object.entries(
    records.reduce((a, r) => { const k = r[field] || '(none)'; a[k] = (a[k]||0)+1; return a }, {})
  ).sort((a,b) => b[1]-a[1])

  return (
    <Modal title={`Stats — ${records.length} total`} onClose={onClose} wide>
      <div className="grid grid-cols-3 gap-6 text-xs">
        {[['Category','category'],['Novelty','novelty'],['Uncertainty','uncertainty']].map(([h,f]) => (
          <div key={f}>
            <h3 className="font-semibold text-gray-300 mb-2">{h}</h3>
            {count(f).map(([k,v]) => (
              <div key={k} className="flex justify-between py-0.5 border-b border-gray-800/50">
                <span className="text-gray-400 truncate mr-2">{k}</span>
                <span className="text-gray-300 shrink-0">{v}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </Modal>
  )
}

// ── Import Modal ───────────────────────────────────────────────────────────

function ImportModal({ onClose, onImport }) {
  const [rows, setRows] = useState(null)
  return (
    <Modal title="Import CSV" onClose={onClose}>
      <p className="text-xs text-gray-400 mb-3">
        Export from Google Sheets via <strong className="text-gray-300">File → Download → CSV</strong>, then select the file below.<br/>
        Recognised columns: Category, Source user, Description, Reference URLs, Tweet date, Search date, Notes, Uncertainty, Novelty.
      </p>
      <label className="inline-flex items-center gap-2 cursor-pointer mb-3">
        <span className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-xs font-medium border border-gray-600">
          Choose CSV file…
        </span>
        {rows && <span className="text-xs text-emerald-400">{rows.length} rows ready</span>}
        <input type="file" accept=".csv" className="sr-only"
          onChange={e => { const f = e.target.files[0]; if (!f) return; f.text().then(t => setRows(parseCSV(t))) }}/>
      </label>
      <div className="flex gap-2">
        <button onClick={() => rows && onImport(rows)} disabled={!rows}
          className="px-3 py-1.5 rounded bg-blue-700 hover:bg-blue-600 text-xs font-medium disabled:opacity-40">
          Import {rows ? `${rows.length} records` : ''}
        </button>
        <button onClick={onClose}
          className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-xs">
          Cancel
        </button>
      </div>
    </Modal>
  )
}

function Modal({ title, onClose, children, wide }) {
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className={`bg-gray-900 border border-gray-700 rounded-xl p-5 mx-4 max-h-[80vh] overflow-y-auto shadow-2xl
          ${wide ? 'max-w-3xl w-full' : 'max-w-lg w-full'}`}
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">{title}</h2>
          <IconBtn onClick={onClose}><X size={14}/></IconBtn>
        </div>
        {children}
      </div>
    </div>
  )
}
