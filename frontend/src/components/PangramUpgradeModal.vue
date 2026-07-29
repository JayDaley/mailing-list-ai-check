<script setup>
// The Pangram upgrade notice, shown once at start-up while GET /api/pangram/notice
// reports state "pending", and reopened later from the header's alert icon.
//
// The app's default detector changed from Pangram 3 to Pangram 4. Pangram 4
// costs more per message, and stored verdicts reached with Pangram 3 are not
// comparable with new ones, so the notice offers two independent choices:
//
//   1. keep Pangram 3 for new scoring (PUT /api/settings, pangram_model
//      "default"), which also turns the header switch on; and
//   2. re-test the existing Pangram 3 verdicts now (POST /api/pangram/retest
//      over the reported message ids, in batches of at most 1000 — the server's
//      per-request cap, as for the staleness endpoints).
//
// Whichever buttons are used, the notice ends up resolved: Apply and "No thanks"
// PUT state "dismissed", "I'll decide later" (and Escape, and a backdrop click)
// PUT "later", which keeps the warning on the header icon without prompting
// again unasked.
//
// Contract: props { open: Boolean, notice: Object|null }, emits ['close', 'refresh'].
// `refresh` asks the shell to re-fetch the notice, which is what clears (or
// keeps) the header alert icon.
import { computed, ref, watch, onUnmounted } from 'vue'

import { postJson, putJson } from '../api'
import { fmtInt } from '../lib/format'
import { useSettingsStore } from '../stores/settings'

const props = defineProps({
  open: { type: Boolean, default: false },
  notice: { type: Object, default: null },
})
const emit = defineEmits(['close', 'refresh'])

const settings = useSettingsStore()

//: The server caps one retest request at 1000 message ids.
const CHUNK = 1000

// Pangram's realtime list prices, in US dollars per word.
const V4_PER_WORD = 0.05 / 100
const V3_PER_WORD = 0.05 / 1000

const step = ref('intro') // intro | running | done
const running = ref(false) // an action is in flight → the buttons are disabled
const stages = ref([])

// The two choices, applied together by the Apply button.
const keepV3 = ref(false)
const retest = ref(false)

const oldScores = computed(() => props.notice?.old_scores ?? 0)
const messageIds = computed(() => props.notice?.message_ids || [])
const estimatedWords = computed(() => props.notice?.estimated_words ?? 0)

// Re-testing with Pangram 3 kept costs a twentieth of the Pangram 4 estimate, so
// ticking "keep using Pangram v3" restates the figure the re-test would actually
// incur. The Pangram 4 figure is the server's; the Pangram 3 one is derived from
// the same word count.
const estimatedCost = computed(() =>
  keepV3.value ? estimatedWords.value * V3_PER_WORD : (props.notice?.estimated_cost_v4 ?? 0),
)
const costModel = computed(() => (keepV3.value ? 'Pangram 3' : 'Pangram 4'))

