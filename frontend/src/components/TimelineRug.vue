<script setup>
// Rug plot over a time axis. Every occupied time bin is a fixed 2px-wide
// column, butted against its neighbours with no gap:
//
//   - Rug: while no time bin holds more than one message, every message is an
//     individual full-height bar at its position on the axis — the classic rug.
//   - Binned: once messages cluster into bursts, each occupied bin becomes a
//     column whose height scales with its message count (square-root, so
//     sparse bins stay visible beside a burst) and whose fill stacks the
//     prediction buckets in the mix-bar order, bottom-up.
//
// The two forms are one rendering path: a bin of one message is a full-height,
// single-color bar that opens that message on click (`open`, with the message
// id); a bin of several emits its inclusive date span (`range`, as
// {from, to} YYYY-MM-DD) for the parent to filter by. Empty bins stay empty,
// so quiet periods read as quiet.
//
// Bins are laid over [start, end] when given — stacked plots sharing a domain
// stay aligned — and over the points' own extent otherwise.
//
// Hovering a column shows its bin (date, count, mix) in a fixed tooltip after
// a short delay — far shorter than the native title delay it replaces.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { fmtDate, fmtInt } from '../lib/format'
import { BUCKET_PHRASES, TIMELINE_BUCKETS, bucketColor } from '../lib/labels'

const props = defineProps({
  // [{id, t, bucket, subject?}] — t in epoch ms, bucket per TIMELINE_BUCKETS.
  points: { type: Array, default: () => [] },
  // Shared domain (epoch ms) for aligned stacking; null → the points' extent.
  start: { type: Number, default: null },
  end: { type: Number, default: null },
  height: { type: Number, default: 14 },
})
const emit = defineEmits(['open', 'range'])

