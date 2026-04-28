import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  collection, onSnapshot, getDoc, setDoc, addDoc, updateDoc, deleteDoc,
  doc, serverTimestamp, writeBatch
} from 'firebase/firestore'
import { signInWithPopup, signOut, onAuthStateChanged } from 'firebase/auth'
import { db, auth, provider } from './firebase.js'
import { csvEscape, safeUrl, normalizeSuggestionUrl } from './security.js'
import { getDeepLinkDocId, selectDeepLinkedRecord } from './deeplink.js'
import { prepareCsvImport } from './csvImport.js'
import {
  Search, BarChart2, Bot,
  ChevronRight, ChevronDown, Layers, X, LogIn, LogOut, Pencil, Trash2,
  CheckCircle, Send, PlusCircle, Inbox, Link2, UserRound, Globe, ShieldAlert
} from 'lucide-react'

// ── Build info ─────────────────────────────────────────────────────────────

const APP_VERSION = '1.4'
const BUILD_TIME = '2026-04-20 23:44 AEST'

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

const STOP_WORDS = new Set([
  'a','an','the','and','or','but','in','on','at','to','for','of','with','by',
  'from','is','are','was','were','be','been','has','have','had','do','does',
  'did','will','would','could','should','may','might','can','it','its','this',
  'that','these','those','i','me','my','we','our','you','your','they','their',
  'he','she','him','her','his','as','into','through','during','before','after',
  'over','under','between','out','off','up','so','yet','nor','not','no','if',
  'while','when','where','how','what','which','who','whom','use','used','using',
  'also','than','then','very','just','even','all','any','each','few','more',
  'most','other','some','such','own','same','both','here','there','about','via',
  's','t','re','ve','ll','d','m','her','his',
])

function extractTagWords(text) {
  if (!text) return []
  return text.toLowerCase().replace(/[^\w\s]/g, ' ').split(/\s+/)
    .filter(w => w.length > 3 && !STOP_WORDS.has(w) && !/^\d+$/.test(w))
}
const SUGGESTION_SESSION_URL =
  import.meta.env.VITE_SUGGESTION_SESSION_URL ||
  'https://requestsuggestionsessionhttp-lqo4ecc5hq-uc.a.run.app'
const SUBMIT_SUGGESTION_URL =
  import.meta.env.VITE_SUBMIT_SUGGESTION_URL ||
  'https://submitsuggestionhttp-lqo4ecc5hq-uc.a.run.app'
const CLAUDE_PROXY_URL =
  import.meta.env.VITE_CLAUDE_PROXY_URL ||
  'https://claudeproxy-lqo4ecc5hq-uc.a.run.app'

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

