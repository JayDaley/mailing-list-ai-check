<script setup>
// App shell: the 40px header bar + the single-screen dashboard below it. The
// header holds the brand, an ⓘ button that opens the documentation drawer, an
// alert button (only while stored text may be out of date), an unfiltered stat
// line (total messages · lists · db size) and the global Anonymous toggle. The
// dashboard itself is the routed view.
import { ref, computed, onMounted } from 'vue'
import { RouterView } from 'vue-router'

import { get } from './api'
import { fmtInt } from './lib/format'
import { useUiStore } from './stores/ui'
import { useFiltersStore } from './stores/filters'
import DocsDrawer from './components/DocsDrawer.vue'
import StaleDataModal from './components/StaleDataModal.vue'

const ui = useUiStore()
const filters = useFiltersStore()

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

onMounted(async () => {
  try {
    const [summary, lists] = await Promise.all([get('/summary'), get('/lists')])
    totalMsgs.value = summary?.total ?? null
    dbBytes.value = summary?.db_size_bytes ?? null
    nLists.value = (lists?.lists || []).length
  } catch {
    // The stat line is decorative; leave it blank if the fetch fails.
  }
  await loadStaleness()
  if (isStale.value) staleOpen.value = true
})

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
      v-if="isStale"
      type="button"
      class="alert-btn"
      title="Stored text may be out of date"
      aria-label="Stored text may be out of date"
      @click="staleOpen = true"
    >
      !
    </button>
    <span class="header-stat">{{ headerStat }}</span>
    <span class="header-spacer"></span>
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
</template>
