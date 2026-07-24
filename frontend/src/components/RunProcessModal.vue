<script setup>
// Presentational modal for a "Run process ($)" pipeline run. The parent
// (ListsPane) owns the async stage-runner and the reactive stages state; this
// component only renders the three stages and their statuses and emits `close`.
//
// Props:
//   - open   {boolean}  render the overlay + modal when true
//   - title  {string}   heading, e.g. "Run process — <list name>"
//   - stages {Array}    [{key, label, status, detail}] where status is one of
//                       'pending' | 'running' | 'done' | 'error' | 'skipped'
//   - running {boolean} a stage is still in flight → Close disabled, Escape inert
// Emits:
//   - close             the Close button, or Escape once the run has finished
import { watch, onUnmounted } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'Run process' },
  stages: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

function requestClose() {
  if (props.running) return
  emit('close')
}

// Escape closes only once the run has finished (it cannot be cancelled).
function onKeydown(e) {
  if (e.key === 'Escape') requestClose()
}
watch(
  () => props.open,
  (open) => {
    if (open) document.addEventListener('keydown', onKeydown)
    else document.removeEventListener('keydown', onKeydown)
  },
)
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

const STATUS_ICON = { done: '✓', error: '✗', skipped: '–' }
function statusIcon(status) {
  return STATUS_ICON[status] || ''
}
</script>

<template>
  <Teleport to="body">
    <!-- Backdrop click is intentionally inert: the run cannot be dismissed by
         clicking outside; only the Close button (or Escape, once finished). -->
    <div v-if="open" class="rp-overlay">
      <div class="rp-modal" role="dialog" aria-modal="true">
        <div class="rp-title">{{ title }}</div>
        <div class="rp-stages">
          <div
            v-for="s in stages"
            :key="s.key"
            class="rp-stage"
            :class="`rp-stage-${s.status}`"
          >
            <span class="rp-icon" aria-hidden="true">
              <span v-if="s.status === 'running'" class="rp-spinner"></span>
              <span v-else class="rp-glyph">{{ statusIcon(s.status) }}</span>
            </span>
            <span class="rp-label">{{ s.label }}</span>
            <span class="rp-detail mono">{{ s.detail }}</span>
          </div>
        </div>
        <div class="rp-note">Scoring sends extracted text to the paid Pangram API.</div>
        <div class="rp-footer">
          <button
            type="button"
            class="rp-close-btn"
            :disabled="running"
            @click="requestClose"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.mono {
  font-family: var(--mono);
}
.rp-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.rp-modal {
  width: 420px;
  max-width: calc(100vw - 32px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 14px 16px;
  color: var(--text-secondary);
  font-size: 11.5px;
}
.rp-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
  margin-bottom: 10px;
}
.rp-stages {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rp-stage {
  display: grid;
  grid-template-columns: 16px 58px 1fr;
  gap: 8px;
  align-items: baseline;
}
.rp-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 14px;
}
.rp-glyph {
  font-size: 12px;
  line-height: 1;
}
.rp-label {
  font-weight: 600;
  color: var(--text-name);
}
.rp-detail {
  font-size: 10.5px;
  color: var(--text-muted);
  min-width: 0;
  word-break: break-word;
}
/* status colours */
.rp-stage-pending .rp-label,
.rp-stage-pending .rp-glyph {
  color: var(--text-muted);
  opacity: 0.7;
}
.rp-stage-done .rp-glyph {
  color: var(--accent);
}
.rp-stage-error .rp-glyph {
  color: var(--danger);
}
.rp-stage-error .rp-detail {
  color: var(--danger);
}
.rp-stage-skipped .rp-glyph {
  color: var(--text-muted);
}
/* spinner */
.rp-spinner {
  display: inline-block;
  width: 11px;
  height: 11px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: rp-spin 0.7s linear infinite;
}
@keyframes rp-spin {
  to {
    transform: rotate(360deg);
  }
}
.rp-note {
  font-size: 10px;
  color: var(--text-muted);
  margin: 12px 0 10px;
}
.rp-footer {
  display: flex;
  justify-content: flex-end;
}
.rp-close-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
}
.rp-close-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
