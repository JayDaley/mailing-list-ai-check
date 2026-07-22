<script setup>
// The left / lower "Lists" pane. Two mutually exclusive modes driven by the
// shared filter state:
//
//   1. Lists index (default)  — every list with a mix bar (GET /api/lists).
//      "+ Add list" and "Regenerate index" live in the pane header, in every
//      mode (POST /api/pull, POST /api/lists/regenerate).
//   2. List stats (a `list` filter) — per-list aggregates from GET /api/summary
//      (stat tiles, detection-mix summary, last-50-messages rug, pull footer).
//
// Sender (person/address) details live in the Senders pane, not here.
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get, postJson } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { MIX_CAPTION, labelColor } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import MixBar from './MixBar.vue'
import MixSummary from './MixSummary.vue'

const filters = useFiltersStore()
const route = useRoute()
const router = useRouter()

// --- mode -------------------------------------------------------------------
const mode = computed(() => (filters.list ? 'list' : 'index'))
const contextSub = computed(() => (mode.value === 'list' ? 'per-list aggregates' : 'lists index'))

// --- data -------------------------------------------------------------------
const lists = ref([]) // [{name, message_count, label_counts, last_synced_at, ...}]
const summary = ref(null) // GET /api/summary for the selected list
const summaryLoading = ref(false)
const summaryError = ref(null)

async function loadLists() {
  try {
    const data = await get('/lists')
    lists.value = data?.lists || []
  } catch {
    lists.value = []
  }
}

// The card aggregates over the list alone, regardless of the other filters
// active on the messages table.
const summaryParams = computed(() => (mode.value === 'list' ? { list: filters.list } : null))

