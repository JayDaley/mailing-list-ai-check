<script setup>
// The messages pane: toolbar (title, count, detection-mix bar, filter chips,
// clear), a horizontally-scrolling grid table with a sticky header + column
// filter row, infinite-scroll rows, and a loaded-count footer. It is the primary
// writer of the filters store and the driver of the shared messages store.
import { ref, computed, watch, onMounted } from 'vue'

import { get, apiUrl, postForm } from '../api'
import { rateTint, rateTextColor } from '../lib/colors'
import { fmtDate, fmtInt } from '../lib/format'
import {
  LABEL_SHORT,
  LABEL_COLORS,
  PRED_ORDER,
  foldToPrediction,
} from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import { useUiStore } from '../stores/ui'
import { useMessagesStore } from '../stores/messages'
import ExportModal from './ExportModal.vue'
import MixBar from './MixBar.vue'
import WindowScores from './WindowScores.vue'

const emit = defineEmits(['open'])

const filters = useFiltersStore()
const ui = useUiStore()
const messages = useMessagesStore()

// --- reference data (lists / persons) ---
const lists = ref([]) // [{name, message_count}] — only lists with messages
const persons = ref([]) // [{id, canonical_name, message_count, addresses:[{email}]}]
const personsById = computed(() => {
  const map = {}
  for (const p of persons.value) {
    map[p.id] = { name: p.canonical_name, emails: (p.addresses || []).map((a) => a.email) }
  }
  return map
})

async function loadRefData() {
  try {
    const [l, p] = await Promise.all([get('/lists'), get('/persons')])
    lists.value = (l?.lists || []).filter((x) => x.message_count > 0)
    persons.value = p?.persons || []
  } catch {
    // Reference data is best-effort; controls still function without counts.
  }
}

// --- From dropdown: every sender (linked person or unlinked address) with
// messages in the displayed list(s), alphabetical. Reloads when the list
// filter changes; /api/senders is paged, so accumulate until `total`.
const senderOptions = ref([]) // [{value: 'p:<person_id>'|'a:<email>', label}]
let sendersToken = 0
async function loadSenderOptions() {
  const token = ++sendersToken
  try {
    const all = []
    let pageNo = 1
    let total = Infinity
    while (all.length < total && pageNo <= 10) {
      const data = await get('/senders', {
        list: filters.list || undefined,
        sort: 'name',
        order: 'asc',
        page: pageNo,
        per_page: 200,
      })
      if (token !== sendersToken) return
      const batch = data?.senders || []
      if (!batch.length) break
      all.push(...batch)
      total = data?.total ?? all.length
      pageNo += 1
    }
    senderOptions.value = all
      .filter((s) => s.message_count > 0)
      .map((s) =>
        s.type === 'person'
          ? { value: 'p:' + s.person_id, label: s.name }
          : { value: 'a:' + (s.emails?.[0] || ''), label: s.name },
      )
  } catch {
    if (token === sendersToken) senderOptions.value = []
  }
}

// Keep the active person selectable even when the list scope excludes them.
const fromOptions = computed(() => {
  const opts = senderOptions.value
  if (filters.person && !opts.some((o) => o.value === 'p:' + filters.person)) {
    const name = personsById.value[filters.person]?.name || filters.person
    return [{ value: 'p:' + filters.person, label: name }, ...opts]
  }
  return opts
})

const fromValue = computed(() => {
  if (filters.person) return 'p:' + filters.person
  const av = 'a:' + filters.address
  if (filters.address && senderOptions.value.some((o) => o.value === av)) return av
  return ''
})

