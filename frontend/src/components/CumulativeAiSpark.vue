<script setup>
// Cumulative-AI sparkline: when did this sender's AI use start?
//
// Two normalized cumulative curves over one time axis, drawn as steps:
//
//   - Grey area — the sender's posts to date, as a fraction of their final
//     count. The silhouette doubles as the posting history (quiet periods are
//     flat).
//   - AI-colored line — their AI-labelled messages to date, as a fraction of
//     their final AI count. Drawn only when there is at least one, so absence
//     of the line means a clean sender.
//
// Each curve is normalized to its own final value, so both run corner to
// corner and the *shape* carries the signal whatever the sender's AI share: a
// line tracking the area's edge is a mix that never changed; one hugging the
// floor then cutting upward is a late adopter. Magnitudes live in the MixBar
// above and in this component's tooltip, not here.
//
// The x-domain is [start, end] when given — every row of a table shares one
// axis, so "March" lands at the same pixel in each — and the points' own
// extent otherwise.
import { computed } from 'vue'

import { fmtDate, fmtInt } from '../lib/format'
import { LABEL_COLORS } from '../lib/labels'

const props = defineProps({
  // [{t, ai}] — t in epoch ms, ai truthy for an AI-labelled message. Assumed
  // sorted by t ascending (the API serves them that way).
  points: { type: Array, default: () => [] },
  // Shared domain (epoch ms) for aligned rows; null → the points' extent.
  start: { type: Number, default: null },
  end: { type: Number, default: null },
  height: { type: Number, default: 12 },
})

// Steps beyond this are decimated: at sparkline size extra vertices are
// invisible, and a 60-row table should not carry thousands of path segments.
const MAX_STEPS = 300

const pts = computed(() => props.points.filter((p) => Number.isFinite(p.t)))

const aiTotal = computed(() => pts.value.reduce((sum, p) => sum + (p.ai ? 1 : 0), 0))

const firstAiAt = computed(() => pts.value.find((p) => p.ai)?.t ?? null)

// Both cumulative fractions at every message, with the shared x mapping.
const steps = computed(() => {
  const arr = pts.value
  const n = arr.length
  if (!n) return []

  let s = props.start
  let e = props.end
  if (s == null) s = arr[0].t
  if (e == null) e = arr[n - 1].t
  if (!(e > s)) e = s + 1

  const all = []
  let ai = 0
  for (let i = 0; i < n; i++) {
    if (arr[i].ai) ai += 1
    all.push({
      x: Math.min(100, Math.max(0, ((arr[i].t - s) / (e - s)) * 100)),
      total: ((i + 1) / n) * 100,
      ai: aiTotal.value ? (ai / aiTotal.value) * 100 : 0,
    })
  }
  if (n <= MAX_STEPS) return all
  const stride = Math.ceil(n / MAX_STEPS)
  const sampled = all.filter((_, i) => i % stride === 0)
  if (sampled[sampled.length - 1] !== all[n - 1]) sampled.push(all[n - 1])
  return sampled
})

// A step-after path over one of the cumulative series ('total' | 'ai').
function stepPath(key) {
  let d = 'M 0 100'
  for (const p of steps.value) d += ` H ${p.x.toFixed(2)} V ${(100 - p[key]).toFixed(2)}`
  return d + ' H 100'
}

const areaPath = computed(() => (steps.value.length ? stepPath('total') + ' V 100 Z' : ''))
const linePath = computed(() => (steps.value.length && aiTotal.value ? stepPath('ai') : ''))

const aiColor = LABEL_COLORS.AI

const title = computed(() => {
  const n = pts.value.length
  if (!n) return 'no dated messages'
  const posts = `${fmtInt(n)} dated message${n === 1 ? '' : 's'}`
  if (!aiTotal.value) return `${posts} · no AI`
  return `${posts} · ${fmtInt(aiTotal.value)} AI · first AI ${fmtDate(firstAiAt.value)}`
})
</script>

<template>
  <svg
    v-if="steps.length"
    class="cum-spark"
    :style="{ height: height + 'px' }"
    viewBox="0 0 100 100"
    preserveAspectRatio="none"
  >
    <title>{{ title }}</title>
    <path class="cum-spark-area" :d="areaPath" />
    <path v-if="linePath" class="cum-spark-line" :stroke="aiColor" :d="linePath" />
  </svg>
</template>

<style scoped>
.cum-spark {
  display: block;
  width: 100%;
  min-width: 0;
}
.cum-spark-area {
  fill: var(--border);
  stroke: none;
}
.cum-spark-line {
  fill: none;
  stroke-width: 1.4;
  vector-effect: non-scaling-stroke;
  stroke-linejoin: round;
}
</style>
