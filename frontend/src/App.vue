<script setup>
// App shell: the 40px header bar + the single-screen dashboard below it. The
// header holds the brand, an ⓘ button that opens the documentation drawer, an
// alert button (only while a warning is active), an unfiltered stat line (total
// messages · lists · db size), the Pangram-v3 switch and the global Anonymous
// toggle. The dashboard itself is the routed view.
import { ref, computed, onMounted } from 'vue'
import { RouterView } from 'vue-router'

import { get } from './api'
import { fmtInt } from './lib/format'
import { useUiStore } from './stores/ui'
import { useFiltersStore } from './stores/filters'
import { useSettingsStore } from './stores/settings'
import DocsDrawer from './components/DocsDrawer.vue'
import StaleDataModal from './components/StaleDataModal.vue'
import PangramUpgradeModal from './components/PangramUpgradeModal.vue'

const ui = useUiStore()
const filters = useFiltersStore()
const settings = useSettingsStore()

// Unfiltered header stat, fetched once. `total`/`db_size_bytes` come from the
// summary (with no filters); `nlists` from /api/lists.
const totalMsgs = ref(null)
const nLists = ref(null)
const dbBytes = ref(null)

const headerStat = computed(() => {
  const parts = []
  if (totalMsgs.value != null) parts.push(`${fmtInt(totalMsgs.value)} msgs`)
  if (nLists.value != null) parts.push(`${fmtInt(nLists.value)} lists`)
  // db_size_bytes may be absent until the backend lands — omit gracefully.
  if (dbBytes.value != null) {
    const mb = (dbBytes.value / (1024 * 1024)).toFixed(1)
    parts.push(`db ${mb} MB`)
  }
  return parts.join(' · ')
})

// Stored text may predate the current extraction routine (GET /api/staleness —
// an extraction-generation comparison, no text re-derived). The report is fetched once at
// start-up and re-fetched after the modal acts, since both the check and a
// re-processing run change it. While it says stale, the header shows the alert
// button; the modal itself is opened once, unprompted, on the first such load.
const staleness = ref(null)
const staleOpen = ref(false)
const isStale = computed(() => !!staleness.value?.stale)

async function loadStaleness() {
  try {
    staleness.value = await get('/staleness')
  } catch {
    // Never block the dashboard on this check; without a report there is no
    // prompt and no alert icon.
    staleness.value = null
  }
}

// The Pangram upgrade notice (GET /api/pangram/notice): whether the user has
// answered the change of default detector, and what re-testing the stored
// Pangram 3 verdicts would involve. It counts as a warning while it is
// unanswered ("pending") or deferred ("later").
const notice = ref(null)
const pangramOpen = ref(false)
const noticeActive = computed(
  () => notice.value?.state === 'pending' || notice.value?.state === 'later',
)

async function loadNotice() {
  try {
    notice.value = await get('/pangram/notice')
  } catch {
    // As with the staleness report: no report, no prompt and no alert icon.
    notice.value = null
  }
}

// --- the header's alert icon -------------------------------------------------
//
// One button stands for every active warning. Clicking it opens one warning's
// modal, and successive clicks cycle through them, so a second warning is never
// unreachable and two modals are never open at once.
const WARNING_LABELS = {
  stale: 'Stored text may be out of date',
  pangram: 'The default Pangram detector changed to Pangram 4',
}

const warnings = computed(() => {
  const keys = []
  if (isStale.value) keys.push('stale')
  if (noticeActive.value) keys.push('pangram')
  return keys
})
const warningLabel = computed(() => warnings.value.map((k) => WARNING_LABELS[k]).join('; '))

// Index of the warning the next click opens. It is taken modulo the current
// list, so a warning that resolves cannot leave the cycle pointing past the end.
const warningIndex = ref(0)

function openWarning(key) {
  const list = warnings.value
  const i = list.indexOf(key)
  if (i < 0) return
  warningIndex.value = (i + 1) % list.length
  if (key === 'stale') staleOpen.value = true
  else pangramOpen.value = true
}

function openNextWarning() {
  const list = warnings.value
  if (!list.length) return
  openWarning(list[warningIndex.value % list.length])
}

onMounted(async () => {
  try {
    const [summary, lists] = await Promise.all([get('/summary'), get('/lists')])
    totalMsgs.value = summary?.total ?? null
    dbBytes.value = summary?.db_size_bytes ?? null
    nLists.value = (lists?.lists || []).length
  } catch {
    // The stat line is decorative; leave it blank if the fetch fails.
  }
  await Promise.all([loadStaleness(), loadNotice(), settings.load()])
  // At most one modal opens unprompted. The stale prompt takes precedence; an
  // unanswered Pangram notice then stays reachable through the alert icon.
  if (isStale.value) openWarning('stale')
  else if (notice.value?.state === 'pending') openWarning('pangram')
})

// The header switch chooses the detector new scoring uses. A failed write rolls
// the store back, so the switch returns to what the server last reported.
async function onTogglePangramV3(event) {
  try {
    await settings.setUsePangramV3(event.target.checked)
  } catch {
    // Leave the switch reflecting the store; nothing else depends on it here.
  }
}

// Turning anonymous mode on hides the sender-identifying UI, so any active
// person or address filter must be cleared. Clear through the store actions so
// the URL sync stays consistent.
function onToggleAnonymous(event) {
  ui.setAnonymous(event.target.checked)
  if (ui.anonymous) {
    filters.setFilter('person', '')
    filters.setFilter('address', '')
  }
}

// The documentation drawer (README / CHANGELOG / docs), local to the shell.
const docsOpen = ref(false)
</script>

<template>
  <header class="app-header">
    <span class="brand">Mail AI Check</span>
    <button
      type="button"
      class="info-btn"
      title="Documentation"
      aria-label="Documentation"
      @click="docsOpen = true"
    >
      i
    </button>
    <button
      v-if="warnings.length"
      type="button"
      class="alert-btn"
      :title="warningLabel"
      :aria-label="warningLabel"
      @click="openNextWarning"
    >
      !
    </button>
    <span class="header-stat">{{ headerStat }}</span>
    <span class="header-spacer"></span>
    <label class="hdr-toggle">
      Use Pangram v3 (old)
      <input
        type="checkbox"
        class="hdr-checkbox"
        :checked="settings.usingPangramV3"
        @change="onTogglePangramV3"
      />
    </label>
    <label class="anon-toggle">
      Anonymous
      <input
        type="checkbox"
        class="anon-checkbox"
        :checked="ui.anonymous"
        @change="onToggleAnonymous"
      />
    </label>
  </header>

  <RouterView />

  <DocsDrawer :open="docsOpen" @close="docsOpen = false" />

  <StaleDataModal
    :open="staleOpen"
    :info="staleness"
    @close="staleOpen = false"
    @refresh="loadStaleness"
  />

  <PangramUpgradeModal
    :open="pangramOpen"
    :notice="notice"
    @close="pangramOpen = false"
    @refresh="loadNotice"
  />
</template>
