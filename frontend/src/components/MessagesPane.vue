<script setup>
// The messages pane: toolbar (title, count, detection-mix bar, filter chips,
// clear), a horizontally-scrolling grid table with a sticky header + column
// filter row, infinite-scroll rows, and a loaded-count footer. It is the primary
// writer of the filters store and the driver of the shared messages store.
import { ref, computed, watch, onMounted } from 'vue'

import { get, apiUrl, postForm } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { LABEL_ORDER, LABEL_SHORT } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import { useUiStore } from '../stores/ui'
import { useMessagesStore } from '../stores/messages'
import MixBar from './MixBar.vue'
import ScoreCell from './ScoreCell.vue'

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
// Caption with each label's share of the scored total, e.g. "Human (62%) · …".
// Percentages match the MixBar segment tooltips (share of scored, rounded).
const mixCaption = computed(() => {
  const total = LABEL_ORDER.reduce((sum, l) => sum + (Number(mixCounts.value[l]) || 0), 0)
  return LABEL_ORDER.map((l) => {
    const word = LABEL_SHORT[l]
    if (!total) return word
    const pct = Math.round(((Number(mixCounts.value[l]) || 0) / total) * 100)
    return `${word} (${pct}%)`
  }).join(' · ')
})
let mixToken = 0
async function loadMix() {
  const token = ++mixToken
  try {
    const data = await get('/summary', filters.asParams)
    if (token === mixToken) mixCounts.value = data?.label_distribution || {}
  } catch {
    if (token === mixToken) mixCounts.value = {}
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
const gridCols = computed(() =>
  ui.anonymous
    ? '176px 100px 0px minmax(240px, 1fr) 140px 172px 64px'
    : '176px 100px 170px minmax(240px, 1fr) 140px 172px 64px',
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
  })
}

// --- export / import ---
// Export and import operate on whole lists (the pipeline state), not the
// filtered message subset: export sends the current list-name filter (or all
// lists when none is set); import ingests an uploaded .jsonl(.gz) dump. Both
// surface their outcome in a transient toolbar status that auto-clears.
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

const exportTitle = computed(() =>
  filters.list ? `Export list '${filters.list}' (the whole list)…` : 'Export all lists…',
)

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

