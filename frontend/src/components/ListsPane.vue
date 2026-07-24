<script setup>
// The left / lower "Lists" pane. Two mutually exclusive modes driven by the
// shared filter state:
//
//   1. Lists index (default)  — every list with a mix bar (GET /api/lists).
//      "+ Add list" and "Regenerate index" live in the pane header, in every
//      mode (POST /api/lists/regenerate).
//   2. List stats (a `list` filter) — per-list aggregates from GET /api/summary
//      (stat tiles, detection-mix summary, last-50-messages rug, Add footer).
//
// "Run process ($)" buttons (the Add-list form and the Add popover) do not
// pull-and-score in one call. They close their own UI and open the
// RunProcessModal, which drives the three pipeline stages sequentially via
// separate endpoints: fetch (POST /api/pull/fetch or /api/pull/range/fetch),
// then extract (POST /api/extract), then check (POST /api/score). The Add
// popover opens from each index row's "Add" button and from the list-stats
// footer's "Add" button; only "Regenerate index" keeps its older single-call
// flow.
//
// Sender (person/address) details live in the Senders pane, not here.
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get, postJson } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { MIX_CAPTION, labelColor } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import MixBar from './MixBar.vue'
import MixSummary from './MixSummary.vue'
import RunProcessModal from './RunProcessModal.vue'

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
const pullMsg = ref('')
const regenerating = ref(false)
const regenMsg = ref('')
const statusMsg = computed(() =>
  regenerating.value ? 'enumerating server folders…' : pullMsg.value || regenMsg.value,
)

// --- staged run modal (fetch → extract → check) -----------------------------
// The "Run process ($)" buttons drive the pipeline in three sequential POSTs,
// surfacing each stage's status in RunProcessModal. runProcess owns the
// reactive stage state; the modal is presentational. The Add-list form and the
// popover differ only in the fetch endpoint/body and detail formatter.
const STAGE_DEFS = [
  { key: 'fetch', label: 'Fetch' },
  { key: 'extract', label: 'Extract' },
  { key: 'check', label: 'Check' },
]
const processRunning = ref(false) // a stage is in flight → guard + disable Close
const processModalOpen = ref(false)
const processTitle = ref('')
const processStages = ref([])
const processModalTitle = computed(() => `Run process — ${processTitle.value}`)

function initStages() {
  processStages.value = STAGE_DEFS.map((d) => ({ ...d, status: 'pending', detail: '' }))
}
function setStage(key, status, detail) {
  const s = processStages.value.find((x) => x.key === key)
  if (!s) return
  s.status = status
  if (detail !== undefined) s.detail = detail
}
function errMsg(err) {
  return err instanceof Error ? err.message : String(err)
}

// Fetch-stage detail lines. Plain: the Add-list form flow; range: the popover
// flow (prepends the matched count, appends the cap note when the API capped).
function fetchDetailPlain(r) {
  return (
    `fetched ${fmtInt(r.fetched)} · duplicates ${fmtInt(r.duplicates)} · ` +
    `parse errors ${fmtInt(r.parse_errors)}`
  )
}
function fetchDetailRange(r) {
  let line =
    `matched ${fmtInt(r.matched)} · fetched ${fmtInt(r.fetched)} · ` +
    `duplicates ${fmtInt(r.duplicates)} · parse errors ${fmtInt(r.parse_errors)}`
  if (r.capped) line += ' · capped at 1,000'
  return line
}