// --- detection-mix (filtered summary) ---
const mixCounts = ref({})
// Messages gated under the reliability floor: the bar's trailing grey segment,
// and part of the total every share is computed over.
const mixTooShort = ref(0)
// Caption with each label's share of the total, e.g. "Human (62%) · …", plus a
// "Too short" entry when any message was gated. Percentages match the MixBar
// segment tooltips (share of the same total, rounded).
const mixCaption = computed(() => {
  const folded = foldToPrediction(mixCounts.value)
  const scored = PRED_ORDER.reduce((sum, l) => sum + (Number(folded[l]) || 0), 0)
  const total = scored + mixTooShort.value
  const part = (word, n) => (total ? `${word} (${Math.round((n / total) * 100)}%)` : word)
  const parts = PRED_ORDER.map((l) => part(LABEL_SHORT[l], Number(folded[l]) || 0))
  if (mixTooShort.value > 0) parts.push(part('Too short', mixTooShort.value))
  return parts.join(' · ')
})
let mixToken = 0
async function loadMix() {
  const token = ++mixToken
  try {
    const data = await get('/summary', filters.asParams)
    if (token === mixToken) {
      mixCounts.value = data?.label_distribution || {}
      mixTooShort.value = data?.too_short || 0
    }
  } catch {
    if (token === mixToken) {
      mixCounts.value = {}
      mixTooShort.value = 0
    }
  }
}

// --- watch all non-page filter/sort keys → refresh rows + mix ---
const filterKey = computed(() =>
  JSON.stringify([
    filters.list,
    filters.address,
    filters.person,
    filters.date_from,
    filters.date_to,
    filters.label,
    filters.min_likelihood,
    filters.max_likelihood,
    filters.q,
    filters.has_score,
    filters.cpm_min,
    filters.cpm_max,
    filters.sort,
    filters.order,
  ]),
)

watch(filterKey, () => {
  messages.refresh()
  loadMix()
})

watch(
  () => filters.list,
  () => loadSenderOptions(),
)

onMounted(() => {
  loadRefData()
  loadSenderOptions()
  messages.refresh()
  loadMix()
})

// --- active-filter border helper ---
const ACTIVE = '#2f6feb'
const IDLE = '#dfe3e8'
const b = (v) => (v !== '' && v != null ? ACTIVE : IDLE)

// --- grid columns (From collapses to 0 in anonymous mode) ---
// Date · List · From · Subject · Analysis (prediction pill + headline) ·
// AI Score (per-window scores) · Chars · Chars/min (the reply-timing rate).
// The filter row stacks its controls two deep, so Date needs room for only one
// date input and List gains the rest.
const gridCols = computed(() =>
  ui.anonymous
    ? '120px 156px 0px minmax(200px, 1fr) 230px 220px 64px 92px'
    : '120px 156px 170px minmax(200px, 1fr) 230px 220px 64px 92px',
)
const cellPad = computed(() => (ui.density === 'comfortable' ? '6px 10px' : '2px 10px'))
const fromCellPad = computed(() => (ui.anonymous ? '0' : cellPad.value))
const fromHeadPad = computed(() => (ui.anonymous ? '0' : '5px 10px 2px'))
const fromFilterPad = computed(() => (ui.anonymous ? '0' : '3px 10px 5px'))

// --- sorting ---
const dateInd = computed(() =>
  filters.sort === 'date' ? (filters.order === 'asc' ? ' ▲' : ' ▼') : '',
)
const scoreInd = computed(() =>
  filters.sort === 'fraction_ai' ? (filters.order === 'asc' ? ' ▲' : ' ▼') : '',
)
function sortBy(col) {
  // NB: the real API's sort column for the score is 'fraction_ai' (not
  // 'likelihood' as the handoff prose says) — SORT_COLUMNS in store.py.
  const order = filters.sort === col && filters.order === 'desc' ? 'asc' : 'desc'
  filters.patch({ sort: col, order })
}

// --- list combobox ---
const listInput = ref('')
const listDdOpen = ref(false)
const listInputVal = computed(() => (listDdOpen.value ? listInput.value : filters.list))
const listOptions = computed(() => {
  const q = listInput.value.trim().toLowerCase()
  const opts = lists.value
    .filter((l) => !q || l.name.toLowerCase().includes(q))
    .map((l) => ({ name: l.name, count: `${fmtInt(l.message_count)} msgs` }))
  if (!q) opts.unshift({ name: '(all lists)', count: null, all: true })
  return opts
})
const listNoMatch = computed(
  () => !lists.value.some((l) => !listInput.value.trim() || l.name.toLowerCase().includes(listInput.value.trim().toLowerCase())),
)
function openListDd() {
  listDdOpen.value = true
  listInput.value = filters.list
}
function blurListDd() {
  setTimeout(() => {
    listDdOpen.value = false
  }, 120)
}
function pickList(opt) {
  filters.setFilter('list', opt.all ? '' : opt.name)
  listDdOpen.value = false
  listInput.value = opt.all ? '' : opt.name
}