async function doExport() {
  if (exporting.value) return
  exporting.value = true
  try {
    const res = await fetch(apiUrl('/export', filters.list ? { list: filters.list } : undefined), {
      headers: { Accept: 'application/gzip' },
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
      filenameFromDisposition(res.headers.get('Content-Disposition')) || 'mailing-list-export.jsonl.gz'
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
    fd.append('file', file, file.name) // preserve the .gz-bearing filename
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
    let extStr = ''
    if (ext) extStr = ext.status === 'ok' ? `${ext.status}·${ext.method}` : ext.status
    return {
      id: m.id,
      dateStr: fmtDate(m.date),
      list: m.list,
      fromName,
      fromTitle,
      subject: m.subject,
      extStr,
      score: m.score,
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

// --- footer / empty ---
const loadedNote = computed(() => {
  const loaded = messages.items.length
  const total = messages.total
  return (
    `${fmtInt(loaded)} of ${fmtInt(total)} loaded` +
    (loaded < total ? ' · scroll for more' : '')
  )
})
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
        :title="exportTitle"
        @click="doExport"
      >
        {{ exporting ? 'exporting…' : 'export' }}
      </button>
      <button
        class="io-btn"
        :disabled="importing"
        title="Import a list export (.jsonl / .gz)…"
        @click="pickImport"
      >
        {{ importing ? 'importing…' : 'import' }}
      </button>
      <input
        ref="fileInput"
        type="file"
        accept=".jsonl,.gz,.jsonl.gz,application/gzip"
        style="display: none;"
        @change="onImportFile"
      />
    </div>

    <!-- scroll region -->
    <div class="messages-scroll" @scroll="onScroll">
      <div style="min-width: 1080px;">
        <div class="messages-sticky">
          <!-- header row -->
          <div class="messages-grid messages-head" :style="{ gridTemplateColumns: gridCols }">
            <div class="col-head sortable" @click="sortBy('date')">Date{{ dateInd }}</div>
            <div class="col-head">List</div>
            <div class="col-head" :style="{ padding: fromHeadPad, overflow: 'hidden' }">
              <template v-if="!ui.anonymous">From</template>
            </div>
            <div class="col-head">Subject</div>
            <div class="col-head">Extraction</div>
            <div class="col-head sortable" style="text-align: right;" @click="sortBy('fraction_ai')">
              Score{{ scoreInd }}
            </div>
            <div class="col-head" style="text-align: right;">Chars</div>
          </div>
          <!-- column filter row -->
          <div
            class="messages-grid messages-filter-row"
            :style="{ gridTemplateColumns: gridCols }"
          >
            <div style="padding: 3px 10px 5px;">
              <span style="display: flex; gap: 3px;">
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
              </span>
            </div>
            <div style="padding: 3px 10px 5px; position: relative;">
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
            <div :style="{ padding: fromFilterPad, overflow: 'hidden' }">
              <span v-if="!ui.anonymous" style="display: flex; gap: 3px;">
                <select
                  :value="fromValue"
                  title="Sender"
                  class="fctl"
                  style="width: 92px; flex: none;"
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
                  style="flex: 1; min-width: 0;"
                  :style="{ border: `1px solid ${b(filters.address)}` }"
                  @change="setAddress"
                />
              </span>
            </div>
            <div style="padding: 3px 10px 5px;">
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
            <div style="padding: 3px 10px 5px;">
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
            <div style="padding: 3px 10px 5px;">
              <span style="display: flex; gap: 3px; justify-content: flex-end;">
                <select
                  :value="filters.label"
                  title="Label"
                  class="fctl"
                  style="flex: 1; min-width: 0;"
                  :style="{ border: `1px solid ${b(filters.label)}` }"
                  @change="(e) => filters.setFilter('label', e.target.value)"
                >
                  <option value="">any</option>
                  <option value="AI">AI</option>
                  <option value="AI-Assisted">AI-Asst</option>
                  <option value="Mixed">Mixed</option>
                  <option value="Human">Human</option>
                </select>
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  placeholder="min"
                  :value="filters.min_likelihood"
                  title="Min fraction AI"
                  class="fctl fctl-num"
                  :style="{ border: `1px solid ${b(filters.min_likelihood)}` }"
                  @change="(e) => filters.setFilter('min_likelihood', e.target.value)"
                />
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  placeholder="max"
                  :value="filters.max_likelihood"
                  title="Max fraction AI"
                  class="fctl fctl-num"
                  :style="{ border: `1px solid ${b(filters.max_likelihood)}` }"
                  @change="(e) => filters.setFilter('max_likelihood', e.target.value)"
                />
              </span>
            </div>
            <div style="padding: 3px 10px 5px;"></div>
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
            <div class="cell cell-mono" :style="{ padding: cellPad, whiteSpace: 'nowrap', color: '#8a929b' }">
              {{ m.extStr }}
            </div>
            <div class="cell" :style="{ padding: cellPad, textAlign: 'right', whiteSpace: 'nowrap' }">
              <ScoreCell :score="m.score" />
            </div>
            <div
              class="cell cell-mono cell-muted"
              :style="{ padding: cellPad, textAlign: 'right' }"
            >
              {{ m.chars }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="isEmpty" class="messages-empty">No messages match the current filters.</div>
    <div class="messages-footer">{{ loadedNote }}</div>
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
.messages-filter-row {
  border-bottom: 1px solid #e2e5e9;
  background: #fafbfc;
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
  width: 50%;
  min-width: 0;
  color: #626a72;
}
.fctl-mono {
  font-family: var(--mono);
}
.fctl-num {
  font-size: 10.5px;
  padding: 0 3px;
  width: 42px;
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
.messages-empty {
  padding: 28px;
  text-align: center;
  color: #8a929b;
}
.messages-footer {
  flex: none;
  padding: 3px 12px;
  border-top: 1px solid #e2e5e9;
  background: #fafbfc;
  font-size: 10.5px;
  color: #8a929b;
  font-family: var(--mono);
}
</style>
