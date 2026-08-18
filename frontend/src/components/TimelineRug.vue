<script setup>
// Adaptive rug plot over a time axis. Accepts any number of points and picks
// its form from the pixels available:
//
//   - Rug: while no time bin holds more than one message, every message is an
//     individual full-height bar at its position on the axis — the classic rug.
//   - Binned: once messages outnumber the pixels, or cluster into bursts, each
//     occupied bin becomes a column whose height scales with its message count
//     (square-root, so sparse bins stay visible beside a burst) and whose fill
//     stacks the prediction buckets in the mix-bar order, bottom-up.
//
// The two forms are one rendering path: a bin of one message is a full-height,
// single-color bar that opens that message on click (`open`, with the message
// id); a bin of several emits its inclusive date span (`range`, as
// {from, to} YYYY-MM-DD) for the parent to filter by. Gaps stay empty, so
// quiet periods read as quiet.
//
// Bins are laid over [start, end] when given — stacked plots sharing a domain
// stay aligned — and over the points' own extent otherwise.
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
  // Fixed column width in px (≥ 2), overriding the adaptive width. Stacked
  // plots sharing a domain pass one value so every row bins identically —
  // same mark width, same bin boundaries — whatever its message count.
  colWidth: { type: Number, default: null },
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
onBeforeUnmount(() => observer?.disconnect())

// Columns are as fat as the data allows — few messages keep the familiar
// chunky bars — but never fatter than 6px nor thinner than 2px (a 1px bar and
// its 1px gap), the floor that sets how many bins one pixel row can hold.
const MIN_COL = 2
const MAX_COL = 6
const MIN_BAR_H = 2

const columns = computed(() => {
  const w = Math.floor(width.value)
  const pts = props.points.filter((p) => Number.isFinite(p.t))
  if (w < MIN_COL || !pts.length) return []

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

  const colW =
    props.colWidth != null
      ? Math.max(MIN_COL, Math.floor(props.colWidth))
      : Math.max(MIN_COL, Math.min(MAX_COL, Math.floor(w / pts.length)))
  const k = Math.max(1, Math.floor(w / colW))

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

  const h = props.height
  return [...bins.values()].map((b) => ({
    key: b.i,
    left: b.i * colW + 'px',
    width: colW - 1 + 'px',
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
})

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
  const b = col.bin
  if (b.single) emit('open', b.single.id)
  else emit('range', { from: isoDay(b.tMin), to: isoDay(b.tMax + 24 * 60 * 60 * 1000) })
}
</script>

<template>
  <span ref="rootEl" class="timeline-rug" :style="{ height: height + 'px' }">
    <span
      v-for="c in columns"
      :key="c.key"
      class="tlr-col"
      :style="{ left: c.left, width: c.width, height: c.height }"
      :title="c.title"
      @click.stop="onClick(c)"
    >
      <span
        v-for="seg in c.segments"
        :key="seg.bucket"
        class="tlr-seg"
        :style="{ flexGrow: seg.grow, background: seg.color }"
      ></span>
    </span>
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
</style>