// Run the three stages strictly in order. fetchFn returns the fetch response
// (which echoes the `limit` fed to extract/score); fetchDetailFn formats it. On
// any stage error the run stops, that stage shows the message, later stages stay
// pending, and Close is re-enabled. The pane refreshes whether or not the run
// completed: an error after (or during) the fetch stage may still have
// inserted messages.
async function runProcess(name, fetchFn, fetchDetailFn) {
  if (processRunning.value) return
  processTitle.value = name
  initStages()
  processModalOpen.value = true
  processRunning.value = true
  try {
    // Fetch
    setStage('fetch', 'running')
    let fetchRes
    try {
      fetchRes = await fetchFn()
    } catch (err) {
      setStage('fetch', 'error', errMsg(err))
      return
    }
    setStage('fetch', 'done', fetchDetailFn(fetchRes))

    const limit = fetchRes.limit
    if (limit === 0) {
      // Range matched nothing — nothing to extract or score.
      setStage('extract', 'done', 'nothing to process')
      setStage('check', 'done', 'nothing to process')
    } else {
      // Extract
      setStage('extract', 'running')
      let extractRes
      try {
        extractRes = await postJson('/extract', { limit })
      } catch (err) {
        setStage('extract', 'error', errMsg(err))
        return
      }
      setStage(
        'extract',
        'done',
        `extracted ${fmtInt(extractRes.extracted)} · empty ${fmtInt(extractRes.empty)}`,
      )

      // Check
      setStage('check', 'running')
      let scoreRes
      try {
        scoreRes = await postJson('/score', { limit })
      } catch (err) {
        setStage('check', 'error', errMsg(err))
        return
      }
      if (scoreRes.scoring_skipped) {
        setStage('check', 'skipped', 'skipped (no Pangram API key)')
      } else {
        setStage(
          'check',
          'done',
          `scored ${fmtInt(scoreRes.scored)} · cache hits ${fmtInt(scoreRes.cache_hits)} · ` +
            `API calls ${fmtInt(scoreRes.api_calls)} · too short ${fmtInt(scoreRes.too_short)}`,
        )
      }
    }
  } finally {
    processRunning.value = false
    await loadLists()
    if (mode.value === 'list') await Promise.all([loadSummary(), loadRug()])
  }
}