let summaryToken = 0
async function loadSummary() {
  const params = summaryParams.value
  if (!params) return
  const token = ++summaryToken
  summaryLoading.value = true
  summaryError.value = null
  try {
    const data = await get('/summary', params)
    if (token === summaryToken) summary.value = data
  } catch (err) {
    if (token === summaryToken) {
      summary.value = null
      summaryError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    if (token === summaryToken) summaryLoading.value = false
  }
}

// --- rug plot (last 50 messages of the selected list) -----------------------
const rugMsgs = ref([]) // oldest → newest
let rugToken = 0
async function loadRug() {
  if (!filters.list) return
  const token = ++rugToken
  try {
    const data = await get('/messages', {
      list: filters.list,
      per_page: 50,
      sort: 'date',
      order: 'desc',
    })
    // The API returns newest-first; the rug reads oldest → newest.
    if (token === rugToken) rugMsgs.value = (data?.messages || []).slice().reverse()
  } catch {
    if (token === rugToken) rugMsgs.value = []
  }
}

const rugBars = computed(() =>
  rugMsgs.value.map((m) => {
    const label = m.score?.label || null
    return {
      id: m.id,
      color: labelColor(label),
      title: `${fmtDate(m.date)} · ${label || 'unscored'} — ${m.subject || '(no subject)'}`,
    }
  }),
)

function openRugMessage(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

// Refetch whenever the selected list changes.
watch(
  () => filters.list,
  () => {
    loadSummary()
    loadRug()
  },
)

onMounted(() => {
  loadLists()
  loadSummary()
  loadRug()
})

// --- small helpers ----------------------------------------------------------
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// Compact "MMM DD HH:mm" stamp for last_synced_at (ISO), or "never".
function fmtSynced(iso) {
  if (!iso) return 'never'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mm = MONTHS[d.getMonth()]
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm} ${dd} ${hh}:${mi}`
}

// --- pull / regenerate status (shared by index + list-stats footer) --------
const pulling = ref(false)
const pullMsg = ref('')
const regenerating = ref(false)
const regenMsg = ref('')
const statusMsg = computed(() =>
  pulling.value
    ? 'pulling and scoring…'
    : regenerating.value
      ? 'enumerating server folders…'
      : pullMsg.value || regenMsg.value,
)

function pullSummaryLine(name, r) {
  const scored = r.scoring_skipped ? 'skipped' : fmtInt(r.scored)
  return (
    `${name}: fetched ${fmtInt(r.fetched)} · extracted ${fmtInt(r.extracted)} · ` +
    `scored ${scored} · too short ${fmtInt(r.too_short)}`
  )
}

async function runPull(name, count) {
  if (pulling.value) return false
  pulling.value = true
  pullMsg.value = ''
  regenMsg.value = ''
  let ok = false
  try {
    const r = await postJson('/pull', { list: name, count })
    pullMsg.value = pullSummaryLine(name, r)
    ok = true
    await loadLists()
    if (mode.value === 'list') await Promise.all([loadSummary(), loadRug()])
  } catch (err) {
    pullMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    pulling.value = false
  }
  return ok
}

// --- index mode -------------------------------------------------------------
// Two-state visibility toggle over the already-loaded lists array (client-side,
// not persisted). "Show active" keeps only tracked, recently-active lists;
// "Show all" shows every enumerated folder. Default: active.
const showActive = ref(true)
const ACTIVE_WINDOW_MS = 90 * 24 * 60 * 60 * 1000

// A list is "active" when it is tracked (message_count > 0) AND either has no
// server-activity timestamp yet (first check not run → stay visible) or its
// newest server message is within the last 90 days.
function isActiveList(l) {
  if (!(Number(l.message_count) > 0)) return false
  const lm = l.last_message_at
  if (lm == null) return true
  const t = new Date(lm).getTime()
  if (Number.isNaN(t)) return true // unparseable → keep visible rather than hide data
  return t >= Date.now() - ACTIVE_WINDOW_MS
}

const filteredLists = computed(() =>
  showActive.value ? lists.value.filter(isActiveList) : lists.value,
)
const totalListCount = computed(() => lists.value.length)
// Filtering hid everything, but the API did return rows → show a friendly hint
// instead of a bare, rowless index.
const showActiveEmpty = computed(
  () => showActive.value && lists.value.length > 0 && filteredLists.value.length === 0,
)

const listRows = computed(() =>
  [...filteredLists.value]
    .sort((a, b) => (b.message_count || 0) - (a.message_count || 0))
    .map((l) => ({
      name: l.name,
      count: fmtInt(l.message_count || 0),
      counts: l.label_counts || {},
      synced: fmtSynced(l.last_synced_at),
    })),
)

const pullFormOpen = ref(false)
const pullName = ref('')
const pullCount = ref(50)
function openPullForm() {
  pullFormOpen.value = true
  pullMsg.value = ''
  regenMsg.value = ''
}
function cancelPull() {
  pullFormOpen.value = false
}
function submitPull() {
  const name = pullName.value.trim()
  if (!name) {
    pullMsg.value = 'enter a list name'
    return
  }
  let count = parseInt(pullCount.value, 10)
  if (!Number.isFinite(count)) count = 50
  count = Math.min(1000, Math.max(1, count))
  runPull(name, count).then((ok) => {
    if (ok) {
      pullFormOpen.value = false
      pullName.value = ''
    }
  })
}
// Summarise the POST /api/lists/regenerate response. Appends the per-list
// server-activity check counts when the backend reports them.
function regenSummary(c) {
  let msg =
    `${fmtInt(c.total)} lists · +${fmtInt(c.added)} added · ` +
    `+${fmtInt(c.restored)} restored · −${fmtInt(c.deleted)} removed`
  if (c.activity_checked) msg += ` · activity checked ${fmtInt(c.activity_checked)}`
  if (c.activity_failed > 0) msg += ` · activity failed ${fmtInt(c.activity_failed)}`
  return msg
}
async function regenerate() {
  if (regenerating.value) return
  regenerating.value = true
  regenMsg.value = ''
  pullMsg.value = ''
  try {
    const c = await postJson('/lists/regenerate', {})
    regenMsg.value = regenSummary(c)
    await loadLists()
  } catch (err) {
    regenMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    regenerating.value = false
  }
}

// --- list-stats mode --------------------------------------------------------
const listMeta = computed(() => lists.value.find((l) => l.name === filters.list) || null)
const listCard = computed(() => {
  const s = summary.value
  if (!s) return null
  return {
    name: filters.list,
    synced: fmtSynced(listMeta.value?.last_synced_at),
    total: fmtInt(s.total),
    scored: fmtInt(s.scored),
    mix: s.label_distribution || {},
  }
})

function closeList() {
  filters.setFilter('list', '')
}
function pullList() {
  runPull(filters.list, 50)
}
</script>

<template>
  <div class="card">
    <div class="pane-header">
      <span class="pane-title">Lists</span>
      <span class="pane-subtitle">{{ contextSub }}</span>
      <span
        v-if="mode !== 'list'"
        class="seg"
        role="group"
        aria-label="List visibility"
      >
        <button
          type="button"
          class="seg-btn"
          :class="{ 'seg-on': showActive }"
          :aria-pressed="showActive"
          @click="showActive = true"
        >
          Show active
        </button>
        <button
          type="button"
          class="seg-btn"
          :class="{ 'seg-on': !showActive }"
          :aria-pressed="!showActive"
          @click="showActive = false"
        >
          Show all ({{ fmtInt(totalListCount) }})
        </button>
      </span>
      <span class="header-actions">
        <button
          v-if="!pullFormOpen"
          class="btn-primary"
          :disabled="pulling"
          @click="openPullForm"
        >
          + Add list
        </button>
        <button class="btn-secondary" :disabled="regenerating" @click="regenerate">
          Regenerate index
        </button>
      </span>
    </div>
    <div class="pane-body ctx-body">
      <div v-if="pullFormOpen" class="pull-form">
        <div class="pull-form-row">
          <input
            type="text"
            placeholder="list name"
            :value="pullName"
            class="pull-name mono"
            @input="(e) => (pullName = e.target.value)"
          />
          <input
            type="number"
            min="1"
            max="1000"
            :value="pullCount"
            class="pull-count"
            @input="(e) => (pullCount = e.target.value)"
          />
          <button class="btn-primary btn-go" :disabled="pulling" @click="submitPull">Go</button>
          <button class="btn-cancel" @click="cancelPull">✕</button>
        </div>
        <div class="pull-note">Scoring sends extracted text to the paid Pangram API.</div>
      </div>
      <div v-if="statusMsg && mode !== 'list'" class="status-mono status-mono-dark status-line">
        {{ statusMsg }}
      </div>

      <!-- list stats -->
      <template v-if="mode === 'list'">
        <div class="card-head">
          <div class="card-name mono">{{ filters.list }}</div>
          <button class="close-x" title="Clear list filter" @click="closeList">×</button>
        </div>
        <div v-if="summaryLoading && !listCard" class="ctx-status">loading…</div>
        <div v-else-if="summaryError" class="ctx-status ctx-error">{{ summaryError }}</div>
        <template v-else-if="listCard">
          <div class="synced-line">last synced {{ listCard.synced }}</div>
          <div class="stats-row">
            <div class="tile">
              <div class="tile-val tile-val-sm">{{ listCard.total }}</div>
              <div class="tile-cap tile-cap-sm">Msgs</div>
            </div>
            <div class="tile">
              <div class="tile-val tile-val-sm">{{ listCard.scored }}</div>
              <div class="tile-cap tile-cap-sm">Scored</div>
            </div>
            <MixSummary
              :counts="listCard.mix"
              :clickable="true"
              class="stats-mix"
              @select="(l) => filters.setFilter('label', l)"
            />
          </div>
          <div class="section-head">
            Last {{ rugBars.length }} messages
            <span class="rug-note">oldest → newest · one bar per email</span>
          </div>
          <div class="rug">
            <span
              v-for="b in rugBars"
              :key="b.id"
              class="rug-bar"
              :style="{ background: b.color }"
              :title="b.title"
              @click="openRugMessage(b.id)"
            ></span>
          </div>
          <div class="pull-footer">
            <button class="btn-secondary" :disabled="pulling" @click="pullList">Pull 50 newest</button>
            <span class="status-mono">{{ statusMsg }}</span>
          </div>
        </template>
      </template>

      <!-- lists index -->
      <template v-else>
        <div class="index-caption">
          <span>List</span>
          <span style="text-align: right;">Msgs</span>
          <span>{{ MIX_CAPTION }}</span>
          <span style="text-align: right;">Synced</span>
        </div>
        <div v-if="showActiveEmpty" class="index-empty">
          No active lists — add a list, or switch to Show all.
        </div>
        <div
          v-for="l in listRows"
          :key="l.name"
          class="index-row hover-row"
          @click="filters.setFilter('list', l.name)"
        >
          <span class="index-name mono">{{ l.name }}</span>
          <span class="index-count mono">{{ l.count }}</span>
          <MixBar :counts="l.counts" :height="9" />
          <span class="index-synced mono">{{ l.synced }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.ctx-body {
  padding: 10px 12px;
}
.mono {
  font-family: var(--mono);
}
.ctx-status {
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 4px 0;
}
.ctx-error {
  color: var(--danger);
}

/* --- card head (list stats) --- */
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.card-name {
  font-size: 14px;
  font-weight: 700;
}
.close-x {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}
.synced-line {
  font-size: 10.5px;
  color: var(--text-muted);
}

/* --- stat tiles + mix summary --- */
.stats-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 8px;
}
.stats-mix {
  flex: 1;
  padding-top: 1px;
}
.tile {
  background: var(--tile);
  border-radius: 4px;
  padding: 5px 8px;
  flex: none;
  min-width: 52px;
}
.tile-val {
  font-size: 14px;
  font-weight: 700;
  font-family: var(--mono);
}
.tile-val-sm {
  font-size: 13.5px;
}
.tile-cap {
  font-size: 9.5px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 700;
}
.tile-cap-sm {
  font-size: 9px;
}

/* --- section headings --- */
.section-head {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin: 12px 0 4px;
}
.rug-note {
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  margin-left: 6px;
}

/* --- rug plot --- */
.rug {
  display: flex;
  gap: 2px;
  height: 30px;
  align-items: stretch;
}
.rug-bar {
  flex: 1;
  min-width: 2px;
  max-width: 12px;
  border-radius: 1px;
  cursor: pointer;
}
.rug-bar:hover {
  opacity: 0.75;
}
.hover-row:hover {
  background: var(--hover-row);
}

/* --- pull footer (list stats) --- */
.pull-footer {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-mono {
  font-size: 10.5px;
  color: var(--text-muted);
  font-family: var(--mono);
}
.status-mono-dark {
  color: var(--text-secondary);
}

/* --- buttons --- */
.btn-primary {
  font-size: 11px;
  font-weight: 700;
  padding: 3px 9px;
  border: none;
  border-radius: 3px;
  background: var(--accent);
  color: #ffffff;
  cursor: pointer;
}
.btn-secondary {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
}

/* --- index mode --- */
.index-caption {
  display: grid;
  grid-template-columns: 1fr 44px 150px 88px;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding: 2px 0;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}
.index-row {
  display: grid;
  grid-template-columns: 1fr 44px 150px 88px;
  gap: 6px;
  align-items: center;
  border-bottom: 1px solid var(--border-row);
  cursor: pointer;
  padding: 3px 0;
  font-size: 11.5px;
}
.index-name {
  font-weight: 500;
  color: var(--text-name);
}
.index-row:hover .index-name {
  color: var(--accent);
}
.index-count {
  text-align: right;
  color: var(--text-secondary);
}
.index-synced {
  text-align: right;
  color: var(--text-muted);
  font-size: 10.5px;
  white-space: nowrap;
}
.index-empty {
  padding: 10px 2px;
  font-size: 11.5px;
  color: var(--text-muted);
}

/* --- segmented "Show active / Show all" toggle --- */
.seg {
  display: inline-flex;
  align-items: stretch;
  height: 22px;
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.seg-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 0 9px;
  border: none;
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}
.seg-btn + .seg-btn {
  border-left: 1px solid var(--border);
}
.seg-btn.seg-on {
  background: var(--accent);
  color: #ffffff;
}

/* --- header actions + pull form --- */
.header-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.status-line {
  margin-bottom: 6px;
}
.pull-form {
  margin-bottom: 8px;
  background: var(--tile);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 7px 9px;
}
.pull-form-row {
  display: flex;
  gap: 5px;
  align-items: center;
}
.pull-name {
  font-size: 11px;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  flex: 1;
  min-width: 0;
}
.pull-count {
  font-size: 11px;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 54px;
}
.btn-go {
  height: 24px;
  padding: 0 9px;
}
.btn-cancel {
  font-size: 11px;
  height: 24px;
  padding: 0 7px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
}
.pull-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 4px;
}
</style>
