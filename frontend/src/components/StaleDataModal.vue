<script setup>
// The stale-data modal, opened at start-up when the store holds extractions that
// an older extraction routine produced, and reopened later by the header's alert
// icon.
//
// Four steps, in one modal:
//
//   1. intro    — what the version check found (GET /api/staleness, done by the
//                 shell and passed in as `info`), and the offer to look.
//   2. checking — POST /api/staleness/check re-derives every stored extraction
//                 locally. Nothing is rewritten and nothing is paid for; rows
//                 that come out identical are stamped current server-side.
//   3. results  — the affected messages in a scrolling table with a total, and
//                 the "Run process ($)" button.
//   4. running  — Extract then Check over the affected messages only, as two
//                 stage rows (POST /api/staleness/reextract, then /rescore over
//                 the ids that call reports as needing a score). Both are sent in
//                 batches of at most 1000 ids, the server's per-request cap.
//
// Contract: props { open: Boolean, info: Object|null }, emits ['close', 'refresh'].
// `refresh` asks the shell to re-fetch the staleness report, which is what
// clears (or keeps) the header alert icon.
import { computed, ref, watch, onUnmounted } from 'vue'

import { postJson } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { useUiStore } from '../stores/ui'

const props = defineProps({
  open: { type: Boolean, default: false },
  info: { type: Object, default: null },
})
const emit = defineEmits(['close', 'refresh'])

const ui = useUiStore()

//: The server caps one reextract/rescore request at 1000 message ids.
const CHUNK = 1000

const step = ref('intro') // intro | checking | results | running
const error = ref('')
const rows = ref([]) // the affected messages from /staleness/check
const checkSummary = ref(null) // {checked, unchanged, stamped, differing}
const running = ref(false) // a stage is in flight → Close disabled
const stages = ref([])

const affectedCount = computed(() => rows.value.length)

// What the "($)" refers to: the affected messages that would go to the detector
// after re-extraction — those whose re-derived text is scoreable ('ok') and
// either has no verdict yet or has one reached on text that changed. A message
// whose extracted text moved but whose scored text did not keeps its verdict and
// costs nothing.
const costCount = computed(
  () =>
    rows.value.filter((r) => r.new_status === 'ok' && (!r.scored || r.scored_text_changed)).length,
)

const staleCount = computed(() => props.info?.stale_count ?? 0)
const totalCount = computed(() => props.info?.total ?? 0)
const staleVersions = computed(() =>
  (props.info?.versions || []).filter((v) => v.stale).map((v) => v.version || 'unrecorded'),
)

function senderName(row) {
  if (ui.anonymous) return '—'
  const from = row.from || {}
  return from.display_name || from.address || ''
}
function senderTitle(row) {
  if (ui.anonymous) return ''
  return (row.from || {}).address || ''
}

// What moved, for the table's last column. A changed scored text is the
// consequential one (it invalidates the stored verdict), so it is named first.
function changeLabel(row) {
  const parts = []
  if (row.scored_text_changed) parts.push('scored text')
  if (row.text_changed) parts.push('extracted text')
  if (row.old_status !== row.new_status) parts.push(`${row.old_status} → ${row.new_status}`)
  return parts.join(', ')
}

// --- step 2: the check ------------------------------------------------------

async function runCheck() {
  step.value = 'checking'
  error.value = ''
  try {
    const res = await postJson('/staleness/check')
    rows.value = res?.messages || []
    checkSummary.value = {
      checked: res?.checked ?? 0,
      unchanged: res?.unchanged ?? 0,
      stamped: res?.stamped ?? 0,
      differing: res?.differing ?? 0,
    }
    step.value = 'results'
    // The check stamps every unchanged extraction with the running version, so
    // the shell's report is out of date the moment it returns.
    emit('refresh')
  } catch (err) {
    error.value = err.message || 'the check failed'
    step.value = 'intro'
  }
}

// --- step 4: the run --------------------------------------------------------

