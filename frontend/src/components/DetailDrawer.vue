<script setup>
// The slide-over message-detail drawer.
//
// Fetches GET /api/messages/:id on mount and whenever messageId changes, and
// renders: a top bar that steps ↑/↓ through the messages store's current
// filtered+sorted result set, the message metadata grid, the Pangram score
// card (three fraction bars), a line-numbered extracted-text block (greeting/
// sign-off/signature lines greyed via `ignored_lines`), and a line-numbered
// raw-body block with the lines that survived extraction highlighted.
//
// Raw↔extraction highlighting replicates the prototype's rule exactly: a raw
// line is highlighted when it is non-empty after trim, its trimmed+lowercased
// form is in the set of extracted lines, and it does NOT start with '>'
// (quoted lines never highlight).
//
// Contract: props { messageId: Number }, emits ['close'].
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get } from '../api'
import { useMessagesStore } from '../stores/messages'
import { useFiltersStore } from '../stores/filters'
import { useUiStore } from '../stores/ui'
import { LABEL_COLORS } from '../lib/labels'
import { fmtDate } from '../lib/format'

const props = defineProps({
  messageId: { type: Number, required: true },
})
const emit = defineEmits(['close'])

const route = useRoute()
const router = useRouter()
const messages = useMessagesStore()
const filters = useFiltersStore()
const ui = useUiStore()

// --- detail fetch ---
const detail = ref(null)
const loading = ref(false)
const error = ref(null)