// --- person / address / subject controls ---
function setFrom(e) {
  const v = e.target.value
  if (!v) filters.patch({ person: '', address: '' })
  else if (v.startsWith('p:')) filters.setFilter('person', v.slice(2))
  else filters.setFilter('address', v.slice(2))
}
function setAddress(e) {
  filters.setFilter('address', e.target.value.trim())
}
// debounce the subject search so we do not refetch on every keystroke.
const qLocal = ref(filters.q)
watch(
  () => filters.q,
  (v) => {
    if (v !== qLocal.value) qLocal.value = v
  },
)
let qTimer = null
function setQ(e) {
  qLocal.value = e.target.value
  clearTimeout(qTimer)
  const val = e.target.value
  qTimer = setTimeout(() => filters.setFilter('q', val), 250)
}

// --- chips ---
const chips = computed(() => {
  const defs = [
    ['list', 'list'],
    ['person', 'sender'],
    ['address', 'from'],
    ['label', 'label'],
    ['q', 'q'],
    ['min_likelihood', 'min'],
    ['max_likelihood', 'max'],
    ['date_from', 'from'],
    ['date_to', 'to'],
    ['has_score', 'scored'],
    ['cpm_min', 'cpm min'],
    ['cpm_max', 'cpm max'],
  ]
  const out = []
  for (const [key, name] of defs) {
    const raw = filters[key]
    if (raw === '' || raw == null) continue
    let val = raw
    if (key === 'person') val = personsById.value[raw]?.name || raw
    else if (key === 'has_score') val = raw === 'true' ? 'yes' : 'no'
    out.push({ key, label: `${name}=${val}` })
  }
  return out
})
function clearChip(key) {
  filters.setFilter(key, '')
}
function clearAll() {
  filters.patch({
    list: '',
    address: '',
    person: '',
    date_from: '',
    date_to: '',
    label: '',
    min_likelihood: '',
    max_likelihood: '',
    q: '',
    has_score: '',
    cpm_min: '',
    cpm_max: '',
  })
}

// --- export / import ---
// Export and import carry whole messages and their pipeline state, not the
// filtered view: the export button opens a dialog for picking a format, one or
// more lists and an optional range of message dates (pre-ticked with the current
// list filter, and no other filter is applied); import ingests an uploaded JSON Lines
// dump, zstd-compressed (as exports now are), gzipped (as older ones are) or
// plain. The dialog's stats format is the exception: it writes scores and
// message metadata as CSV and is not re-importable. Both surface their outcome
// in a transient toolbar status that auto-clears.
const exportOpen = ref(false)
const exporting = ref(false)
const importing = ref(false)
const fileInput = ref(null)
const statusMsg = ref('')
const statusIsError = ref(false)
let statusTimer = null
function showStatus(msg, isError) {
  statusMsg.value = msg
  statusIsError.value = isError
  clearTimeout(statusTimer)
  statusTimer = setTimeout(() => {
    statusMsg.value = ''
  }, 8000)
}

// The dialog opens with the current list filter ticked, so the common case —
// export what I am looking at — is one click plus confirm.
const exportPreset = computed(() => (filters.list ? [filters.list] : []))

// Pull the server-provided filename out of a Content-Disposition header,
// preferring the RFC 5987 filename*=UTF-8'' form over the plain quoted one.
function filenameFromDisposition(cd) {
  if (!cd) return ''
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  if (star) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      return star[1]
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(cd)
  return plain ? plain[1] : ''
}