const STAGE_DEFS = [
  { key: 'extract', label: 'Extract' },
  { key: 'check', label: 'Check' },
]

function initStages() {
  stages.value = STAGE_DEFS.map((d) => ({ ...d, status: 'pending', detail: '' }))
}
function setStage(key, status, detail) {
  const s = stages.value.find((x) => x.key === key)
  if (!s) return
  s.status = status
  if (detail !== undefined) s.detail = detail
}

function batches(ids) {
  const out = []
  for (let i = 0; i < ids.length; i += CHUNK) out.push(ids.slice(i, i + CHUNK))
  return out
}

async function runProcess() {
  if (running.value || !affectedCount.value) return
  step.value = 'running'
  error.value = ''
  initStages()
  running.value = true
  try {
    // Extract: rewrite the affected extractions, collecting the ids whose
    // extraction now needs a score.
    setStage('extract', 'running')
    let rewritten = 0
    let unchanged = 0
    let invalidated = 0
    const rescoreIds = []
    try {
      for (const batch of batches(rows.value.map((r) => r.id))) {
        const res = await postJson('/staleness/reextract', { ids: batch })
        rewritten += res.rewritten
        unchanged += res.unchanged
        invalidated += res.scores_invalidated
        rescoreIds.push(...(res.rescore_ids || []))
      }
    } catch (err) {
      setStage('extract', 'error', err.message || 'failed')
      return
    }
    setStage(
      'extract',
      'done',
      `re-extracted ${fmtInt(rewritten)} · unchanged ${fmtInt(unchanged)} · ` +
        `scores invalidated ${fmtInt(invalidated)}`,
    )

    // Check: score only those messages.
    if (!rescoreIds.length) {
      setStage('check', 'done', 'nothing to score')
    } else {
      setStage('check', 'running')
      let scored = 0
      let cacheHits = 0
      let apiCalls = 0
      let tooShort = 0
      let skipped = false
      try {
        for (const batch of batches(rescoreIds)) {
          const res = await postJson('/staleness/rescore', { ids: batch })
          if (res.scoring_skipped) {
            skipped = true
            break
          }
          scored += res.scored
          cacheHits += res.cache_hits
          apiCalls += res.api_calls
          tooShort += res.too_short
        }
      } catch (err) {
        setStage('check', 'error', err.message || 'failed')
        return
      }
      if (skipped) {
        setStage('check', 'skipped', 'skipped (no Pangram API key)')
      } else {
        setStage(
          'check',
          'done',
          `scored ${fmtInt(scored)} · cache hits ${fmtInt(cacheHits)} · ` +
            `API calls ${fmtInt(apiCalls)} · too short ${fmtInt(tooShort)}`,
        )
      }
    }
  } finally {
    running.value = false
    emit('refresh')
  }
}

// --- open / close ------------------------------------------------------------