async function callSuggestionEndpoint(path, body, bearerToken) {
  const endpoint = path === 'requestSuggestionSessionHttp'
    ? SUGGESTION_SESSION_URL
    : SUBMIT_SUGGESTION_URL
  const payload = bearerToken ? { ...body, idToken: bearerToken } : body
  const response = await fetch(endpoint, {
    method: 'POST',
    body: JSON.stringify(payload),
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
  const [deepLinkDocId, setDeepLinkDocId] = useState(() => {
    if (typeof window === 'undefined') return null
    return getDeepLinkDocId(window.location.href)
  })
  const [editing,    setEditing]    = useState(null)
  const [triageOpen, setTriageOpen] = useState(false)
  const [statsOpen,  setStatsOpen]  = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [suggestionOpen, setSuggestionOpen] = useState(false)
  const [selIds,     setSelIds]     = useState(new Set())
  const [toast,      setToast]      = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [groups,     setGroups]     = useState([])
  const [viewMode,   setViewMode]   = useState('grouped') // 'flat' | 'grouped'
  const [expandedGroups, setExpandedGroups] = useState(new Set())
  const [filterTags,     setFilterTags]     = useState(new Set())
  const [conceptMap,     setConceptMap]     = useState(null)   // null=unfetched, []= loaded
  const [loadingConcepts, setLoadingConcepts] = useState(false)
  const [filterConcepts, setFilterConcepts] = useState(new Set())
  const canWrite = user?.email === 'david@prismism.com'
  const showToast = useCallback((msg, type = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }, [])

  // Build conceptMap from stored keywords + current records (used on load and after save)
  const buildConceptMap = useCallback((storedConcepts) => {
    return storedConcepts.map(({ concept, keywords }) => {
      const nameWords = concept
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .toLowerCase().split(/\s+/)
        .filter(w => w.length > 3)
      const allKws = [...new Set([...keywords, ...nameWords])]
      const ids = new Set(
        records.filter(r => {
          const desc = (r.description || '').toLowerCase()
          return allKws.some(k => new RegExp(`\\b${k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(desc))
        }).map(r => r.id)
      )
      return { concept, ids, keywords: allKws }
    }).filter(c => c.ids.size > 0)
  }, [records])

  // Owner-only: call Claude, save keywords to Firestore, rebuild map
  const fetchConcepts = useCallback(async () => {
    if (!records.length || !auth.currentUser) return
    setLoadingConcepts(true)
    setConceptMap(null)
    try {
      const sample = records.map(r => (r.description || '').slice(0, 150)).join('\n')
      const data = await callClaude({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 1024,
        messages: [{
          role: 'user',
          content: `Identify 10-15 meaningful themes in these use-case descriptions.
For each theme output exactly one line in this format (no bullets, no JSON, no extra text):
ThemeName: keyword1, keyword2, keyword3, keyword4

Descriptions:
${sample}`
        }]
      })
      const text = data.content?.[0]?.text ?? ''
      const parsed = text.split('\n')
        .map(line => line.trim())
        .filter(line => line.includes(':'))
        .map(line => {
          const colon = line.indexOf(':')
          const concept = line.slice(0, colon).trim()
          const keywords = line.slice(colon + 1).split(',').map(k => k.trim().toLowerCase()).filter(Boolean)
          return { concept, keywords }
        })
        .filter(c => c.concept && c.keywords.length)
      if (!parsed.length) throw new Error('No concepts parsed from response')
      await setDoc(doc(db, 'meta', 'concept_cloud'), { concepts: parsed, updatedAt: serverTimestamp() })
      setConceptMap(buildConceptMap(parsed))
    } catch (e) {
      console.error('fetchConcepts failed', e)
      setConceptMap([])
    } finally {
      setLoadingConcepts(false)
    }
  }, [records, buildConceptMap])

  // Load stored concept keywords from Firestore and build map against current records
  useEffect(() => {
    if (!records.length) return
    getDoc(doc(db, 'meta', 'concept_cloud'))
      .then(snap => {
        if (snap.exists()) setConceptMap(buildConceptMap(snap.data().concepts ?? []))
        else setConceptMap([])
      })
      .catch(() => setConceptMap([]))
  }, [records, buildConceptMap])

  useEffect(() => onAuthStateChanged(auth, setUser), [])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const syncDeepLink = () => setDeepLinkDocId(getDeepLinkDocId(window.location.href))
    window.addEventListener('popstate', syncDeepLink)
    window.addEventListener('hashchange', syncDeepLink)
    return () => {
      window.removeEventListener('popstate', syncDeepLink)
      window.removeEventListener('hashchange', syncDeepLink)
    }
  }, [])

  useEffect(() => {
    const unsub = onSnapshot(
      collection(db, 'use_cases'),
      snap => {
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
      },
      error => {
        console.error('use_cases listener failed', error)
        showToast(`Record load failed: ${error.code || error.message}`, 'error')
      }
    )
    return unsub
  }, [showToast])

  useEffect(() => {
    const unsub = onSnapshot(
      collection(db, 'suggestion_queue'),
      snap => {
        const docs = snap.docs.map(d => ({ id: d.id, ...d.data() }))
        docs.sort((a, b) => {
          const ca = a.createdAt?.toMillis?.() ?? 0
          const cb = b.createdAt?.toMillis?.() ?? 0
          return cb - ca
        })
        setSuggestions(docs)
      },
      error => {
        console.error('suggestion_queue listener failed', error)
        showToast(`Queue load failed: ${error.code || error.message}`, 'error')
      }
    )
    return unsub
  }, [showToast])

  useEffect(() => {
    const unsub = onSnapshot(
      collection(db, 'use_case_groups'),
      snap => setGroups(snap.docs.map(d => ({ id: d.id, ...d.data() }))),
      error => {
        console.error('use_case_groups listener failed', error)
        showToast(`Groups load failed: ${error.code || error.message}`, 'error')
      }
    )
    return unsub
  }, [showToast])

  const categories = useMemo(() => [...new Set(records.map(r => r.category).filter(Boolean))].sort(), [records])
  const novelties  = useMemo(() => [...new Set(records.map(r => r.novelty).filter(Boolean))].sort(), [records])

  const tagCounts = useMemo(() => {
    const counts = {}
    for (const r of records) {
      for (const w of extractTagWords(r.description)) {
        counts[w] = (counts[w] || 0) + 1
      }
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 60)
  }, [records])

  const filtered = useMemo(() => {
    const tokens = parseSearchTokens(q.trim())
    let r = records
    if (tokens.length) r = r.filter(rec => matchRecord(rec, tokens, matchMode))
    if (filterCat) r = r.filter(rec => rec.category === filterCat)
    if (filterUnc) r = r.filter(rec => rec.uncertainty === filterUnc)
    if (filterNov) r = r.filter(rec => rec.novelty === filterNov)
    if (filterTags.size) {
      r = r.filter(rec => {
        const words = new Set(extractTagWords(rec.description))
        return [...filterTags].some(tag => words.has(tag))
      })
    }
    if (filterConcepts.size && conceptMap) {
      const allowed = new Set()
      for (const entry of conceptMap) {
        if (filterConcepts.has(entry.concept)) entry.ids.forEach(id => allowed.add(id))
      }
      r = r.filter(rec => allowed.has(rec.id))
    }
    const { field, dir } = sort
    return [...r].sort((a, b) => {
      const av = a[field] ?? '', bv = b[field] ?? ''
      if (typeof av === 'number' || typeof bv === 'number' || field === 'seqId') {
        const an = Number(av), bn = Number(bv)
        return dir === 'asc' ? an - bn : bn - an
      }
      return dir === 'asc'
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av))
    })
  }, [records, q, matchMode, filterCat, filterUnc, filterNov, filterTags, filterConcepts, conceptMap, sort])

  // ── Group lookup maps ──────────────────────────────────────────────────────

  // groupByLeadDocId: { firestoreDocId → groupDoc } for lead records
  const groupByLeadDocId = useMemo(() => {
    const map = {}
    groups.forEach(g => { if (g.leadId) map[g.leadId] = g })
    return map
  }, [groups])

  // memberToGroup: { firestoreDocId → groupDoc } for member records
  const memberToGroup = useMemo(() => {
    const map = {}
    groups.forEach(g => (g.memberIds ?? []).forEach(mid => { map[mid] = g }))
    return map
  }, [groups])

  useEffect(() => {
    if (!deepLinkDocId || !records.length) return
    const match = selectDeepLinkedRecord(records, `https://openclaw-explorer.web.app/?id=${encodeURIComponent(deepLinkDocId)}`)
    if (!match) return

    const group = memberToGroup[match.id] ?? groupByLeadDocId[match.id] ?? null
    if (group) {
      setExpandedGroups(current => {
        if (current.has(group.id)) return current
        const next = new Set(current)
        next.add(group.id)
        return next
      })
    }

    setSelected(current => (current?.id === match.id ? current : match))
    setEditing(null)
  }, [deepLinkDocId, records, memberToGroup, groupByLeadDocId])

  // displayRows: flattened list of { type: 'flat'|'lead'|'member', record, group? }
  const displayRows = useMemo(() => {
    if (viewMode !== 'grouped') return filtered.map(r => ({ type: 'flat', record: r }))
    const rows = []
    for (const record of filtered) {
      if (memberToGroup[record.id]) continue  // will appear under its lead
      const group = groupByLeadDocId[record.id]
      if (group) {
        rows.push({ type: 'lead', record, group })
        if (expandedGroups.has(group.id)) {
          for (const memberId of (group.memberIds ?? [])) {
            const mr = records.find(r => r.id === memberId)
            if (mr) rows.push({ type: 'member', record: mr, group })
          }
        }
      } else {
        rows.push({ type: 'flat', record })
      }
    }
    return rows
  }, [filtered, viewMode, groupByLeadDocId, memberToGroup, expandedGroups, records])

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
    const { id, seqId, createdAt, _approveId, ...payload } = data
    payload.updatedAt = serverTimestamp()
    if (id) {
      await updateDoc(doc(db, 'use_cases', id), payload)
      showToast('Saved')
    } else {
      payload.createdAt = serverTimestamp()
      await addDoc(collection(db, 'use_cases'), payload)
      if (_approveId) {
        await deleteDoc(doc(db, 'suggestion_queue', _approveId))
      }
      showToast(_approveId ? 'Approved and added' : 'Created')
    }
    setEditing(null)
  }

  async function deleteRecord(id) {
    if (!canWrite || !confirm('Delete this record?')) return
    await deleteDoc(doc(db, 'use_cases', id))
    if (selected?.id === id) setSelected(null)
    showToast('Deleted', 'error')
  }

  function toggleGroupExpand(groupId) {
    setExpandedGroups(s => { const n = new Set(s); n.has(groupId) ? n.delete(groupId) : n.add(groupId); return n })
  }

  async function approveGroup(group) {
    if (!canWrite) return
    await updateDoc(doc(db, 'use_case_groups', group.id), { status: 'approved', updatedAt: serverTimestamp() })
    showToast('Group approved')
  }

  async function rejectGroup(group) {
    if (!canWrite) return
    if (!confirm('Reject this grouping? Records will be ungrouped.')) return
    await deleteDoc(doc(db, 'use_case_groups', group.id))
    showToast('Grouping rejected', 'error')
  }

  async function applyTriageAction(action) {
    if (action.action === 'group' || action.action === 'merge') {
      const leadSeqId  = action.leadId   ?? action.ids?.[0]
      const memberSeqs = action.memberIds ?? action.ids?.slice(1) ?? []
      const leadRec    = records.find(r => r.seqId === leadSeqId)
      const memberRecs = memberSeqs.map(id => records.find(r => r.seqId === id)).filter(Boolean)
      if (!leadRec || memberRecs.length === 0) { showToast('Could not resolve records for group', 'error'); return }
      await addDoc(collection(db, 'use_case_groups'), {
        leadId:    leadRec.id,
        memberIds: memberRecs.map(r => r.id),
        reason:    action.reason ?? '',
        source:    'triage',
        status:    'pending',
        createdAt: serverTimestamp(),
        updatedAt: serverTimestamp(),
      })
      showToast('Group proposal created (pending)')
    } else {
      showToast(`Apply not yet implemented for: ${action.action}`, 'error')
    }
  }

  // ── Group membership management ────────────────────────────────────────────

  async function addRecordToGroup(record, groupId) {
    if (memberToGroup[record.id] || groupByLeadDocId[record.id]) {
      showToast('Record is already in a group — remove it first', 'error'); return
    }
    const group = groups.find(g => g.id === groupId)
    if (!group) return
    await updateDoc(doc(db, 'use_case_groups', groupId), {
      memberIds: [...(group.memberIds ?? []), record.id],
      updatedAt: serverTimestamp(),
    })
    showToast('Added to group')
  }

  // Promote a member record to lead; old lead becomes a member
  async function promoteGroupMember(group, memberRecord) {
    const oldLeadId = group.leadId
    const newMemberIds = [
      ...(group.memberIds ?? []).filter(id => id !== memberRecord.id),
      oldLeadId,
    ]
    await updateDoc(doc(db, 'use_case_groups', group.id), {
      leadId:    memberRecord.id,
      memberIds: newMemberIds,
      updatedAt: serverTimestamp(),
    })
    showToast(`#${memberRecord.seqId} promoted to lead`)
  }

  // Remove a member from the group; if last member dissolves the group
  async function removeMemberFromGroup(group, memberRecord) {
    const newMemberIds = (group.memberIds ?? []).filter(id => id !== memberRecord.id)
    if (newMemberIds.length === 0) {
      if (!confirm('Removing the last member will dissolve the group. Continue?')) return
      await deleteDoc(doc(db, 'use_case_groups', group.id))
      showToast('Group dissolved')
    } else {
      await updateDoc(doc(db, 'use_case_groups', group.id), {
        memberIds: newMemberIds, updatedAt: serverTimestamp(),
      })
      showToast('Removed from group')
    }
  }

  // Remove the lead from the group; promote newLeadId as the new lead.
  // If newLeadId is falsy (no members) the group is dissolved.
  async function removeLeadFromGroup(group, newLeadId) {
    if (!newLeadId) {
      await deleteDoc(doc(db, 'use_case_groups', group.id))
      showToast('Group dissolved')
      return
    }
    const newMemberIds = (group.memberIds ?? []).filter(id => id !== newLeadId)
    await updateDoc(doc(db, 'use_case_groups', group.id), {
      leadId:    newLeadId,
      memberIds: newMemberIds,
      updatedAt: serverTimestamp(),
    })
    showToast('Lead removed, new lead promoted')
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
          {/* CSV import/export intentionally hidden for now */}
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
            onClick={() => { setFilterCat(''); setFilterUnc(''); setFilterNov(''); setQ(''); setFilterTags(new Set()) }}
            className="text-xs text-gray-600 hover:text-gray-400 text-left">
            Clear filters
          </button>
          <div>
            <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">View</label>
            <div className="flex rounded overflow-hidden border border-gray-700 text-[10px] font-medium">
              {[['flat','Flat'],['grouped','Grouped']].map(([v,l]) => (
                <button key={v} onClick={() => setViewMode(v)}
                  className={`flex-1 px-2 py-1 ${viewMode === v ? 'bg-blue-700 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'}`}>
                  {l}
                </button>
              ))}
            </div>
            <div className="mt-1 text-[9px] text-gray-600 leading-tight">
              v{APP_VERSION} · {BUILD_TIME}
            </div>
          </div>
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

          {/* Concept cloud */}
          <TagCloud
            tagCounts={tagCounts}
            filterTags={filterTags}
            onToggle={word => setFilterTags(prev => {
              const next = new Set(prev)
              next.has(word) ? next.delete(word) : next.add(word)
              return next
            })}
            onClear={() => setFilterTags(new Set())}
            conceptMap={conceptMap}
            loadingConcepts={loadingConcepts}
            filterConcepts={filterConcepts}
            onToggleConcept={concept => setFilterConcepts(prev => {
              const next = new Set(prev)
              next.has(concept) ? next.delete(concept) : next.add(concept)
              return next
            })}
            onClearConcepts={() => setFilterConcepts(new Set())}
            onFetchConcepts={fetchConcepts}
            canRefresh={canWrite}
            filteredCount={filtered.length}
          />

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
                  {displayRows.map(({ type, record: r, group }) => (
                    <tr key={`${type}-${r.id}`}
                      onClick={() => { setSelected(r); setEditing(null) }}
                      onDoubleClick={() => { if (type === 'lead') toggleGroupExpand(group.id) }}
                      className={[
                        'border-b border-gray-800/50 cursor-pointer hover:bg-gray-800/30 transition-colors',
                        selected?.id === r.id ? 'bg-blue-950/40' : '',
                        selIds.has(r.id) ? 'bg-blue-950/20' : '',
                        type === 'member' ? 'bg-gray-950/60 opacity-80' : '',
                      ].join(' ')}>

                      {/* # / expand column */}
                      <td className="pl-2 pr-1 py-1.5 whitespace-nowrap" onClick={e => e.stopPropagation()}>
                        {type === 'member' ? (
                          <div className="pl-5 flex items-center">
                            <span className="font-mono text-blue-500/40">{r.seqId}</span>
                          </div>
                        ) : (
                          <div className="flex items-center gap-0.5">
                            {type === 'lead' ? (
                              <button onClick={e => { e.stopPropagation(); toggleGroupExpand(group.id) }}
                                className="text-gray-500 hover:text-amber-400 p-0.5 shrink-0">
                                {expandedGroups.has(group.id) ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
                              </button>
                            ) : (
                              <input type="checkbox" className="accent-blue-500 cursor-pointer"
                                checked={selIds.has(r.id)}
                                onChange={() => toggleSelId(r.id)}/>
                            )}
                            <span className="font-mono text-blue-500/70">{r.seqId}</span>
                          </div>
                        )}
                      </td>

                      <td className="px-2 py-1.5 text-gray-300 max-w-[144px] truncate">{r.category}</td>
                      <td className="px-2 py-1.5 text-gray-400 max-w-[128px] truncate">{r.sourceUser}</td>

                      {/* description with group badge */}
                      <td className="px-2 py-1.5 text-gray-300 max-w-xs truncate">
                        {type === 'lead' && (
                          <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium mr-1.5 shrink-0
                            ${group.status === 'pending' ? 'bg-amber-900/60 text-amber-400' : 'bg-blue-900/50 text-blue-400'}`}>
                            <Layers size={8}/>
                            {group.memberIds?.length ?? 0}
                            {group.status === 'pending' ? '?' : ''}
                          </span>
                        )}
                        {type === 'member' && (
                          <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] mr-1.5 shrink-0
                            ${group.status === 'pending' ? 'bg-amber-900/30 text-amber-600' : 'bg-gray-800 text-gray-500'}`}>
                            {group.status === 'pending' ? 'similar?' : 'grouped'}
                          </span>
                        )}
                        {r.description}
                      </td>

                      <td className="px-2 py-1.5 text-gray-600 whitespace-nowrap">{r.tweetDate}</td>
                      <td className={`px-2 py-1.5 font-medium ${uncColor(r.uncertainty)}`}>{r.uncertainty}</td>
                      <td className="px-2 py-1.5">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${novColor(r.novelty)}`}>
                          {r.novelty}
                        </span>
                      </td>

                      {/* actions */}
                      <td className="pr-1">
                        {type === 'lead' && group.status === 'pending' && canWrite ? (
                          <div className="flex items-center gap-0.5" onClick={e => e.stopPropagation()}>
                            <button onClick={() => approveGroup(group)} title="Approve grouping"
                              className="text-emerald-700 hover:text-emerald-400 p-0.5">
                              <CheckCircle size={11}/>
                            </button>
                            <button onClick={() => rejectGroup(group)} title="Reject grouping"
                              className="text-red-900 hover:text-red-500 p-0.5">
                              <X size={11}/>
                            </button>
                          </div>
                        ) : (
                          <button onClick={e => { e.stopPropagation(); setSelected(r); setEditing(null) }}
                            className="text-gray-700 hover:text-gray-400 p-0.5">
                            <ChevronRight size={12}/>
                          </button>
                        )}
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
                canWrite={canWrite}
                memberGroup={memberToGroup[selected.id] ?? null}
                leadGroup={groupByLeadDocId[selected.id] ?? null}
                allRecords={records}
                groups={groups}
                onJumpToLead={leadDocId => {
                  const lead = records.find(r => r.id === leadDocId)
                  if (!lead) return
                  const grp = groupByLeadDocId[leadDocId]
                  if (grp) setExpandedGroups(s => { const n = new Set(s); n.add(grp.id); return n })
                  setSelected(lead)
                }}
                onAddToGroup={groupId => addRecordToGroup(selected, groupId)}
                onPromoteToLead={group => promoteGroupMember(group, selected)}
                onRemoveMember={group => removeMemberFromGroup(group, selected)}
                onRemoveLead={removeLeadFromGroup}/>
            )}
            {editing && (
              <EditPanel key={editing.id ?? 'new'} record={editing}
                categories={categories}
                onSave={saveRecord}
                onCancel={() => setEditing(null)}/>
            )}
          </div>

          {/* Triage */}
          {triageOpen && (
            <TriagePanel
              records={filtered}
              allRecords={records}
              selIds={selIds}
              onClose={() => setTriageOpen(false)}
              showToast={showToast}
              canWrite={canWrite}
              onApplyAction={applyTriageAction}/>
          )}
        </div>
      </div>

      {statsOpen  && <StatsModal  records={records} onClose={() => setStatsOpen(false)}/>}
      {suggestionOpen && (
        <SuggestionModal
          user={user}
          canWrite={canWrite}
          suggestions={suggestions}
          onClose={() => setSuggestionOpen(false)}
          showToast={showToast}
          onApprove={(item, enriched = {}) => {
            setSuggestionOpen(false)
            setEditing({
              category:    enriched.category    ?? '',
              sourceUser:  enriched.sourceUser  ?? item.displayName ?? '',
              description: enriched.description ?? '',
              refUrls:     item.url             ?? '',
              tweetDate:   enriched.tweetDate   ?? '',
              searchDate:  new Date().toISOString().slice(0, 10),
              notes:       enriched.notes       ?? '',
              uncertainty: enriched.uncertainty ?? 'medium',
              novelty:     enriched.novelty     ?? 'novel',
              _approveId:  item.id,
            })
            setSelected(null)
          }}
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

// ── Concept Cloud ──────────────────────────────────────────────────────────

function TagCloud({
  conceptMap, loadingConcepts, filterConcepts,
  onToggleConcept, onClearConcepts, onFetchConcepts, canRefresh, filteredCount
}) {
  const [open, setOpen] = useState(false)
  const handleOpen = () => setOpen(o => !o)
  const totalCount = conceptMap ? conceptMap.length : 0

  const activeCount = filterConcepts.size

  // Size bubbles by concept's record count
  const maxSize = conceptMap ? Math.max(...conceptMap.map(c => c.ids.size), 1) : 1
  const bubbleStyle = count => {
    const r = count / maxSize
    const fs = Math.round(10 + r * 9)
    const px = Math.round(7 + r * 9)
    const py = Math.round(4 + r * 5)
    return { fontSize: `${fs}px`, padding: `${py}px ${px}px`, lineHeight: 1.2 }
  }

  return (
    <div className="border-b border-gray-800 bg-gray-900 shrink-0">
      <button onClick={handleOpen}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-800 transition-colors">
        <ChevronDown size={12}
          className={`text-gray-500 transition-transform duration-200 ${open ? 'rotate-180' : ''}`}/>
        <span className="text-[10px] uppercase tracking-wide text-gray-500">Concepts</span>
        {totalCount > 0 && (
          <span className="text-[10px] text-gray-500">{totalCount}</span>
        )}
        {activeCount > 0 && (
          <span className="text-[10px] text-blue-400">{activeCount} active · {filteredCount} rows</span>
        )}
      </button>

      {open && (
        <div className="px-3 pb-3">
          {(loadingConcepts || conceptMap === null) && (
            <p className="text-[10px] text-gray-500 py-2">{loadingConcepts ? 'Analysing records…' : 'Loading…'}</p>
          )}
          {!loadingConcepts && conceptMap !== null && conceptMap.length === 0 && (
            <p className="text-[10px] text-gray-500 py-2">
              No concepts available.{' '}
              {canRefresh && <button onClick={onFetchConcepts} className="text-blue-400 hover:text-blue-300">Generate</button>}
            </p>
          )}
          {!loadingConcepts && conceptMap && conceptMap.length > 0 && (
            <div className="flex flex-wrap gap-2 items-center pt-1">
              {conceptMap.map(({ concept, ids, keywords }) => {
                const active = filterConcepts.has(concept)
                return (
                  <button key={concept} onClick={() => onToggleConcept(concept)}
                    title={`${ids.size} records — keywords: ${(keywords||[]).join(', ')}`}
                    style={bubbleStyle(ids.size)}
                    className={`rounded-full font-medium transition-colors ${
                      active
                        ? 'bg-blue-600 text-white'
                        : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
                    }`}>
                    {concept}
                  </button>
                )
              })}
              {canRefresh && (
                <button onClick={onFetchConcepts}
                  title="Re-generate concepts"
                  className="text-[10px] text-gray-600 hover:text-gray-400 flex items-center gap-0.5 ml-1">
                  ↺ refresh
                </button>
              )}
              {activeCount > 0 && (
                <button onClick={onClearConcepts}
                  className="text-[10px] text-gray-600 hover:text-gray-400 flex items-center gap-0.5">
                  <X size={10}/> clear
                </button>
              )}
            </div>
          )}
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

function DetailPanel({ record: r, onEdit, onDelete, onClose, onToggleSel, isSelected, canWrite,
                       memberGroup, leadGroup, allRecords, groups,
                       onJumpToLead, onAddToGroup, onPromoteToLead, onRemoveMember, onRemoveLead }) {
  const urls = (r.refUrls ?? '').split(',').map(u => u.trim()).filter(Boolean)

  // Local state for group management UI
  const [pickNewLead,  setPickNewLead]  = useState('')   // docId of chosen new lead when removing lead
  const [pickGroup,    setPickGroup]    = useState('')   // groupId when adding to a group
  const [showRemoveLead, setShowRemoveLead] = useState(false)
  const [showAddGroup,   setShowAddGroup]   = useState(false)

  // Records for each member in the lead's group
  const memberRecords = leadGroup
    ? (leadGroup.memberIds ?? []).map(id => allRecords?.find(rec => rec.id === id)).filter(Boolean)
    : []

  // Eligible groups to add this record to (non-rejected, not already containing this record)
  const eligibleGroups = (groups ?? []).filter(g =>
    g.status !== 'rejected' &&
    g.leadId !== r.id &&
    !(g.memberIds ?? []).includes(r.id)
  )

  const groupStatusCls = status =>
    status === 'pending' ? 'bg-amber-900/50 text-amber-400' : 'bg-blue-900/50 text-blue-400'

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

      {/* ── Group management section ── */}

      {/* (a) Record is a group MEMBER */}
      {memberGroup && (
        <div className="mt-4 pt-3 border-t border-gray-800 space-y-2">
          <div className="flex items-center gap-1.5 text-[10px] text-gray-500 uppercase tracking-wide">
            <Layers size={10}/> Group member
            <span className={`ml-1 px-1.5 py-0.5 rounded font-medium ${groupStatusCls(memberGroup.status)}`}>
              {memberGroup.status}
            </span>
          </div>
          <p className="text-gray-400 text-xs">{memberGroup.reason}</p>
          <button onClick={() => onJumpToLead(memberGroup.leadId)}
            className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300">
            <ChevronRight size={11}/> Jump to lead record
          </button>
          {canWrite && (
            <div className="flex gap-2 pt-1">
              <button onClick={() => onPromoteToLead(memberGroup)}
                className="px-2 py-1 rounded bg-amber-900/50 hover:bg-amber-800/60 text-amber-300 text-[11px] font-medium">
                Promote to lead
              </button>
              <button onClick={() => onRemoveMember(memberGroup)}
                className="px-2 py-1 rounded bg-gray-700 hover:bg-red-900/50 text-gray-300 hover:text-red-300 text-[11px] font-medium">
                Remove from group
              </button>
            </div>
          )}
        </div>
      )}

      {/* (b/c) Record is a group LEAD */}
      {leadGroup && (
        <div className="mt-4 pt-3 border-t border-gray-800 space-y-2">
          <div className="flex items-center gap-1.5 text-[10px] text-gray-500 uppercase tracking-wide">
            <Layers size={10}/> Group lead — {memberRecords.length} similar
            <span className={`ml-1 px-1.5 py-0.5 rounded font-medium ${groupStatusCls(leadGroup.status)}`}>
              {leadGroup.status}
            </span>
          </div>
          <p className="text-gray-400 text-xs">{leadGroup.reason}</p>
          {memberRecords.length > 0 && (
            <p className="text-[11px] text-gray-500">
              Members: {memberRecords.map(m => `#${m.seqId}`).join(', ')}
            </p>
          )}
          {canWrite && !showRemoveLead && (
            <button onClick={() => { setShowRemoveLead(true); setPickNewLead(memberRecords[0]?.id ?? '') }}
              className="px-2 py-1 rounded bg-gray-700 hover:bg-red-900/50 text-gray-300 hover:text-red-300 text-[11px] font-medium">
              Remove me from this group…
            </button>
          )}
          {canWrite && showRemoveLead && (
            <div className="space-y-1.5">
              {memberRecords.length === 0 ? (
                <p className="text-xs text-gray-500">No members left — removing will dissolve the group.</p>
              ) : (
                <>
                  <label className="block text-[10px] text-gray-500 uppercase tracking-wide">Promote as new lead</label>
                  <select value={pickNewLead} onChange={e => setPickNewLead(e.target.value)}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100">
                    {memberRecords.map(m => (
                      <option key={m.id} value={m.id}>#{m.seqId} — {m.description?.slice(0, 50)}</option>
                    ))}
                  </select>
                </>
              )}
              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    if (memberRecords.length === 0) {
                      if (!confirm('No members remain. Dissolve the group?')) return
                      // dissolve — handled in removeLeadFromGroup with empty memberIds
                    }
                    await onRemoveLead(leadGroup, pickNewLead || memberRecords[0]?.id)
                    setShowRemoveLead(false)
                  }}
                  className="px-2 py-1 rounded bg-red-900/60 hover:bg-red-800/70 text-red-200 text-[11px] font-medium">
                  {memberRecords.length === 0 ? 'Dissolve group' : 'Confirm'}
                </button>
                <button onClick={() => setShowRemoveLead(false)}
                  className="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-300 text-[11px]">
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* (a) Add ungrouped record to an existing group */}
      {!memberGroup && !leadGroup && canWrite && eligibleGroups.length > 0 && (
        <div className="mt-4 pt-3 border-t border-gray-800 space-y-1.5">
          {!showAddGroup ? (
            <button onClick={() => { setShowAddGroup(true); setPickGroup(eligibleGroups[0]?.id ?? '') }}
              className="flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-300">
              <Layers size={10}/> Add to existing group…
            </button>
          ) : (
            <>
              <label className="block text-[10px] text-gray-500 uppercase tracking-wide">Add to group</label>
              <select value={pickGroup} onChange={e => setPickGroup(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100">
                {eligibleGroups.map(g => {
                  const lead = allRecords?.find(rec => rec.id === g.leadId)
                  const label = lead ? `#${lead.seqId} — ${lead.description?.slice(0, 45)}` : g.id
                  return <option key={g.id} value={g.id}>{label}</option>
                })}
              </select>
              <div className="flex gap-2">
                <button onClick={async () => { await onAddToGroup(pickGroup); setShowAddGroup(false) }}
                  className="px-2 py-1 rounded bg-blue-800 hover:bg-blue-700 text-blue-100 text-[11px] font-medium">
                  Add
                </button>
                <button onClick={() => setShowAddGroup(false)}
                  className="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-300 text-[11px]">
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Edit Panel ─────────────────────────────────────────────────────────────

function EditPanel({ record, onSave, onCancel, categories = [] }) {
  const [form, setForm] = useState({ ...record })
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="w-96 shrink-0 border-l border-gray-800 overflow-y-auto bg-gray-900 p-4">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold text-sm">{record.id ? `Edit #${record.seqId}` : 'New Record'}</h2>
        <IconBtn onClick={onCancel}><X size={13}/></IconBtn>
      </div>
      <div className="space-y-2.5">
        <div>
          <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Category</label>
          <input
            list="category-options"
            value={form.category ?? ''}
            onChange={e => set('category', e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500 text-gray-100"
          />
          <datalist id="category-options">
            {categories.map(c => <option key={c} value={c}/>)}
          </datalist>
        </div>
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
        <div>
          <label className="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Novelty</label>
          <select value={form.novelty ?? ''} onChange={e => set('novelty', e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs">
            <option value="">— select —</option>
            <option>highly novel</option>
            <option>novel</option>
            <option>possibly novel</option>
            <option>common</option>
          </select>
        </div>
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

async function callClaude(body) {
  const idToken = await auth.currentUser?.getIdToken()
  if (!idToken) throw new Error('Not signed in')
  const res = await fetch(CLAUDE_PROXY_URL, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${idToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error?.message ?? res.statusText)
  return data
}

function parseTwitterUrl(url) {
  try {
    const u = new URL(url)
    const host = u.hostname.replace(/^www\./, '')
    if (!['twitter.com', 'x.com'].includes(host)) return null
    const parts = u.pathname.split('/').filter(Boolean)
    // e.g. /username/status/1234567890
    const username = parts[0] ?? null
    const tweetId  = parts[1] === 'status' ? parts[2] : null
    let tweetDate  = null
    if (tweetId) {
      // Twitter Snowflake: timestamp_ms = (id >> 22) + 1288834974657
      const tsMs = Number(BigInt(tweetId) >> BigInt(22)) + 1288834974657
      tweetDate = new Date(tsMs).toISOString().slice(0, 10)
    }
    return { username, tweetDate }
  } catch {
    return null
  }
}

async function enrichFromUrl(url, existingSourceUser) {
  const twitter = parseTwitterUrl(url)

  // Programmatic: extract username + date from Twitter URLs without using Claude
  const programmatic = {}
  if (twitter?.username) {
    const handle = `@${twitter.username}`
    const existing = (existingSourceUser || '').trim()
    if (!existing) {
      programmatic.sourceUser = handle
    } else if (!existing.toLowerCase().includes(twitter.username.toLowerCase())) {
      programmatic.sourceUser = `${existing}, ${handle}`
    }
    if (twitter.tweetDate) programmatic.tweetDate = twitter.tweetDate
  }

  // For Twitter: fetch tweet text via public oEmbed API (CORS-enabled, no auth needed)
  let tweetText = null
  if (twitter) {
    try {
      const oembed = await fetch(
        `https://publish.twitter.com/oembed?url=${encodeURIComponent(url)}&omit_script=true`,
        { signal: AbortSignal.timeout(8000) }
      )
      if (oembed.ok) {
        const tmp = document.createElement('div')
        tmp.innerHTML = (await oembed.json()).html ?? ''
        tweetText = tmp.textContent?.trim() ?? null
      }
    } catch { /* fall through */ }
  }

  // Claude: for Twitter pass tweet text inline; for others use server-side prefetch_url
  try {
    const userContent = tweetText
      ? `Tweet text:\n${tweetText}\n\nURL: ${url}`
      : `URL: ${url}`
    const data = await callClaude({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 512,
      prefetch_url: twitter ? undefined : url,
      system: `You populate a database of OpenClaw (open-source Claude-based agent) use cases.
Given a URL and its content, fill in record fields. Return ONLY a raw JSON object — no markdown fences, no commentary.
Fields: category (string, e.g. "Productivity / Planning"), description (approx 30 words summarising the specific use case shown), novelty ("highly novel"|"novel"|"possibly novel"|"common"), uncertainty ("low"|"medium"|"high"), notes (optional, omit if nothing useful).
For Twitter/X links you may also return: sourceUser (handle without @), tweetDate (YYYY-MM-DD).`,
      messages: [{ role: 'user', content: userContent }],
    })
    // Strip markdown fences if Claude wraps the JSON anyway
    const raw = data.content[0].text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
    const claudeResult = JSON.parse(raw)
    // Programmatic values win over Claude's guesses for sourceUser/tweetDate
    return { ...claudeResult, ...programmatic }
  } catch (e) {
    return { ...programmatic, notes: `Enrichment error: ${String(e.message ?? e).slice(0, 30)}` }
  }
}

function SuggestionModal({ user, canWrite, suggestions, onClose, showToast, onApprove }) {
  const [activeTab, setActiveTab] = useState('submit')
  const [approvingId, setApprovingId] = useState(null)
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
            <p className="text-xs text-gray-400">
              {canWrite ? 'Approve to create a record, or reject to remove from queue.' : 'Public queue, read-only for everyone except the owner.'}
            </p>
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
                  {canWrite && (
                    <div className="flex gap-2 mt-2">
                      <button
                        disabled={approvingId === item.id}
                        onClick={async () => {
                          setApprovingId(item.id)
                          try {
                            const enriched = await enrichFromUrl(item.url, item.displayName)
                            onApprove(item, enriched)
                          } catch {
                            onApprove(item, {})
                          } finally {
                            setApprovingId(null)
                          }
                        }}
                        className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-800 hover:bg-emerald-700 text-emerald-200 text-[11px] font-medium disabled:opacity-50">
                        <CheckCircle size={11}/> {approvingId === item.id ? 'Enriching…' : 'Approve'}
                      </button>
                      <button
                        onClick={async () => {
                          if (!confirm('Reject and delete this suggestion?')) return
                          await deleteDoc(doc(db, 'suggestion_queue', item.id))
                          showToast('Rejected', 'error')
                        }}
                        className="flex items-center gap-1 px-2 py-1 rounded bg-gray-700 hover:bg-red-900 text-gray-300 hover:text-red-200 text-[11px] font-medium">
                        <X size={11}/> Reject
                      </button>
                    </div>
                  )}
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

function TriagePanel({ records, allRecords, selIds, onClose, showToast, canWrite, onApplyAction }) {
  const [prompt,   setPrompt]   = useState('')
  const [loading,  setLoading]  = useState(false)
  const [result,   setResult]   = useState(null)
  const [actions,  setActions]  = useState([])
  const [applying, setApplying] = useState(null)  // index of action being applied

  const scope = selIds.size ? records.filter(r => selIds.has(r.id)) : records

  async function submit() {
    if (!prompt.trim() || loading) return
    if (!canWrite) { showToast('Sign in as owner to use Triage', 'error'); return }
    setLoading(true); setResult(null); setActions([])

    const recordsText = scope.map(r =>
      `#${r.seqId} [${r.category}] ${r.sourceUser} | ${r.tweetDate} | unc=${r.uncertainty} novelty=${r.novelty}\n` +
      `  DESC: ${r.description}\n  NOTES: ${r.notes ?? ''}\n  URLS: ${r.refUrls ?? ''}`
    ).join('\n\n')

    const system = `You are an analyst triaging a research database of OpenClaw (open-source browser-automation agent) use cases.
Each record: #seqID [Category] SourceUser | TweetDate | unc=X novelty=Y, then DESC/NOTES/URLS.
When suggesting changes, respond with plain English then a JSON block in \`\`\`json ... \`\`\` with an array of actions:
  { "action": "merge"|"reclassify"|"flag"|"delete"|"update_field"|"group",
    "ids": [seqId...], "reason": "...",
    "new_category"?: "...", "field"?: "...", "new_value"?: "...", "flag_reason"?: "...",
    "leadId"?: seqId, "memberIds"?: [seqId...] }
Use "group" (preferred) to cluster similar use cases under a lead without deleting — specify leadId and memberIds.
Use "merge" only when records are true duplicates that should be collapsed.
IMPORTANT for grouping: be domain-specific. "YouTube pipeline" ≠ "tweet pipeline" ≠ "podcast pipeline".
Reference records by their #seqID numbers. Be concise and specific.`

    try {
      const data = await callClaude({
        model: 'claude-sonnet-4-6',
        max_tokens: 2048,
        system,
        messages: [{ role: 'user', content: `${prompt}\n\n---\nRECORDS (${scope.length}):\n${recordsText}` }],
      })
      const text = data.content[0].text
      const jsonMatch = text.match(/```json\s*([\s\S]*?)```/)
      setResult(text.replace(/```json[\s\S]*?```/g, '').trim())
      if (jsonMatch) { try { setActions(JSON.parse(jsonMatch[1])) } catch {} }
    } catch (e) {
      setResult(`Error: ${e.message}`)
    }
    setLoading(false)
  }

  async function handleApply(action, i) {
    if (applying !== null) return
    setApplying(i)
    try {
      await onApplyAction(action)
    } catch (e) {
      showToast(`Apply failed: ${e.message}`, 'error')
    } finally {
      setApplying(null)
    }
  }

  const actionIsGroupable = a => a.action === 'group' || a.action === 'merge'

  return (
    <div className="border-t border-gray-800 bg-gray-900 p-3 shrink-0 max-h-64 overflow-y-auto">
      <div className="flex items-center gap-2 mb-2">
        <Bot size={13} className="text-blue-400"/>
        <span className="text-xs font-medium">Triage</span>
        <span className="text-[10px] text-gray-600">
          scope: {selIds.size ? [...selIds].map(i => '#'+i).join(', ') : `all ${scope.length}`}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {!canWrite && <span className="text-[10px] text-amber-500">Sign in as owner to use</span>}
          <IconBtn onClick={onClose}><X size={12}/></IconBtn>
        </div>
      </div>
      <div className="flex gap-2">
        <textarea value={prompt} onChange={e => setPrompt(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit() }}
          placeholder="e.g. find similar ideas · group duplicates · reclassify #5 as Finance…"
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
              <span className={`font-medium shrink-0 ${actionIsGroupable(a) ? 'text-amber-300' : 'text-amber-400'}`}>
                {a.action}
              </span>
              {a.leadId && (
                <span className="text-blue-300 shrink-0">lead #{a.leadId} ← {(a.memberIds ?? []).map(id => '#'+id).join(' ')}</span>
              )}
              {!a.leadId && <span className="text-blue-400 shrink-0">{(a.ids ?? []).map(id => '#' + id).join(' ')}</span>}
              {a.new_category && <span className="text-emerald-400">→ {a.new_category}</span>}
              <span className="text-gray-500 flex-1 truncate">{a.reason}</span>
              {canWrite && (
                <button
                  disabled={applying !== null}
                  onClick={() => handleApply(a, i)}
                  className={`text-[10px] font-medium shrink-0 px-1.5 py-0.5 rounded
                    ${actionIsGroupable(a)
                      ? 'bg-amber-900/50 text-amber-300 hover:bg-amber-800/60'
                      : 'text-gray-500 hover:text-gray-300'}
                    disabled:opacity-40`}>
                  {applying === i ? '…' : 'Apply'}
                </button>
              )}
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
  const [error, setError] = useState(null)

  const handleFileChange = e => {
    const f = e.target.files[0]
    if (!f) return
    f.text().then(text => {
      const result = prepareCsvImport(text)
      if (result.error) {
        setRows(null)
        setError(result.error)
        return
      }
      setRows(result.rows)
      setError(null)
    })
  }

  return (
    <Modal title="Import CSV" onClose={onClose}>
      <p className="text-xs text-gray-400 mb-3">
        Export from Google Sheets via <strong className="text-gray-300">File → Download → CSV</strong>, then select the file below.<br/>
        Recognised columns: Category, Source user, Description, Reference URLs, Reference URLs / Tweets, Tweet date, Search date, Notes, Uncertainty, Novelty.
      </p>
      <label className="inline-flex items-center gap-2 cursor-pointer mb-3">
        <span className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-xs font-medium border border-gray-600">
          Choose CSV file…
        </span>
        {rows && <span className="text-xs text-emerald-400">{rows.length} rows ready</span>}
        <input type="file" accept=".csv" className="sr-only" onChange={handleFileChange}/>
      </label>
      {error && <p className="text-xs text-red-300 mb-3">{error}</p>}
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