function closeProcessModal() {
  if (processRunning.value) return
  processModalOpen.value = false
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
  // Close the form and clear the name input, then drive the staged run in the
  // modal (fetch → extract → check).
  pullFormOpen.value = false
  pullName.value = ''
  runProcess(name, () => postJson('/pull/fetch', { list: name, count }), fetchDetailPlain)
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

// --- add-and-check popover ---------------------------------------------------
// One popover open at a time, anchored under the Add button that opened it (an
// index row's or the list-stats footer's). Its two tabs
// preview server-side messages ("new since last fetch" / "before last fetch").
// Each "Run process ($)" button closes the popover and hands the chosen range
// to runProcess, which drives the staged fetch/extract/check run in the modal.
const popoverList = ref(null) // name of the list whose Add popover is open
const popoverTab = ref('new') // 'new' | 'before'
// Inline fixed-position style for the teleported popover (see toggleAddPopover).
const popoverStyle = ref({})
const POPOVER_WIDTH = 400

// Tab 1 — "new since last fetch".
const newPreview = ref(null) // {mode, list, total, shown, more, messages}
const newPreviewLoading = ref(false)
const newPreviewError = ref('')
const newCountInput = ref('all') // "all" or a positive integer (as a string)

// Tab 2 — "before last fetch".
const beforePreview = ref(null)
const beforePreviewLoading = ref(false)
const beforePreviewError = ref('')
const beforeCount = ref(25) // requested preview window (1..1000)
const beforePreviewedCount = ref(0) // the count the last successful preview used
const beforePreviewed = ref(false) // a preview has run → enable "Run process ($)"

function senderName(m) {
  return m.from_name || m.from_email || '(unknown)'
}
// The other half of the sender pair as a tooltip (email when a name is shown).
function senderTitle(m) {
  return m.from_name ? m.from_email || '' : ''
}

function resetPopover() {
  newPreview.value = null
  newPreviewError.value = ''
  newCountInput.value = 'all'
  beforePreview.value = null
  beforePreviewError.value = ''
  beforeCount.value = 25
  beforePreviewedCount.value = 0
  beforePreviewed.value = false
}

function closeAddPopover() {
  popoverList.value = null
}
// Fixed-position style anchored to the clicked Add button. The popover is
// teleported to <body>, so no ancestor overflow clips it; it right-aligns to
// the button, clamps to the viewport, and flips above when room below is tight.
function computePopoverStyle(btn) {
  const rect = btn.getBoundingClientRect()
  const gap = 4
  const margin = 8
  // Horizontal clamping is left to CSS (100vw resolves in the layout engine,
  // which knows the real viewport even in embedded webviews where
  // window.innerWidth reports 0).
  const style = {
    position: 'fixed',
    width: `min(${POPOVER_WIDTH}px, calc(100vw - ${2 * margin}px))`,
    left: `clamp(${margin}px, ${Math.round(rect.right - POPOVER_WIDTH)}px, calc(100vw - ${POPOVER_WIDTH + margin}px))`,
  }
  const viewH = window.innerHeight || document.documentElement.clientHeight
  const spaceBelow = viewH - rect.bottom
  // Open upward (anchored by its bottom edge) only when below is tight and
  // above has more room; without a usable JS viewport height (viewH 0) open
  // downward. The popover grows with its content either way.
  if (viewH && spaceBelow < 260 && rect.top > spaceBelow) {
    style.bottom = `calc(100vh - ${Math.round(rect.top - gap)}px)`
  } else {
    style.top = `${Math.round(rect.bottom + gap)}px`
  }
  return style
}

// Toggle from an "Add" button (index row or list-stats footer); clicking a
// different list's Add moves the popover there.
function toggleAddPopover(name, event) {
  if (popoverList.value === name) {
    closeAddPopover()
    return
  }
  if (event && event.currentTarget) {
    popoverStyle.value = computePopoverStyle(event.currentTarget)
  }
  popoverList.value = name
  popoverTab.value = 'new'
  resetPopover()
  loadNewPreview()
}
function setPopoverTab(tab) {
  popoverTab.value = tab
  // Lazily load tab 1 the first time it is shown (or after an error clears).
  if (tab === 'new' && !newPreview.value && !newPreviewLoading.value && !newPreviewError.value) {
    loadNewPreview()
  }
}

async function loadNewPreview() {
  const name = popoverList.value
  if (!name) return
  newPreviewLoading.value = true
  newPreviewError.value = ''
  try {
    newPreview.value = await postJson('/lists/preview', { list: name, mode: 'new' })
  } catch (err) {
    newPreview.value = null
    newPreviewError.value = err instanceof Error ? err.message : String(err)
  } finally {
    newPreviewLoading.value = false
  }
}

async function runBeforePreview() {
  const name = popoverList.value
  if (!name || beforePreviewLoading.value) return
  let count = parseInt(beforeCount.value, 10)
  if (!Number.isFinite(count)) count = 25
  count = Math.min(1000, Math.max(1, count))
  beforeCount.value = count
  beforePreviewLoading.value = true
  beforePreviewError.value = ''
  try {
    beforePreview.value = await postJson('/lists/preview', { list: name, mode: 'before', count })
    beforePreviewedCount.value = count
    beforePreviewed.value = true
  } catch (err) {
    beforePreview.value = null
    beforePreviewError.value = err instanceof Error ? err.message : String(err)
    beforePreviewed.value = false
  } finally {
    beforePreviewLoading.value = false
  }
}

// Restore the tab-1 fetch input to "all" when it is cleared.
function normaliseNewCount() {
  if (String(newCountInput.value).trim() === '') newCountInput.value = 'all'
}

// A "Run process ($)" button in the popover: compute the range the same way the
// preview tabs do, close the popover, and hand the run to the staged modal.
// 'new': input "all"/empty → null (all new). 'before': the previewed count.
function startPopoverProcess(mode) {
  const name = popoverList.value
  if (!name) return
  let count
  if (mode === 'new') {
    const raw = String(newCountInput.value).trim()
    if (raw === '' || raw === 'all') {
      count = null
    } else {
      count = parseInt(raw, 10)
      if (!Number.isFinite(count) || count < 1) count = null
    }
  } else {
    count = beforePreviewedCount.value
  }
  closeAddPopover()
  runProcess(name, () => postJson('/pull/range/fetch', { list: name, mode, count }), fetchDetailRange)
}

// Close on Escape or a click outside the open popover (the row's own Add button
// handles its own toggle). Capture phase so it runs regardless of @click.stop.
function onPopoverKeydown(e) {
  if (e.key === 'Escape') closeAddPopover()
}
function onPopoverDocClick(e) {
  if (!popoverList.value) return
  const el = e.target
  if (el.closest && (el.closest('.add-popover') || el.closest('.row-add-btn'))) return
  closeAddPopover()
}
// The anchor row can scroll out from under a fixed-position popover, so close
// on any scroll that does not originate inside the popover itself.
function onPopoverScroll(e) {
  if (!popoverList.value) return
  const el = e.target
  if (el && el.nodeType === 1 && el.closest && el.closest('.add-popover')) return
  closeAddPopover()
}
watch(popoverList, (open) => {
  if (open) {
    document.addEventListener('keydown', onPopoverKeydown)
    document.addEventListener('click', onPopoverDocClick, true)
    document.addEventListener('scroll', onPopoverScroll, true)
  } else {
    document.removeEventListener('keydown', onPopoverKeydown)
    document.removeEventListener('click', onPopoverDocClick, true)
    document.removeEventListener('scroll', onPopoverScroll, true)
  }
})
onUnmounted(() => {
  document.removeEventListener('keydown', onPopoverKeydown)
  document.removeEventListener('click', onPopoverDocClick, true)
  document.removeEventListener('scroll', onPopoverScroll, true)
})

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
</script>

<template>
  <div class="card">
    <div class="pane-header">
      <span class="pane-title">Lists</span>
      <span class="pane-subtitle">{{ contextSub }}</span>
      <label
        v-if="mode !== 'list'"
        class="show-all"
        :title="`Show all (${fmtInt(totalListCount)})`"
      >
        <input
          type="checkbox"
          class="show-all-input"
          role="switch"
          :checked="!showActive"
          :aria-checked="!showActive"
          @change="(e) => (showActive = !e.target.checked)"
        />
        <span class="switch" aria-hidden="true"><span class="switch-knob"></span></span>
        <span class="show-all-text">Show All</span>
      </label>
      <span class="header-actions">
        <button v-if="!pullFormOpen" class="io-btn" @click="openPullForm">
          Add list
        </button>
        <button class="io-btn" :disabled="regenerating" @click="regenerate">
          Rebuild index
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
          <button class="btn-primary btn-go" :disabled="processRunning" @click="submitPull">
            Run process ($)
          </button>
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
            <button
              type="button"
              class="io-btn row-add-btn"
              @click.stop="toggleAddPopover(filters.list, $event)"
            >
              Add
            </button>
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
          <span></span>
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
          <button
            type="button"
            class="io-btn row-add-btn"
            @click.stop="toggleAddPopover(l.name, $event)"
          >
            Add
          </button>
        </div>
      </template>
    </div>

    <!-- add-and-check popover, anchored to whichever Add button opened it (an
         index row's or the list-stats footer's); teleported to <body> so no
         ancestor overflow (scrolling pane body, clipped card) can hide it -->
    <Teleport to="body">
      <div v-if="popoverList" class="add-popover" :style="popoverStyle" @click.stop>
        <div class="pop-head">
          <div class="pop-tabs" role="tablist">
            <button
              type="button"
              class="pop-tab"
              :class="{ 'pop-tab-on': popoverTab === 'new' }"
              :aria-selected="popoverTab === 'new'"
              @click="setPopoverTab('new')"
            >
              New since last fetch
            </button>
            <button
              type="button"
              class="pop-tab"
              :class="{ 'pop-tab-on': popoverTab === 'before' }"
              :aria-selected="popoverTab === 'before'"
              @click="setPopoverTab('before')"
            >
              Before last fetch
            </button>
          </div>
          <button type="button" class="pop-close" title="Close" @click="closeAddPopover">✕</button>
        </div>

        <!-- Tab 1 — new since last fetch -->
        <div v-if="popoverTab === 'new'" class="pop-view">
          <div v-if="newPreviewLoading" class="pop-status">checking server…</div>
          <div v-else-if="newPreviewError" class="pop-status pop-error">{{ newPreviewError }}</div>
          <template v-else-if="newPreview">
            <div v-if="newPreview.total === 0" class="pop-status">
              No new messages since the last fetch.
            </div>
            <template v-else>
              <div class="pop-list">
                <div v-for="(m, i) in newPreview.messages" :key="i" class="pop-msg">
                  <span class="pop-from" :title="senderTitle(m)">{{ senderName(m) }}</span>
                  <span class="pop-subj">{{ m.subject || '(no subject)' }}</span>
                  <span class="pop-date mono">{{ fmtDate(m.date) }}</span>
                </div>
              </div>
              <div v-if="newPreview.more > 0" class="pop-more">
                + {{ fmtInt(newPreview.more) }} more not shown
              </div>
            </template>
          </template>
          <div class="pop-fetch-row">
            <label class="pop-label">
              Messages to fetch:
              <input
                type="text"
                class="pop-input"
                :value="newCountInput"
                @input="(e) => (newCountInput = e.target.value)"
                @change="normaliseNewCount"
                @blur="normaliseNewCount"
              />
            </label>
            <button type="button" class="io-btn" @click="startPopoverProcess('new')">
              Run process ($)
            </button>
          </div>
        </div>

        <!-- Tab 2 — before last fetch -->
        <div v-else class="pop-view">
          <div class="pop-fetch-row">
            <label class="pop-label">
              Messages to preview:
              <input
                type="number"
                min="1"
                max="1000"
                class="pop-input pop-input-num"
                :value="beforeCount"
                @input="(e) => (beforeCount = e.target.value)"
              />
            </label>
            <button
              type="button"
              class="io-btn"
              :disabled="beforePreviewLoading"
              @click="runBeforePreview"
            >
              Preview
            </button>
          </div>
          <div v-if="beforePreviewLoading" class="pop-status">checking server…</div>
          <div v-else-if="beforePreviewError" class="pop-status pop-error">
            {{ beforePreviewError }}
          </div>
          <template v-else-if="beforePreview">
            <div v-if="beforePreview.total === 0" class="pop-status">No messages found.</div>
            <template v-else>
              <div class="pop-list">
                <div v-for="(m, i) in beforePreview.messages" :key="i" class="pop-msg">
                  <span class="pop-from" :title="senderTitle(m)">{{ senderName(m) }}</span>
                  <span class="pop-subj">{{ m.subject || '(no subject)' }}</span>
                  <span class="pop-date mono">{{ fmtDate(m.date) }}</span>
                </div>
              </div>
              <div v-if="beforePreview.more > 0" class="pop-more">
                + {{ fmtInt(beforePreview.more) }} more not shown
              </div>
            </template>
          </template>
          <div class="pop-fetch-row">
            <button
              type="button"
              class="io-btn"
              :disabled="!beforePreviewed"
              @click="startPopoverProcess('before')"
            >
              Run process ($)
            </button>
          </div>
        </div>

        <div class="pop-footer">
          <div class="pop-note">Scoring sends extracted text to the paid Pangram API.</div>
        </div>
      </div>
    </Teleport>

    <!-- staged run modal, teleported to <body> (inside the component) -->
    <RunProcessModal
      :open="processModalOpen"
      :title="processModalTitle"
      :stages="processStages"
      :running="processRunning"
      @close="closeProcessModal"
    />
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
  grid-template-columns: 1fr 44px 150px 88px 34px;
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
  grid-template-columns: 1fr 44px 150px 88px 34px;
  gap: 6px;
  align-items: center;
  border-bottom: 1px solid var(--border-row);
  cursor: pointer;
  padding: 3px 0;
  font-size: 11.5px;
  position: relative; /* anchors the per-row add-and-check popover */
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

/* --- "Show All" toggle switch --- */
.show-all {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  cursor: pointer;
  user-select: none;
}
.show-all-text {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  white-space: nowrap;
}
/* Native checkbox drives state/focus but is visually replaced by the switch. */
.show-all-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  margin: 0;
}
.switch {
  position: relative;
  display: inline-block;
  width: 26px;
  height: 14px;
  border-radius: 7px;
  background: var(--border);
  transition: background 0.12s ease;
  flex: none;
}
.switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 1px 1px rgba(0, 0, 0, 0.25);
  transition: transform 0.12s ease;
}
.show-all-input:checked + .switch {
  background: var(--accent);
}
.show-all-input:checked + .switch .switch-knob {
  transform: translateX(12px);
}
.show-all-input:focus-visible + .switch {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

/* --- lightweight text buttons (matches MessagesPane .io-btn) --- */
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
.row-add-btn {
  justify-self: end;
}

/* --- add-and-check popover --- */
.add-popover {
  /* position / top / bottom / left / width are set inline (see computePopoverStyle) */
  position: fixed;
  z-index: 200;
  width: 400px;
  max-width: 92vw;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 10px;
  cursor: default;
  font-size: 11px;
  color: var(--text-secondary);
}
.pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.pop-tabs {
  display: inline-flex;
  gap: 6px;
}
.pop-tab {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 1px 3px;
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
}
.pop-tab-on {
  color: var(--text-name);
  border-bottom-color: var(--accent);
}
.pop-close {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 0 2px;
}
.pop-list {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--border-row);
  border-radius: 4px;
  margin-bottom: 6px;
}
.pop-msg {
  display: grid;
  grid-template-columns: 1fr 1.4fr auto;
  gap: 6px;
  align-items: baseline;
  padding: 3px 6px;
  border-bottom: 1px solid var(--border-row);
}
.pop-msg:last-child {
  border-bottom: none;
}
.pop-from {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-name);
}
.pop-subj {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.pop-date {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}
.pop-more {
  font-size: 10px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.pop-status {
  font-size: 10.5px;
  color: var(--text-muted);
  padding: 3px 0;
}
.pop-error {
  color: var(--danger);
}
.pop-fetch-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0;
}
.pop-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-secondary);
}
.pop-input {
  font-size: 11px;
  height: 20px;
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 60px;
}
.pop-input-num {
  width: 60px;
}
.pop-footer {
  margin-top: 6px;
}
.pop-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 4px;
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