async function load(id) {
  loading.value = true
  error.value = null
  try {
    detail.value = await get(`/messages/${id}`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    detail.value = null
  } finally {
    loading.value = false
  }
}

watch(
  () => props.messageId,
  (id) => {
    if (id != null) load(id)
  },
  { immediate: true },
)

// --- prev / next stepping through the current filtered+sorted result set ---
// The messages store's `items` array IS that set (same filters/sort, paged in).
const curIdx = computed(() =>
  messages.items.findIndex((x) => x.id === props.messageId),
)

const posText = computed(() => {
  const n = messages.total
  // Deep link to a message not in the loaded set: "–/{n}", both arrows off.
  return curIdx.value >= 0 ? `${curIdx.value + 1}/${n} in view` : `–/${n}`
})

const prevDisabled = computed(() => curIdx.value <= 0)
const nextDisabled = computed(() => {
  const idx = curIdx.value
  if (idx < 0) return true
  // Enabled at the last loaded row only when more pages can still be fetched.
  return idx >= messages.items.length - 1 && !messages.hasMore
})

function goTo(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

function drawerPrev() {
  const idx = curIdx.value
  if (idx > 0) goTo(messages.items[idx - 1].id)
}

async function drawerNext() {
  const idx = curIdx.value
  if (idx < 0) return
  if (idx < messages.items.length - 1) {
    goTo(messages.items[idx + 1].id)
  } else if (messages.hasMore) {
    // At the last loaded row but more exist: pull the next page, then step.
    await messages.loadMore()
    if (idx + 1 < messages.items.length) goTo(messages.items[idx + 1].id)
  }
}

// --- metadata ---
const fromName = computed(() => {
  const d = detail.value
  if (!d) return ''
  return d.person?.name || d.from?.display_name || d.from?.address || ''
})
const fromEmail = computed(() => detail.value?.from?.address || '')

const dateFull = computed(() => {
  const iso = detail.value?.date
  if (!iso) return ''
  const dt = new Date(iso)
  return Number.isNaN(dt.getTime()) ? String(iso) : dt.toUTCString()
})

// --- Pangram score card ---
const scored = computed(() => {
  const sc = detail.value?.score
  return sc != null && sc.fraction_ai != null
})
const label = computed(() => detail.value?.score?.label || '')
const labelBg = computed(() => LABEL_COLORS[label.value] || LABEL_COLORS.unscored)

function pctOf(v) {
  return v == null ? '—' : Math.round(v * 100) + '%'
}

const scoreMeta = computed(() => {
  if (!scored.value) return ''
  const sc = detail.value.score
  return `detector v${sc.detector_version} · scored ${fmtDate(sc.scored_at)}`
})

const fracRows = computed(() => {
  if (!scored.value) return []
  const sc = detail.value.score
  return [
    { key: 'AI', v: sc.fraction_ai },
    { key: 'AI-Assisted', v: sc.fraction_ai_assisted },
    { key: 'Human', v: sc.fraction_human },
  ].map((r) => ({
    key: r.key,
    pct: pctOf(r.v),
    w: Math.round((r.v || 0) * 100) + '%',
    color: LABEL_COLORS[r.key],
  }))
})

// Wording for "Not scored (…)" and for a missing extraction elsewhere.
const extStatus = computed(() =>
  detail.value?.extraction ? detail.value.extraction.status : 'no extraction',
)

// --- extracted-text card ---
const extMeta = computed(() => {
  const ex = detail.value?.extraction
  if (!ex) return 'no extraction'
  if (ex.status === 'ok') {
    const k = (ex.ignored_lines || []).length
    return (
      ex.status +
      ' · ' +
      ex.method +
      ' · ' +
      (ex.char_count || 0).toLocaleString() +
      ' chars · ' +
      k +
      ' lines excluded from AI analysis'
    )
  }
  return ex.status
})

const extLines = computed(() => {
  const ex = detail.value?.extraction
  if (!ex || ex.status !== 'ok' || !ex.extracted_text) {
    return [
      {
        num: 1,
        text: '(no extracted text)',
        bg: 'transparent',
        col: '#8a929b',
        op: '1',
        title: '',
      },
    ]
  }
  const ignored = new Set(ex.ignored_lines || [])
  return ex.extracted_text.split('\n').map((t, i) => ({
    num: i + 1,
    text: t || ' ',
    bg: ignored.has(i) ? '#eef0f3' : 'transparent',
    col: ignored.has(i) ? '#8a929b' : 'inherit',
    op: ignored.has(i) ? '0.75' : '1',
    title: ignored.has(i)
      ? 'Excluded from AI analysis (greeting/sign-off/signature)'
      : '',
  }))
})

// --- raw-body card ---
// Set of every extracted line, trimmed + lowercased, empties dropped — the
// membership test the prototype uses to decide which raw lines are highlighted.
const extSet = computed(() => {
  const s = new Set()
  const text = detail.value?.extraction?.extracted_text
  if (text) {
    for (const t of text.split('\n')) {
      const k = t.trim().toLowerCase()
      if (k) s.add(k)
    }
  }
  return s
})

const rawLines = computed(() => {
  const raw = detail.value?.raw_body || ''
  const set = extSet.value
  return raw.split('\n').map((t, i) => ({
    num: i + 1,
    text: t || ' ',
    // Prototype rule: non-empty trimmed line, present in the extracted set, and
    // not a quoted '>' line (startsWith tested on the ORIGINAL, untrimmed line).
    bg:
      t.trim() && set.has(t.trim().toLowerCase()) && !t.startsWith('>')
        ? '#fff3bf'
        : 'transparent',
  }))
})

// --- interactions ---
function filterList() {
  if (detail.value?.list) filters.setFilter('list', detail.value.list)
  emit('close')
}

function onKey(e) {
  // The prototype binds no drawer keys; Escape→close is a sensible addition.
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div>
    <div class="drawer-overlay" @click="emit('close')"></div>
    <div class="drawer-panel">
      <div class="drawer-topbar">
        <button
          class="drawer-nav-btn"
          :disabled="prevDisabled"
          title="Previous message"
          @click="drawerPrev"
        >
          ↑
        </button>
        <button
          class="drawer-nav-btn"
          :disabled="nextDisabled"
          title="Next message"
          @click="drawerNext"
        >
          ↓
        </button>
        <span class="drawer-pos">{{ posText }}</span>
        <span style="flex: 1;"></span>
        <button class="drawer-close-btn" @click="emit('close')">Close ✕</button>
      </div>

      <div class="drawer-body">
        <div v-if="loading && !detail" style="color: #8a929b;">Loading…</div>
        <div v-else-if="error" style="color: #8a929b;">{{ error }}</div>

        <template v-else-if="detail">
          <h2 style="font-size: 15px; margin: 0 0 8px; line-height: 1.35;">
            {{ detail.subject }}
          </h2>

          <div
            style="display: grid; grid-template-columns: 88px 1fr; gap: 3px 12px; font-size: 12px; margin-bottom: 12px;"
          >
            <span class="meta-key">List</span>
            <span
              ><a href="#" @click.prevent="filterList">{{ detail.list }}</a></span
            >

            <template v-if="!ui.anonymous">
              <span class="meta-key">From</span>
              <span
                >{{ fromName }}
                <span
                  style="color: #8a929b; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px;"
                  >&lt;{{ fromEmail }}&gt;</span
                ></span
              >
            </template>

            <span class="meta-key">Date</span>
            <span>{{ dateFull }}</span>

            <span class="meta-key">Message-ID</span>
            <span
              style="font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 10.5px; word-break: break-all;"
              >{{ detail.message_id }}</span
            >
          </div>

          <!-- Pangram score card -->
          <div class="drawer-card">
            <div
              style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;"
            >
              <span style="font-size: 11.5px; font-weight: 700;">Pangram score</span>
              <span
                v-if="scored"
                :style="{
                  display: 'inline-block',
                  padding: '0 7px',
                  borderRadius: '3px',
                  fontSize: '10.5px',
                  fontWeight: 700,
                  color: '#ffffff',
                  background: labelBg,
                }"
                >{{ label }}</span
              >
              <span
                style="font-size: 10.5px; color: #8a929b; font-family: ui-monospace, Menlo, Consolas, monospace;"
                >{{ scoreMeta }}</span
              >
            </div>

            <div
              v-for="fr in fracRows"
              :key="fr.key"
              style="display: flex; align-items: center; gap: 10px; padding: 1px 0;"
            >
              <span style="width: 76px; font-size: 11px; color: #626a72; flex: none;">{{
                fr.key
              }}</span>
              <span
                style="flex: 1; height: 8px; background: #eef0f3; border-radius: 3px; overflow: hidden;"
                ><span
                  :style="{
                    display: 'block',
                    height: '100%',
                    width: fr.w,
                    background: fr.color,
                  }"
                ></span
              ></span>
              <span
                style="width: 36px; text-align: right; font-size: 11px; font-weight: 600; flex: none; font-family: ui-monospace, Menlo, Consolas, monospace;"
                >{{ fr.pct }}</span
              >
            </div>

            <div v-if="!scored" style="font-size: 11.5px; color: #8a929b;">
              Not scored ({{ extStatus }}).
            </div>
          </div>

          <!-- Extracted text card -->
          <div class="drawer-card">
            <div style="font-size: 11.5px; font-weight: 700; margin-bottom: 6px;">
              Extracted text
              <span
                style="color: #8a929b; font-weight: 400; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 10.5px;"
                >· {{ extMeta }}</span
              >
            </div>
            <div class="code-block" style="background: #fbfdff;">
              <div v-for="ln in extLines" :key="ln.num" style="display: flex;">
                <span class="code-gutter">{{ ln.num }}</span>
                <span
                  :title="ln.title"
                  :style="{
                    flex: 1,
                    minWidth: 0,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    background: ln.bg,
                    color: ln.col,
                    opacity: ln.op,
                    borderRadius: '2px',
                  }"
                  >{{ ln.text }}</span
                >
              </div>
            </div>
          </div>

          <!-- Raw body card -->
          <div class="drawer-card" style="margin-bottom: 0;">
            <div style="font-size: 11.5px; font-weight: 700; margin-bottom: 6px;">
              Raw body
              <span style="color: #8a929b; font-weight: 400; font-size: 10.5px;"
                >· extracted lines highlighted</span
              >
            </div>
            <div class="code-block" style="background: #f7f8fa;">
              <div v-for="ln in rawLines" :key="ln.num" style="display: flex;">
                <span class="code-gutter">{{ ln.num }}</span>
                <span
                  :style="{
                    flex: 1,
                    minWidth: 0,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    background: ln.bg,
                    borderRadius: '2px',
                  }"
                  >{{ ln.text }}</span
                >
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.drawer-nav-btn {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid #e2e5e9;
  border-radius: 3px;
  background: #ffffff;
  cursor: pointer;
}
.drawer-nav-btn:disabled {
  cursor: default;
  opacity: 0.4;
}
.drawer-pos {
  font-size: 10.5px;
  color: #8a929b;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
.meta-key {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #8a929b;
  font-weight: 700;
  padding-top: 2px;
}
.drawer-card {
  border: 1px solid #e2e5e9;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.code-block {
  border: 1px solid #e2e5e9;
  border-radius: 4px;
  padding: 8px 10px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  line-height: 1.55;
}
.code-gutter {
  flex: none;
  width: 3ch;
  text-align: right;
  padding-right: 9px;
  margin-right: 9px;
  border-right: 1px solid #e2e5e9;
  color: #b3b9c0;
  user-select: none;
}
</style>