// `lists` empty means every list, which is what the endpoint reads an absent
// `list` param as; buildQuery repeats the key for each name it is given.
//
// The two formats differ only in their endpoint and the one extra param: the
// stats export takes the same selection and answers with a zip of CSV files,
// `pseudonymous` sent only when it is set (an empty value is dropped anyway).
async function doExport({ format, pseudonymous, lists: names, date_from, date_to }) {
  if (exporting.value) return
  const stats = format === 'stats'
  exporting.value = true
  try {
    const path = stats ? '/export/stats' : '/export'
    const params = { list: names, date_from, date_to }
    if (stats && pseudonymous) params.pseudonymous = 'true'
    const res = await fetch(apiUrl(path, params), {
      headers: { Accept: stats ? 'application/zip' : 'application/zstd' },
    })
    if (!res.ok) {
      let msg = `Export failed (${res.status})`
      try {
        const j = await res.json()
        if (j && j.error) msg = j.error
      } catch {
        // keep the status-code fallback
      }
      throw new Error(msg)
    }
    const blob = await res.blob()
    const fname =
      filenameFromDisposition(res.headers.get('Content-Disposition')) ||
      (stats ? 'mlac-stats.zip' : 'mailing-list-export.jsonl.zst')
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    showStatus(err instanceof Error ? err.message : String(err), true)
  } finally {
    exporting.value = false
    // Closed either way: the outcome of a failed export is a toolbar status,
    // which the dialog would otherwise cover.
    exportOpen.value = false
  }
}

function pickImport() {
  if (importing.value) return
  fileInput.value?.click()
}

async function onImportFile(e) {
  const file = e.target.files && e.target.files[0]
  e.target.value = '' // allow re-selecting the same file later
  if (!file) return
  importing.value = true
  try {
    const fd = new FormData()
    fd.append('file', file, file.name) // informational only: the server sniffs the content
    const data = await postForm('/import', fd)
    const parts = [
      `imported ${fmtInt(data.messages_inserted || 0)}`,
      `skipped ${fmtInt(data.messages_skipped || 0)}`,
    ]
    const updated = (data.extractions_updated || 0) + (data.scores_updated || 0)
    if (updated) parts.push(`updated ${fmtInt(updated)}`)
    if (data.body_mismatches) parts.push(`mismatches ${fmtInt(data.body_mismatches)}`)
    showStatus(parts.join(' · '), false)
    // Bring the pane in sync with the freshly imported data.
    loadRefData()
    loadSenderOptions()
    messages.refresh()
    loadMix()
  } catch (err) {
    showStatus(err instanceof Error ? err.message : String(err), true)
  } finally {
    importing.value = false
  }
}

// --- rows ---
// Reply-timing tooltips: the Chars/min cell shows the rate itself, so the
// tooltip names the band it falls in. The thresholds live in store.py
// (TIMING_*_CPM).
const TIMING_TITLES = {
  implausible:
    'Implausible: new text implies ≥ 250 chars/minute since the parent message — too fast to have been composed in the window',
  suspicious: 'Suspicious: new text implies ≥ 100 chars/minute since the parent message',
  normal: 'Normal: new text implies < 100 chars/minute since the parent message',
}
const rows = computed(() =>
  messages.items.map((m) => {
    const person = m.person
    const emails = person ? personsById.value[person.id]?.emails : null
    const fromName = ui.anonymous
      ? ''
      : person
        ? person.name
        : m.from?.display_name || m.from?.address || ''
    const fromTitle = ui.anonymous
      ? ''
      : person
        ? (emails && emails.length ? emails.join(', ') : m.from?.address || '')
        : m.from?.address || ''
    const ext = m.extraction
    const sc = m.score
    const scored = sc != null && sc.fraction_ai != null
    // Analysis pill: the prediction_short bucket (the stored label holds the
    // same value verbatim), coloured like the score badges.
    const predShort = scored ? sc.prediction_short || sc.label : ''
    // Chars/min: the rate behind the timing band, as whole chars/minute. It is
    // absent exactly where the band is (non-replies, missing parent or dates,
    // no extraction), and from 100 up the cell is tinted by the rate.
    const cpm = m.timing_cpm == null ? null : Math.round(m.timing_cpm)
    return {
      id: m.id,
      dateStr: fmtDate(m.date),
      list: m.list,
      fromName,
      fromTitle,
      subject: m.subject,
      scored,
      predShort,
      pillColor: scored ? LABEL_COLORS[predShort] || LABEL_COLORS.unscored : '',
      headline: scored ? sc.headline || '' : '',
      // Under the 50-word reliability floor: gated before Pangram, so the
      // Analysis column says so rather than leaving the row blank.
      tooShort: ext != null && ext.status === 'too_short',
      // Pangram's per-window scores, in document order. Pangram emits no
      // document-level score, so the column lists one entry per window.
      windows: scored ? sc.windows || [] : [],
      timingTitle: TIMING_TITLES[m.timing] || '',
      cpmText: cpm == null ? '—' : fmtInt(cpm),
      cpmBg: rateTint(cpm),
      cpmColor: rateTextColor(cpm),
      chars: ext && ext.char_count ? fmtInt(ext.char_count) : '—',
      person,
      address: m.from?.address || '',
    }
  }),
)

