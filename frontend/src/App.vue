<script setup>
// App shell: the 40px header bar + the single-screen dashboard below it. The
// header holds the brand, an unfiltered stat line (total messages · lists · db
// size) and the global Anonymous toggle. The dashboard itself is the routed view.
import { ref, computed, onMounted } from 'vue'
import { RouterView } from 'vue-router'

import { get } from './api'
import { fmtInt } from './lib/format'
import { useUiStore } from './stores/ui'
import { useFiltersStore } from './stores/filters'

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

onMounted(async () => {
  try {
    const [summary, lists] = await Promise.all([get('/summary'), get('/lists')])
    totalMsgs.value = summary?.total ?? null
    dbBytes.value = summary?.db_size_bytes ?? null
    nLists.value = (lists?.lists || []).length
  } catch {
    // The stat line is decorative; leave it blank if the fetch fails.
  }
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
</script>

<template>
  <header class="app-header">
    <span class="brand">Mail AI Check</span>
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
</template>
