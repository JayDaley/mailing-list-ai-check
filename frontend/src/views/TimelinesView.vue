<script setup>
// The Timelines screen (route /timelines, opened from the Lists pane): one
// adaptive rug plot per list with messages, stacked with no gap between rows
// so the whole corpus reads as one wall of activity. Every row shares the
// corpus-wide time domain from GET /api/lists/timelines, so columns align
// vertically; the sticky month axis at the top names the positions.
//
// Rows keep the server's order (message count descending then name). Clicking
// a list name returns to the dashboard filtered to it; a single-message rug
// bar opens that message; a binned column returns to the dashboard filtered to
// the list and the bin's date span.
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get } from '../api'
import TimelineRug from '../components/TimelineRug.vue'
import { fmtInt } from '../lib/format'
import { BUCKET_PHRASES, TIMELINE_BUCKETS, bucketColor } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'

const filters = useFiltersStore()
const route = useRoute()
const router = useRouter()

// --- data ---------------------------------------------------------------------
const payload = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    payload.value = await get('/lists/timelines')
  } catch (err) {
    payload.value = null
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const startMs = computed(() => (payload.value?.start != null ? payload.value.start * 1000 : null))
const endMs = computed(() => (payload.value?.end != null ? payload.value.end * 1000 : null))

const rows = computed(() =>
  (payload.value?.lists || []).map((entry) => ({
    list: entry.list,
    count: fmtInt(entry.total),
    points: (entry.points || []).map(([id, t, bucket]) => ({
      id,
      t: t * 1000,
      bucket: TIMELINE_BUCKETS[bucket] || 'unscored',
    })),
  })),
)

const totalMessages = computed(() =>
  fmtInt((payload.value?.lists || []).reduce((sum, entry) => sum + (entry.total || 0), 0)),
)

const legend = TIMELINE_BUCKETS.map((bucket) => ({
  name: BUCKET_PHRASES[bucket] || bucket,
  color: bucketColor(bucket),
}))

// --- month axis -----------------------------------------------------------------
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// Every calendar month boundary inside the domain: a gridline runs down the
// whole stack at each one, and a thinned subset carries the axis labels.
const monthBoundaries = computed(() => {
  const s = startMs.value
  const e = endMs.value
  if (s == null || e == null || e <= s) return []
  const out = []
  const d = new Date(s)
  d.setUTCDate(1)
  d.setUTCHours(0, 0, 0, 0)
  d.setUTCMonth(d.getUTCMonth() + 1)
  while (d.getTime() < e) {
    out.push({
      left: (((d.getTime() - s) / (e - s)) * 100).toFixed(2) + '%',
      label: `${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`,
    })
    d.setUTCMonth(d.getUTCMonth() + 1)
  }
  return out
})

// Axis labels, thinned to a stride when the span holds more months than fit
// legibly (~20 labels).
const ticks = computed(() => {
  const all = monthBoundaries.value
  const stride = Math.max(1, Math.ceil(all.length / 20))
  return all.filter((_, i) => i % stride === 0)
})

// --- navigation -----------------------------------------------------------------
function goBack() {
  router.push({ path: '/', query: route.query })
}
function openMessage(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}
function openList(list) {
  router.push({ path: '/', query: filters.buildQuery({ list }) })
}
function openRange(list, range) {
  router.push({
    path: '/',
    query: filters.buildQuery({ list, date_from: range.from, date_to: range.to }),
  })
}
</script>

<template>
  <div class="tl-root">
    <div class="tl-toolbar">
      <button type="button" class="tl-back" @click="goBack">← Dashboard</button>
      <span class="tl-title">List timelines</span>
      <span class="tl-sub">
        {{ fmtInt(rows.length) }} lists · {{ totalMessages }} messages · shared time axis · a
        full-height bar is one email, shorter stacked columns are time bins
      </span>
      <span class="tl-spacer"></span>
      <span class="tl-legend">
        <span v-for="l in legend" :key="l.name" class="tl-legend-item">
          <span class="tl-legend-dot" :style="{ background: l.color }"></span>{{ l.name }}
        </span>
      </span>
    </div>

    <div class="tl-scroll">
      <div v-if="loading && !payload" class="tl-status">loading…</div>
      <div v-else-if="error" class="tl-status tl-error">{{ error }}</div>
      <div v-else-if="!rows.length" class="tl-status">no messages stored yet</div>
      <template v-else>
        <div class="tl-row tl-axis-row">
          <span class="tl-name"></span>
          <div class="tl-axis">
            <span v-for="t in ticks" :key="t.left" class="tl-tick" :style="{ left: t.left }">
              <span class="tl-tick-mark"></span>
              <span class="tl-tick-label mono">{{ t.label }}</span>
            </span>
          </div>
        </div>
        <div class="tl-rows">
          <div class="tl-grid" aria-hidden="true">
            <span
              v-for="g in monthBoundaries"
              :key="g.left"
              class="tl-grid-line"
              :style="{ left: g.left }"
            ></span>
          </div>
          <div v-for="r in rows" :key="r.list" class="tl-row">
            <span class="tl-name mono" :title="`Filter the dashboard to ${r.list}`" @click="openList(r.list)">
              {{ r.list }} <span class="tl-count">{{ r.count }}</span>
            </span>
            <TimelineRug
              class="tl-rug"
              :points="r.points"
              :start="startMs"
              :end="endMs"
              :height="16"
              :col-width="2"
              @open="openMessage"
              @range="(rg) => openRange(r.list, rg)"
            />
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.mono {
  font-family: var(--mono);
}
.tl-root {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 10px 16px 16px;
}
.tl-toolbar {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 0 0 8px;
  flex: none;
}
.tl-back {
  font-size: 11px;
  font-weight: 600;
  border: none;
  background: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0;
}
.tl-title {
  font-size: 12px;
  font-weight: 700;
}
.tl-sub {
  font-size: 11px;
  color: var(--text-muted);
}
.tl-spacer {
  flex: 1;
}
.tl-legend {
  display: inline-flex;
  gap: 10px;
  font-size: 10.5px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.tl-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.tl-legend-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}
.tl-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0 12px 12px;
}
.tl-status {
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 8px 0;
}
.tl-error {
  color: var(--danger);
}
/* One row per list: the name column and the rug, stacked with no gap. The
   wrapper anchors the month gridlines, which span every row. */
.tl-rows {
  position: relative;
}
.tl-row {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 0 10px;
  align-items: stretch;
}
/* Month gridlines. The overlay covers the rug column alone — its 250px left
   inset is the name column (240px) plus the grid gap (10px) above — and sits
   before the rows in the DOM, so the rug bars paint over the lines. */
.tl-grid {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 250px;
  right: 0;
  pointer-events: none;
}
.tl-grid-line {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border);
}
.tl-row:not(.tl-axis-row):hover {
  background: var(--hover-row);
}
.tl-name {
  font-size: 10px;
  color: var(--text-name);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 16px;
  cursor: pointer;
}
.tl-name:hover {
  color: var(--accent);
}
.tl-count {
  color: var(--text-faint);
}
.tl-rug {
  align-self: end;
}
/* Sticky month axis above the wall of rows. */
.tl-axis-row {
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 6px 0 0;
}
.tl-axis {
  position: relative;
  height: 18px;
}
.tl-tick {
  position: absolute;
  top: 0;
  bottom: 0;
}
.tl-tick-mark {
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 0;
  width: 1px;
  background: var(--border);
}
.tl-tick-label {
  position: absolute;
  left: 3px;
  top: 0;
  font-size: 9px;
  color: var(--text-muted);
  white-space: nowrap;
}
</style>