function openRow(id) {
  emit('open', id)
}
function filterList(name) {
  filters.setFilter('list', name)
}
function filterFrom(row) {
  if (row.person) filters.setFilter('person', String(row.person.id))
  else filters.setFilter('address', row.address)
}

// --- infinite scroll ---
function onScroll(e) {
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 240) {
    messages.loadMore()
  }
}

// --- empty ---
const isEmpty = computed(() => !messages.loading && messages.total === 0)
</script>

<template>
  <div class="card">
    <!-- toolbar -->
    <div class="pane-header messages-toolbar">
      <span class="pane-title">Messages</span>
      <span class="messages-count">{{ fmtInt(messages.total) }} shown</span>
      <MixBar
        :counts="mixCounts"
        :too-short="mixTooShort"
        :height="10"
        width="200px"
        :clickable="true"
        @select="(l) => filters.setFilter('label', l)"
      />
      <span class="mix-caption">{{ mixCaption }}</span>
      <span
        v-for="c in chips"
        :key="c.key"
        class="filter-chip"
      >
        {{ c.label }}
        <button class="filter-chip-x" title="Remove filter" @click="clearChip(c.key)">×</button>
      </span>
      <span style="flex: 1;"></span>
      <button
        class="clear-filters-btn"
        :disabled="!filters.hasActiveFilters"
        :style="{ opacity: filters.hasActiveFilters ? 1 : 0.4 }"
        @click="clearAll"
      >
        clear filters
      </button>
      <span
        v-if="statusMsg"
        class="io-status"
        :class="{ 'io-status-error': statusIsError }"
        >{{ statusMsg }}</span
      >
      <button
        class="io-btn"
        :disabled="exporting"
        title="Export lists (whole messages, optionally within a date range)…"
        @click="exportOpen = true"
      >
        {{ exporting ? 'exporting…' : 'export' }}
      </button>
      <button
        class="io-btn"
        :disabled="importing"
        title="Import a list export (.jsonl.zst / .jsonl / older .gz)…"
        @click="pickImport"
      >
        {{ importing ? 'importing…' : 'import' }}
      </button>
      <input
        ref="fileInput"
        type="file"
        accept=".jsonl,.zst,.jsonl.zst,.gz,.jsonl.gz,application/zstd,application/gzip"
        style="display: none;"
        @change="onImportFile"
      />
      <ExportModal
        :open="exportOpen"
        :lists="lists"
        :preset="exportPreset"
        :busy="exporting"
        @close="exportOpen = false"
        @export="doExport"
      />
    </div>

    <!-- scroll region -->
    <div class="messages-scroll" @scroll="onScroll">
      <div style="min-width: 1320px;">
        <div class="messages-sticky">
          <!-- header row -->
          <div class="messages-grid messages-head" :style="{ gridTemplateColumns: gridCols }">
            <div class="col-head sortable" @click="sortBy('date')">Date{{ dateInd }}</div>
            <div class="col-head">List</div>
            <div class="col-head" :style="{ padding: fromHeadPad, overflow: 'hidden' }">
              <template v-if="!ui.anonymous">From</template>
            </div>
            <div class="col-head">Subject</div>
            <div class="col-head">Analysis</div>
            <div class="col-head sortable" @click="sortBy('fraction_ai')">
              AI Score (Confidence){{ scoreInd }}
            </div>
            <div class="col-head" style="text-align: right;">Chars</div>
            <div class="col-head" style="text-align: right;">Chars/min</div>
          </div>
          <!-- column filter row -->
          <div
            class="messages-grid messages-filter-row"
            :style="{ gridTemplateColumns: gridCols }"
          >
            <div class="fcell">
              <input
                type="date"
                :value="filters.date_from"
                title="From date"
                :style="{ border: `1px solid ${b(filters.date_from)}` }"
                class="fctl fctl-date"
                @change="(e) => filters.setFilter('date_from', e.target.value)"
              />
              <input
                type="date"
                :value="filters.date_to"
                title="To date"
                :style="{ border: `1px solid ${b(filters.date_to)}` }"
                class="fctl fctl-date"
                @change="(e) => filters.setFilter('date_to', e.target.value)"
              />
            </div>
            <div class="fcell" style="position: relative;">
              <input
                type="text"
                placeholder="any list…"
                :value="listInputVal"
                class="fctl fctl-mono"
                style="width: 100%;"
                :style="{ border: `1px solid ${b(filters.list)}` }"
                @input="(e) => { listInput = e.target.value; listDdOpen = true }"
                @focus="openListDd"
                @blur="blurListDd"
              />
              <div v-if="listDdOpen" class="list-dropdown">
                <div
                  v-for="o in listOptions"
                  :key="o.name"
                  class="list-dropdown-item"
                  @mousedown="pickList(o)"
                >
                  <span style="font-weight: 600; color: #1f52bf;">{{ o.name }}</span>
                  <span v-if="o.count" style="color: #8a929b;">{{ o.count }}</span>
                </div>
                <div v-if="listNoMatch" class="list-dropdown-empty">no matching lists</div>
              </div>
            </div>
            <div class="fcell" :style="{ padding: fromFilterPad, overflow: 'hidden' }">
              <template v-if="!ui.anonymous">
                <select
                  :value="fromValue"
                  title="Sender"
                  class="fctl"
                  style="width: 100%;"
                  :style="{ border: `1px solid ${b(filters.person || filters.address)}` }"
                  @change="setFrom"
                >
                  <option value="">anyone</option>
                  <option v-for="o in fromOptions" :key="o.value" :value="o.value">
                    {{ o.label }}
                  </option>
                </select>
                <input
                  type="text"
                  placeholder="exact email"
                  :value="filters.address"
                  class="fctl fctl-mono"
                  style="width: 100%;"
                  :style="{ border: `1px solid ${b(filters.address)}` }"
                  @change="setAddress"
                />
              </template>
            </div>
            <div class="fcell">
              <input
                type="search"
                placeholder="subject / text…"
                :value="qLocal"
                class="fctl"
                style="width: 100%;"
                :style="{ border: `1px solid ${b(filters.q)}` }"
                @input="setQ"
              />
            </div>
            <div class="fcell">
              <select
                :value="filters.label"
                title="Analysis"
                class="fctl"
                style="width: 100%;"
                :style="{ border: `1px solid ${b(filters.label)}` }"
                @change="(e) => filters.setFilter('label', e.target.value)"
              >
                <option value="">any</option>
                <option value="Human">Human</option>
                <option value="Mixed">Mixed</option>
                <option value="AI">AI</option>
              </select>
            </div>
            <!-- Scored / unscored: it lived under Extraction, which is gone. -->
            <div class="fcell">
              <select
                :value="filters.has_score"
                title="Scoring status"
                class="fctl"
                style="width: 100%;"
                :style="{ border: `1px solid ${b(filters.has_score)}` }"
                @change="(e) => filters.setFilter('has_score', e.target.value)"
              >
                <option value="">any</option>
                <option value="true">scored</option>
                <option value="false">unscored</option>
              </select>
            </div>
            <div class="fcell"></div>
            <!-- Chars/min: an inclusive range on the reply-timing rate. Either
                 bound alone drops every message with no rate. -->
            <div class="fcell">
              <input
                type="number"
                min="0"
                step="1"
                placeholder="min"
                :value="filters.cpm_min"
                title="Minimum chars/min"
                class="fctl fctl-num"
                :style="{ border: `1px solid ${b(filters.cpm_min)}` }"
                @change="(e) => filters.setFilter('cpm_min', e.target.value)"
              />
              <input
                type="number"
                min="0"
                step="1"
                placeholder="max"
                :value="filters.cpm_max"
                title="Maximum chars/min"
                class="fctl fctl-num"
                :style="{ border: `1px solid ${b(filters.cpm_max)}` }"
                @change="(e) => filters.setFilter('cpm_max', e.target.value)"
              />
            </div>
          </div>
        </div>

        <!-- rows -->
        <div>
          <div
            v-for="m in rows"
            :key="m.id"
            class="messages-grid messages-row"
            :style="{ gridTemplateColumns: gridCols }"
            @click="openRow(m.id)"
          >
            <div class="cell cell-mono cell-muted" :style="{ padding: cellPad, whiteSpace: 'nowrap' }">
              {{ m.dateStr }}
            </div>
            <div class="cell cell-ellipsis" :style="{ padding: cellPad }">
              <a href="#" class="cell-link cell-link-mono" title="Filter to this list" @click.prevent.stop="filterList(m.list)">{{ m.list }}</a>
            </div>
            <div class="cell cell-ellipsis" :style="{ padding: fromCellPad }">
              <a
                v-if="!ui.anonymous"
                href="#"
                class="cell-link"
                :title="m.fromTitle"
                @click.prevent.stop="filterFrom(m)"
                >{{ m.fromName }}</a
              >
            </div>
            <div class="cell cell-ellipsis" :style="{ padding: cellPad }">{{ m.subject }}</div>
            <!-- Pill and headline sit in fixed sub-columns, so every headline
                 starts at the same x however wide its pill is. -->
            <div class="cell analysis-cell" :style="{ padding: cellPad }">
              <span class="analysis-pill-slot">
                <span v-if="m.scored" class="pred-pill" :style="{ background: m.pillColor }">{{
                  m.predShort
                }}</span>
                <span v-else class="cell-dash">—</span>
              </span>
              <span v-if="m.scored" class="headline-text" :title="m.headline">{{ m.headline }}</span>
              <span
                v-else-if="m.tooShort"
                class="too-short-text"
                title="Under the 50-word reliability floor — not sent to Pangram"
                >Too short to test</span
              >
            </div>
            <div class="cell" :style="{ padding: cellPad, minWidth: 0 }">
              <WindowScores :windows="m.windows" />
            </div>
            <div
              class="cell cell-mono cell-muted"
              :style="{ padding: cellPad, textAlign: 'right' }"
            >
              {{ m.chars }}
            </div>
            <!-- Chars/min: the reply-timing rate, in the Chars column's face,
                 with a purple tint per hundred from 100 chars/min up. -->
            <div
              class="cell cell-mono cell-muted cell-rate"
              :style="{ padding: cellPad, background: m.cpmBg, color: m.cpmColor }"
              :title="m.timingTitle"
            >
              {{ m.cpmText }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="isEmpty" class="messages-empty">No messages match the current filters.</div>
  </div>
</template>

<style scoped>
.messages-toolbar {
  gap: 12px;
  flex-wrap: wrap;
}
.messages-count {
  font-size: 11.5px;
  color: #626a72;
  font-family: var(--mono);
}
.mix-caption {
  font-size: 10px;
  color: #8a929b;
  font-family: var(--mono);
}
.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #eaf1fe;
  color: #1f52bf;
  border: 1px solid #c9dbfa;
  border-radius: 3px;
  padding: 0 2px 0 6px;
  font-size: 10.5px;
  font-weight: 600;
  font-family: var(--mono);
}
.filter-chip-x {
  border: none;
  background: none;
  color: #1f52bf;
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 0 3px;
}
.clear-filters-btn {
  font-size: 11px;
  font-weight: 600;
  border: none;
  background: none;
  color: #2f6feb;
  cursor: pointer;
  padding: 0;
}
/* export / import: same lightweight text-button look as clear-filters, kept in
   the toolbar's compact rhythm. */