function fmtUsd(n) {
  const v = Number(n)
  if (!Number.isFinite(v)) return '0.00'
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const canApply = computed(() => (keepV3.value || retest.value) && !running.value)

// --- stage rows (the same shape StaleDataModal reports a run with) -----------

function initStages() {
  const defs = []
  if (keepV3.value) defs.push({ key: 'model', label: 'Setting' })
  if (retest.value) defs.push({ key: 'retest', label: 'Re-test' })
  stages.value = defs.map((d) => ({ ...d, status: 'pending', detail: '' }))
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

// --- the notice state --------------------------------------------------------

// Record the answer and close. A failed write is not worth blocking the user
// over: the notice simply stays as it was and is offered again.
async function resolve(state) {
  try {
    await putJson('/pangram/notice', { state })
  } catch {
    // ignored, deliberately
  }
  emit('refresh')
  emit('close')
}

function later() {
  if (running.value) return
  resolve('later')
}
function noThanks() {
  if (running.value) return
  resolve('dismissed')
}

// --- Apply -------------------------------------------------------------------

async function apply() {
  if (!canApply.value) return
  const didRetest = retest.value
  step.value = 'running'
  initStages()
  running.value = true
  try {
    // The setting goes first, so a re-test that follows is scored by the model
    // the user has just chosen to keep.
    if (keepV3.value) {
      setStage('model', 'running')
      try {
        await settings.setPangramModel('default')
      } catch (err) {
        setStage('model', 'error', err.message || 'failed')
        return
      }
      setStage('model', 'done', 'new scoring uses Pangram v3')
    }

    if (didRetest) {
      const ids = messageIds.value
      if (!ids.length) {
        setStage('retest', 'done', 'nothing to re-test')
      } else {
        setStage('retest', 'running', `0 of ${fmtInt(ids.length)} messages`)
        let invalidated = 0
        let scored = 0
        let cacheHits = 0
        let apiCalls = 0
        let tooShort = 0
        let skipped = false
        let sent = 0
        try {
          for (const batch of batches(ids)) {
            const res = await postJson('/pangram/retest', { ids: batch })
            if (res.scoring_skipped) {
              skipped = true
              break
            }
            invalidated += res.invalidated
            scored += res.scored
            cacheHits += res.cache_hits
            apiCalls += res.api_calls
            tooShort += res.too_short
            sent += batch.length
            setStage('retest', 'running', `${fmtInt(sent)} of ${fmtInt(ids.length)} messages`)
          }
        } catch (err) {
          setStage('retest', 'error', err.message || 'failed')
          return
        }
        if (skipped) {
          setStage('retest', 'skipped', 'skipped (no Pangram API key)')
        } else {
          setStage(
            'retest',
            'done',
            `invalidated ${fmtInt(invalidated)} · scored ${fmtInt(scored)} · ` +
              `cache hits ${fmtInt(cacheHits)} · API calls ${fmtInt(apiCalls)} · ` +
              `too short ${fmtInt(tooShort)}`,
          )
        }
      }
    }

    // The choice has been made either way, so the notice is resolved. A re-test
    // has totals worth reading, so the modal stays open on its report; with only
    // the setting changed there is nothing to wait for and it closes.
    try {
      await putJson('/pangram/notice', { state: 'dismissed' })
    } catch {
      // ignored, deliberately
    }
    emit('refresh')
    if (didRetest) {
      step.value = 'done'
    } else {
      emit('close')
    }
  } finally {
    running.value = false
  }
}

// --- open / close ------------------------------------------------------------

// Every open starts at the intro: the counts may have moved since the last look.
watch(
  () => props.open,
  (open) => {
    if (open) {
      step.value = 'intro'
      keepV3.value = false
      retest.value = false
      stages.value = []
      running.value = false
      document.addEventListener('keydown', onKeydown)
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
)
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

// Escape and a backdrop click mean "I'll decide later", except while an action
// is in flight (it cannot be cancelled) and once the run has reported, where the
// notice is already resolved and the modal only needs closing.
function requestClose() {
  if (running.value) return
  if (step.value === 'done') {
    emit('close')
    return
  }
  resolve('later')
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
    <div v-if="open" class="pu-overlay" @click.self="requestClose">
      <div class="pu-modal" role="dialog" aria-modal="true" aria-labelledby="pu-title">
        <div id="pu-title" class="pu-title">The default Pangram detector is now Pangram 4</div>

        <!-- 1. intro -->
        <template v-if="step === 'intro'">
          <p class="pu-body">
            The detector used for new scoring has changed from Pangram 3 to Pangram 4. Realtime
            scoring with Pangram 4 costs $0.05 per 100 words, against $0.05 per 1,000 words with
            Pangram 3 — between 2 and 10 times more per message, depending on its length.
          </p>
          <p class="pu-body">
            {{ fmtInt(oldScores) }} stored
            {{ oldScores === 1 ? 'message holds a verdict' : 'messages hold verdicts' }}
            reached with Pangram 3. Those verdicts are kept as they are unless they are re-tested.
          </p>

          <div class="pu-choices">
            <label class="pu-choice">
              <input v-model="keepV3" type="checkbox" class="pu-checkbox" />
              <span>Keep using Pangram v3 for new scoring</span>
            </label>
            <label class="pu-choice">
              <input v-model="retest" type="checkbox" class="pu-checkbox" />
              <span>
                Re-test the {{ fmtInt(oldScores) }} existing Pangram 3
                {{ oldScores === 1 ? 'verdict' : 'verdicts' }} now (estimated
                <span class="mono">${{ fmtUsd(estimatedCost) }}</span> with {{ costModel }})
              </span>
            </label>
          </div>

          <div class="pu-note">
            The estimate covers {{ fmtInt(estimatedWords) }} words at list price. Text already in the
            score cache costs nothing.
          </div>

          <div class="pu-footer">
            <button type="button" class="pu-btn" @click="later">I'll decide later</button>
            <button type="button" class="pu-btn" @click="noThanks">No thanks</button>
            <button type="button" class="pu-btn pu-btn-primary" :disabled="!canApply" @click="apply">
              Apply
            </button>
          </div>
        </template>

        <!-- 2. running / 3. done — the same stage rows, read while in flight
             and after the totals land -->
        <template v-else>
          <div class="pu-body">
            <template v-if="retest">
              Re-testing {{ fmtInt(messageIds.length) }}
              {{ messageIds.length === 1 ? 'message' : 'messages' }}.
            </template>
            <template v-else>Applying the chosen setting.</template>
          </div>
          <div class="pu-stages">
            <div v-for="s in stages" :key="s.key" class="pu-stage" :class="`pu-stage-${s.status}`">
              <span class="pu-icon" aria-hidden="true">
                <span v-if="s.status === 'running'" class="pu-spinner"></span>
                <span v-else class="pu-glyph">{{ statusIcon(s.status) }}</span>
              </span>
              <span class="pu-label">{{ s.label }}</span>
              <span class="pu-detail mono">{{ s.detail }}</span>
            </div>
          </div>
          <div class="pu-footer">
            <button type="button" class="pu-btn" :disabled="running" @click="requestClose">
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
.pu-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.pu-modal {
  width: 620px;
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
.pu-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
  margin-bottom: 10px;
}
.pu-body {
  margin: 0 0 8px;
  line-height: 1.55;
}
/* the two independent choices */
.pu-choices {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 10px 0 2px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--toolbar);
}
.pu-choice {
  display: grid;
  grid-template-columns: 14px 1fr;
  gap: 8px;
  align-items: start;
  cursor: pointer;
  line-height: 1.5;
}
.pu-checkbox {
  margin: 2px 0 0;
  accent-color: var(--accent);
}
.pu-note {
  margin: 8px 0 0;
  font-size: 10px;
  color: var(--text-muted);
}

/* stage rows for the run */
.pu-stages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 10px 0 4px;
}
.pu-stage {
  display: grid;
  grid-template-columns: 16px 58px 1fr;
  gap: 8px;
  align-items: baseline;
}
.pu-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 14px;
}
.pu-glyph {
  font-size: 12px;
  line-height: 1;
}
.pu-label {
  font-weight: 600;
  color: var(--text-name);
}
.pu-detail {
  font-size: 10.5px;
  color: var(--text-muted);
  min-width: 0;
  word-break: break-word;
}
.pu-stage-pending .pu-label,
.pu-stage-pending .pu-glyph {
  color: var(--text-muted);
  opacity: 0.7;
}
.pu-stage-done .pu-glyph {
  color: var(--accent);
}
.pu-stage-error .pu-glyph {
  color: var(--danger);
}
.pu-stage-error .pu-detail {
  color: var(--danger);
}
.pu-stage-skipped .pu-glyph {
  color: var(--text-muted);
}
.pu-spinner {
  display: inline-block;
  width: 11px;
  height: 11px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: pu-spin 0.7s linear infinite;
}
@keyframes pu-spin {
  to {
    transform: rotate(360deg);
  }
}

.pu-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  flex: none;
}
.pu-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
}
.pu-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.pu-btn-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.pu-btn-primary:hover:not(:disabled) {
  background: var(--accent-dark);
  border-color: var(--accent-dark);
  color: #fff;
}
.pu-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