// Every open starts at the intro: the report may have changed since the last
// look, and a table of affected messages goes stale as soon as any of them is
// reprocessed.
watch(
  () => props.open,
  (open) => {
    if (open) {
      step.value = 'intro'
      error.value = ''
      rows.value = []
      checkSummary.value = null
      stages.value = []
      document.addEventListener('keydown', onKeydown)
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
)
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

// Escape is inert while a stage is in flight (a run cannot be cancelled) and
// during the check, which is a single request that cannot be interrupted either.
function requestClose() {
  if (running.value || step.value === 'checking') return
  emit('close')
}
function onKeydown(e) {
  if (e.key === 'Escape') requestClose()
}

const STATUS_ICON = { done: '✓', error: '✗', skipped: '–' }
function statusIcon(status) {
  return STATUS_ICON[status] || ''
}
</script>

<template>
  <Teleport to="body">
    <!-- Backdrop click is inert, as in RunProcessModal: the modal is dismissed
         by its own button (or Escape, when nothing is in flight). -->
    <div v-if="open" class="sd-overlay">
      <div class="sd-modal" role="dialog" aria-modal="true" aria-labelledby="sd-title">
        <div id="sd-title" class="sd-title">Stored text may be out of date</div>

        <!-- 1. intro -->
        <template v-if="step === 'intro'">
          <p class="sd-body">
            The extraction routine has changed since some stored messages were processed.
            {{ fmtInt(staleCount) }} of {{ fmtInt(totalCount) }} extracted
            {{ staleCount === 1 ? 'message was' : 'messages were' }} derived by an earlier version
            (<span class="mono">{{ staleVersions.join(', ') }}</span
            >); the current version is <span class="mono">{{ info?.app_version }}</span
            >.
          </p>
          <p class="sd-body">
            The check below re-runs the current extraction and post-processing over every stored
            message and lists the ones whose text would change. It reads and writes nothing else,
            and sends nothing to the Pangram API.
          </p>
          <div v-if="error" class="sd-error">{{ error }}</div>
          <div class="sd-footer">
            <button type="button" class="sd-btn" @click="requestClose">Not now</button>
            <button type="button" class="sd-btn sd-btn-primary" @click="runCheck">
              Show affected messages
            </button>
          </div>
        </template>

        <!-- 2. checking -->
        <template v-else-if="step === 'checking'">
          <div class="sd-checking">
            <span class="sd-spinner"></span>
            <span>Re-running extraction over the stored messages…</span>
          </div>
        </template>

        <!-- 3. results -->
        <template v-else-if="step === 'results'">
          <div class="sd-counts">
            <span class="sd-count">
              <span class="sd-count-val mono">{{ fmtInt(affectedCount) }}</span>
              affected
            </span>
            <span class="sd-count">
              <span class="sd-count-val mono">{{ fmtInt(checkSummary?.checked) }}</span>
              checked
            </span>
            <span class="sd-count">
              <span class="sd-count-val mono">{{ fmtInt(costCount) }}</span>
              need a new score
            </span>
          </div>

          <template v-if="affectedCount">
            <div class="sd-table" :class="{ 'sd-anon': ui.anonymous }">
              <div class="sd-head">
                <span>List</span>
                <span>Date</span>
                <span v-if="!ui.anonymous">From</span>
                <span>Subject</span>
                <span class="sd-num">Chars</span>
                <span>Change</span>
              </div>
              <div class="sd-rows">
                <div v-for="r in rows" :key="r.id" class="sd-row">
                  <span class="sd-cell mono">{{ r.list }}</span>
                  <span class="sd-cell mono">{{ fmtDate(r.date) }}</span>
                  <span v-if="!ui.anonymous" class="sd-cell" :title="senderTitle(r)">
                    {{ senderName(r) }}
                  </span>
                  <span class="sd-cell">{{ r.subject || '(no subject)' }}</span>
                  <span class="sd-cell sd-num mono">
                    {{ fmtInt(r.old_chars) }} → {{ fmtInt(r.new_chars) }}
                  </span>
                  <span class="sd-cell sd-change" :title="changeLabel(r)">{{ changeLabel(r) }}</span>
                </div>
              </div>
            </div>
            <div class="sd-note">
              Re-processing re-scores up to {{ fmtInt(costCount) }} of these messages — one paid
              Pangram call each, unless the new text is already in the score cache. The other
              {{ fmtInt(affectedCount - costCount) }} keep their current score or are not scoreable.
            </div>
          </template>
          <div v-else class="sd-body">
            No differences found: every stored extraction matches what the current routine produces.
            Nothing needs re-processing.
          </div>

          <div class="sd-footer">
            <button type="button" class="sd-btn" @click="requestClose">Close</button>
            <button
              v-if="affectedCount"
              type="button"
              class="sd-btn sd-btn-primary"
              @click="runProcess"
            >
              Run process ($)
            </button>
          </div>
        </template>

        <!-- 4. running -->
        <template v-else-if="step === 'running'">
          <div class="sd-body">
            Re-processing {{ fmtInt(affectedCount) }}
            {{ affectedCount === 1 ? 'message' : 'messages' }}.
          </div>
          <div class="sd-stages">
            <div
              v-for="s in stages"
              :key="s.key"
              class="sd-stage"
              :class="`sd-stage-${s.status}`"
            >
              <span class="sd-icon" aria-hidden="true">
                <span v-if="s.status === 'running'" class="sd-spinner"></span>
                <span v-else class="sd-glyph">{{ statusIcon(s.status) }}</span>
              </span>
              <span class="sd-label">{{ s.label }}</span>
              <span class="sd-detail mono">{{ s.detail }}</span>
            </div>
          </div>
          <div class="sd-footer">
            <button type="button" class="sd-btn" :disabled="running" @click="requestClose">
              Close
            </button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.mono {
  font-family: var(--mono);
}
.sd-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.sd-modal {
  width: 860px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 14px 16px;
  color: var(--text-secondary);
  font-size: 11.5px;
}
.sd-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
  margin-bottom: 10px;
}
.sd-body {
  margin: 0 0 8px;
  line-height: 1.55;
}
.sd-error {
  margin: 4px 0 0;
  color: var(--danger);
}
.sd-checking {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 0;
}

/* counts line above the table */
.sd-counts {
  display: flex;
  gap: 18px;
  margin-bottom: 8px;
}
.sd-count {
  color: var(--text-muted);
}
.sd-count-val {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
  margin-right: 4px;
}

/* the scrolling affected-messages table */
.sd-table {
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.sd-head,
.sd-row {
  display: grid;
  grid-template-columns: 100px 118px 120px minmax(0, 1fr) 92px 148px;
  gap: 10px;
  padding: 4px 8px;
  align-items: baseline;
}
.sd-anon .sd-head,
.sd-anon .sd-row {
  grid-template-columns: 100px 118px minmax(0, 1fr) 92px 148px;
}
.sd-head {
  background: var(--toolbar);
  border-bottom: 1px solid var(--border);
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  flex: none;
}
.sd-rows {
  overflow-y: auto;
  max-height: 46vh;
}
.sd-row {
  border-bottom: 1px solid var(--border-row);
}
.sd-row:last-child {
  border-bottom: none;
}
.sd-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sd-num {
  text-align: right;
}
.sd-change {
  color: var(--text-muted);
}
.sd-note {
  margin: 8px 0 0;
  font-size: 10px;
  color: var(--text-muted);
}

/* stage rows for the run */
.sd-stages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 10px 0 4px;
}
.sd-stage {
  display: grid;
  grid-template-columns: 16px 58px 1fr;
  gap: 8px;
  align-items: baseline;
}
.sd-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 14px;
}
.sd-glyph {
  font-size: 12px;
  line-height: 1;
}
.sd-label {
  font-weight: 600;
  color: var(--text-name);
}
.sd-detail {
  font-size: 10.5px;
  color: var(--text-muted);
  min-width: 0;
  word-break: break-word;
}
.sd-stage-pending .sd-label,
.sd-stage-pending .sd-glyph {
  color: var(--text-muted);
  opacity: 0.7;
}
.sd-stage-done .sd-glyph {
  color: var(--accent);
}
.sd-stage-error .sd-glyph {
  color: var(--danger);
}
.sd-stage-error .sd-detail {
  color: var(--danger);
}
.sd-stage-skipped .sd-glyph {
  color: var(--text-muted);
}
.sd-spinner {
  display: inline-block;
  width: 11px;
  height: 11px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: sd-spin 0.7s linear infinite;
}
@keyframes sd-spin {
  to {
    transform: rotate(360deg);
  }
}

.sd-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  flex: none;
}
.sd-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
}
.sd-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.sd-btn-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.sd-btn-primary:hover:not(:disabled) {
  background: var(--accent-dark);
  border-color: var(--accent-dark);
  color: #fff;
}
.sd-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