.io-btn {
  font-size: 11px;
  font-weight: 600;
  border: none;
  background: none;
  color: #2f6feb;
  cursor: pointer;
  padding: 0;
}
.io-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.io-status {
  font-size: 10px;
  color: #626a72;
  font-family: var(--mono);
}
.io-status-error {
  color: #b23636;
}
.messages-scroll {
  overflow: auto;
  flex: 1;
  min-height: 0;
}
.messages-sticky {
  position: sticky;
  top: 0;
  z-index: 5;
  background: #ffffff;
}
.messages-grid {
  display: grid;
  align-items: center;
}
.messages-head {
  border-bottom: 1px solid #eef0f3;
  align-items: end;
}
.col-head {
  padding: 5px 10px 2px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #626a72;
  white-space: nowrap;
}
.col-head.sortable {
  cursor: pointer;
  user-select: none;
}
.col-head.sortable:hover {
  color: #1c2024;
}
/* Two rows deep: each cell stacks its controls, so the Date and From columns
   hold two full-width controls instead of two half-width ones side by side. */
.messages-filter-row {
  border-bottom: 1px solid #e2e5e9;
  background: #fafbfc;
  align-items: stretch;
}
.fcell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 3px 10px 5px;
  min-width: 0;
}
.fctl {
  font-size: 11px;
  height: 21px;
  padding: 0 5px;
  border-radius: 3px;
  background: #ffffff;
  color: #1c2024;
  box-sizing: border-box;
}
.fctl-date {
  font-size: 10px;
  padding: 0 3px;
  width: 100%;
  min-width: 0;
  color: #626a72;
}
.fctl-mono {
  font-family: var(--mono);
}
.fctl-num {
  font-size: 10.5px;
  padding: 0 3px;
  width: 100%;
  min-width: 0;
  text-align: right;
  /* The stepper arrows would eat most of a 92px column; the inputs stay
     type="number" for the numeric keypad and the browser's own validation. */
  -moz-appearance: textfield;
  appearance: textfield;
}
.fctl-num::-webkit-outer-spin-button,
.fctl-num::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
select.fctl {
  padding: 0 2px;
}
.list-dropdown {
  position: absolute;
  left: 10px;
  right: 10px;
  top: 27px;
  z-index: 40;
  background: #ffffff;
  border: 1px solid #e2e5e9;
  border-radius: 4px;
  box-shadow: 0 8px 24px rgba(15, 18, 22, 0.16);
  max-height: 220px;
  overflow-y: auto;
}
.list-dropdown-item {
  padding: 4px 8px;
  font-size: 11px;
  font-family: var(--mono);
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.list-dropdown-item:hover {
  background: #f4f7fb;
}
.list-dropdown-empty {
  padding: 4px 8px;
  font-size: 10.5px;
  color: #8a929b;
}
.messages-row {
  border-bottom: 1px solid #f2f4f6;
  cursor: pointer;
  font-size: 11.5px;
}
.messages-row:hover {
  background: #f4f7fb;
}
.cell {
  min-width: 0;
}
.cell-mono {
  font-family: var(--mono);
  font-size: 11px;
}
.cell-muted {
  color: #626a72;
}
.cell-ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cell-link {
  font-weight: 500;
  color: var(--text-name);
}
.cell-link:hover {
  color: var(--accent);
}
.cell-link-mono {
  font-family: var(--mono);
}
/* Analysis column: the prediction pill in a fixed slot, then the headline, so
   the headlines line up down the column. */
.analysis-cell {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.analysis-pill-slot {
  display: flex;
  align-items: center;
  min-width: 0;
}
/* The prediction_short bucket as a pill (Human / Mixed / AI), followed by
   Pangram's free-text headline as plain, uncoloured text. */
.pred-pill {
  flex: none;
  padding: 0 7px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  color: #ffffff;
}
.headline-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: #1c2024;
}
/* Gated as too short to score: set in the monospace face, unlike the
   proportional headline, so it reads as a status rather than a verdict. */
.too-short-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mono);
  font-size: 10.5px;
  color: #8a929b;
}
.cell-dash {
  color: #b3b9c0;
}
/* Chars/min: right-aligned like Chars, but stretched to the row height so its
   tint reads as a band across the whole row rather than only behind the text. */
.cell-rate {
  align-self: stretch;
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.messages-empty {
  padding: 28px;
  text-align: center;
  color: #8a929b;
}
</style>