// --- measured width -----------------------------------------------------------
const rootEl = ref(null)
const width = ref(0)
let observer = null
onMounted(() => {
  observer = new ResizeObserver((entries) => {
    width.value = entries[0]?.contentRect?.width || 0
  })
  if (rootEl.value) observer.observe(rootEl.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  clearTimeout(tipTimer)
  document.removeEventListener('scroll', hideTip, true)
})

// Every rug uses the same fixed geometry: 2px columns with no gap between
// them, so every plot bins at the finest width one pixel row can hold and
// the same span covers the same pixels on every row.
const COL_W = 2
const MIN_BAR_H = 2

// The [start, end] domain the bins cover: the props when given, the points'
// own extent otherwise.
const domain = computed(() => {
  const pts = props.points.filter((p) => Number.isFinite(p.t))
  let s = props.start
  let e = props.end
  if (s == null || e == null) {
    let lo = Infinity
    let hi = -Infinity
    for (const p of pts) {
      if (p.t < lo) lo = p.t
      if (p.t > hi) hi = p.t
    }
    if (s == null) s = lo
    if (e == null) e = hi
  }
  if (!(e > s)) e = s + 1
  return { pts, s, e }
})

// Bin the points into columns for a plot w px wide and h px tall.
function buildColumns(w, h) {
  const { pts, s, e } = domain.value
  if (w < COL_W || !pts.length) return []

  const k = Math.max(1, Math.floor(w / COL_W))

  const bins = new Map()
  for (const p of pts) {
    const i = Math.min(k - 1, Math.max(0, Math.floor(((p.t - s) / (e - s)) * k)))
    let bin = bins.get(i)
    if (!bin) {
      bin = { i, total: 0, counts: {}, single: null, tMin: p.t, tMax: p.t }
      bins.set(i, bin)
    }
    bin.total += 1
    bin.counts[p.bucket] = (bin.counts[p.bucket] || 0) + 1
    bin.single = bin.total === 1 ? p : null
    if (p.t < bin.tMin) bin.tMin = p.t
    if (p.t > bin.tMax) bin.tMax = p.t
  }

  let maxCount = 1
  for (const b of bins.values()) if (b.total > maxCount) maxCount = b.total

  return [...bins.values()].map((b) => ({
    key: b.i,
    left: b.i * COL_W + 'px',
    width: COL_W + 'px',
    height:
      maxCount === 1 ? h + 'px' : Math.max(MIN_BAR_H, Math.round(h * Math.sqrt(b.total / maxCount))) + 'px',
    segments: TIMELINE_BUCKETS.filter((name) => b.counts[name] > 0).map((name) => ({
      bucket: name,
      grow: b.counts[name],
      color: bucketColor(name),
    })),
    title: binTitle(b),
    bin: b,
  }))
}

const columns = computed(() => buildColumns(Math.floor(width.value), props.height))

function binTitle(b) {
  if (b.single) {
    const p = b.single
    const word = BUCKET_PHRASES[p.bucket] || p.bucket
    return `${fmtDate(p.t)} · ${word}` + (p.subject != null ? ` — ${p.subject}` : '')
  }
  const mix = TIMELINE_BUCKETS.filter((name) => b.counts[name] > 0)
    .map((name) => `${BUCKET_PHRASES[name] || name} ${fmtInt(b.counts[name])}`)
    .join(' · ')
  return `${fmtInt(b.total)} messages · ${fmtDate(b.tMin)} – ${fmtDate(b.tMax)} · ${mix}`
}

// The bin span as date_from/date_to bounds. Those filters compare lexically
// over stored ISO datetimes (a documented sharp edge shared with the export
// CLI), so a bare date as date_to sorts before that day's own messages: the
// emitted `to` is therefore the day after the bin's last message, which covers
// the span under `<=` without reaching the next day's messages.
function isoDay(t) {
  return new Date(t).toISOString().slice(0, 10)
}

function onClick(col) {
  hideTip()
  const b = col.bin
  if (b.single) emit('open', b.single.id)
  else emit('range', { from: isoDay(b.tMin), to: isoDay(b.tMax + 24 * 60 * 60 * 1000) })
}

// --- hover tooltip -----------------------------------------------------------
// One fixed-position box naming the hovered column's bin, teleported to the
// body so no ancestor overflow clips it. It appears after a short delay: long
// enough to stay quiet while the pointer sweeps across a rug, far shorter
// than the native title delay it replaces. The position is clamped by CSS
// clamp(): 100vw/100vh resolve in the layout engine, which knows the real
// viewport even where window.innerWidth reports 0. The tooltip tracks the
// pointer, but a scroll moves the page under a stationary pointer — a
// capture-phase scroll listener hides it instead of letting it drift.
const TIP_DELAY = 120 // ms from entering a column to the tooltip showing
const TIP_WIDTH = 420 // px, matches .tlr-tip max-width
const TIP_HEIGHT = 44 // px, a two-line box (used only to clamp the bottom)
const TIP_GAP = 12 // px, pointer-to-corner offset

const tip = ref('') // the hovered column's text, or '' while hidden
const tipStyle = ref({})
let tipTimer = null
let pointer = { x: 0, y: 0 }

function positionTip() {
  tipStyle.value = {
    left: `clamp(6px, ${Math.round(pointer.x + TIP_GAP)}px, calc(100vw - ${TIP_WIDTH + 6}px))`,
    top: `clamp(6px, ${Math.round(pointer.y + TIP_GAP)}px, calc(100vh - ${TIP_HEIGHT + 6}px))`,
  }
}

function showTip(col, event) {
  pointer = { x: event.clientX, y: event.clientY }
  clearTimeout(tipTimer)
  tipTimer = setTimeout(() => {
    positionTip()
    tip.value = col.title
    document.addEventListener('scroll', hideTip, true)
  }, TIP_DELAY)
}

function moveTip(event) {
  pointer = { x: event.clientX, y: event.clientY }
  if (tip.value) positionTip()
}

function hideTip() {
  clearTimeout(tipTimer)
  if (!tip.value) return
  tip.value = ''
  document.removeEventListener('scroll', hideTip, true)
}
</script>

<template>
  <span ref="rootEl" class="timeline-rug" :style="{ height: height + 'px' }">
    <span
      v-for="c in columns"
      :key="c.key"
      class="tlr-col"
      :style="{ left: c.left, width: c.width, height: c.height }"
      @mouseenter="showTip(c, $event)"
      @mousemove="moveTip"
      @mouseleave="hideTip"
      @click.stop="onClick(c)"
    >
      <span
        v-for="seg in c.segments"
        :key="seg.bucket"
        class="tlr-seg"
        :style="{ flexGrow: seg.grow, background: seg.color }"
      ></span>
    </span>
    <Teleport to="body">
      <div v-if="tip" class="tlr-tip" :style="tipStyle">{{ tip }}</div>
    </Teleport>
  </span>
</template>

<style scoped>
.timeline-rug {
  position: relative;
  display: block;
  min-width: 0;
  overflow: hidden;
}
.tlr-col {
  position: absolute;
  bottom: 0;
  display: flex;
  /* Buckets stack bottom-up in TIMELINE_BUCKETS order: a mix bar set upright. */
  flex-direction: column-reverse;
  border-radius: 1px;
  overflow: hidden;
  cursor: pointer;
}
.tlr-col:hover {
  opacity: 0.75;
}
.tlr-seg {
  flex-basis: 0;
  min-height: 1px;
}
/* The hover tooltip: a fixed, inert box above everything (its z-index tops
   the lightboxes and popovers), so it never steals the hover or the click
   from the column that raised it. */
.tlr-tip {
  position: fixed;
  z-index: 400;
  pointer-events: none;
  max-width: 420px;
  padding: 4px 8px;
  font-size: 12px;
  line-height: 1.5;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
}
</style>
