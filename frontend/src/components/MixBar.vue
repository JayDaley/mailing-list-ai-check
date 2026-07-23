<script setup>
// Stacked label-mix bar. Segments are drawn in LABEL_ORDER, each sized by its
// share of the total scored count, colored from LABEL_COLORS, on a #eef0f3
// track. Used at 10px (toolbar, 200px wide) and 9px (list / sender rows).
//
// Hovering anywhere on the bar shows a popup with every label's share, e.g.
// "Human (60%) · Mixed (10%) · Assisted (10%) · AI (20%)". The popup is
// teleported to <body> with fixed positioning so scroll-clipping panes
// (overflow: hidden) can't cut it off; its coordinates are computed from the
// bar's on-screen rect on hover. A native title is kept as a fallback.
import { computed, ref, nextTick } from 'vue'
import { LABEL_ORDER, LABEL_COLORS, LABEL_SHORT } from '../lib/labels'

const props = defineProps({
  // Object of label -> count. Unknown / zero labels are ignored.
  counts: { type: Object, default: () => ({}) },
  height: { type: Number, default: 10 },
  // A CSS width (e.g. '200px'), or null / '' for fluid (fills its flex slot).
  width: { type: [String, null], default: null },
  clickable: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

const wrapEl = ref(null)
const popEl = ref(null)
const hover = ref(false)
const popStyle = ref({})
const arrowLeft = ref('12px')

const totalScored = computed(() =>
  LABEL_ORDER.reduce((sum, l) => sum + (Number(props.counts?.[l]) || 0), 0),
)

const segments = computed(() => {
  const total = totalScored.value
  if (!total) return []
  return LABEL_ORDER.filter((l) => (Number(props.counts?.[l]) || 0) > 0).map((l) => {
    const n = Number(props.counts[l]) || 0
    const pct = (n / total) * 100
    return {
      label: l,
      color: LABEL_COLORS[l],
      w: pct.toFixed(1) + '%',
    }
  })
})

// One entry per label (all four, including zeros) for the hover popup.
const summaryParts = computed(() => {
  const total = totalScored.value
  return LABEL_ORDER.map((l) => {
    const n = Number(props.counts?.[l]) || 0
    return {
      label: LABEL_SHORT[l],
      color: LABEL_COLORS[l],
      pct: total ? Math.round((n / total) * 100) : 0,
    }
  })
})

// Plain-text version for the native title (accessibility / fallback).
const summaryTitle = computed(() =>
  totalScored.value
    ? summaryParts.value.map((p) => `${p.label} (${p.pct}%)`).join(' · ')
    : 'No scored messages',
)

// Position the (teleported, fixed) popup above the bar, clamped to the viewport
// so it never spills off the right edge; the arrow tracks the bar's left.
async function showPop() {
  hover.value = true
  await nextTick()
  const wrap = wrapEl.value
  const pop = popEl.value
  if (!wrap || !pop) return
  const r = wrap.getBoundingClientRect()
  const gap = 6
  const margin = 8
  const pw = pop.offsetWidth
  let left = r.left
  const maxLeft = window.innerWidth - pw - margin
  if (left > maxLeft) left = Math.max(margin, maxLeft)
  popStyle.value = {
    position: 'fixed',
    left: left + 'px',
    top: r.top - gap + 'px',
    transform: 'translateY(-100%)',
  }
  // Keep the arrow pointing at (roughly) the start of the bar.
  const a = Math.min(Math.max(r.left - left + 12, 8), pw - 8)
  arrowLeft.value = a + 'px'
}

function hidePop() {
  hover.value = false
}

const trackStyle = computed(() => ({
  display: 'flex',
  height: props.height + 'px',
  flex: '1 1 auto',
  borderRadius: '2px',
  overflow: 'hidden',
  background: '#eef0f3',
}))

const wrapStyle = computed(() => ({
  display: 'flex',
  width: props.width || undefined,
  flex: props.width ? 'none' : '1 1 auto',
}))
</script>

<template>
  <span
    ref="wrapEl"
    :style="wrapStyle"
    :title="summaryTitle"
    @mouseenter="showPop"
    @mouseleave="hidePop"
  >
    <span :style="trackStyle">
      <span
        v-for="seg in segments"
        :key="seg.label"
        :style="{
          display: 'block',
          width: seg.w,
          background: seg.color,
          cursor: clickable ? 'pointer' : 'default',
        }"
        @click="clickable && emit('select', seg.label)"
      ></span>
    </span>

    <Teleport to="body">
      <span
        v-if="hover"
        ref="popEl"
        class="mixbar-pop"
        role="tooltip"
        :style="{ ...popStyle, '--arrow-left': arrowLeft }"
      >
        <span v-for="(p, i) in summaryParts" :key="p.label" class="mixbar-pop-item">
          <span class="mixbar-pop-sep" v-if="i > 0" aria-hidden="true">·</span>
          <span class="mixbar-pop-dot" :style="{ background: p.color }"></span>
          <span class="mixbar-pop-name">{{ p.label }}</span>
          <span class="mixbar-pop-pct">({{ p.pct }}%)</span>
        </span>
      </span>
    </Teleport>
  </span>
</template>

<style scoped>
.mixbar-pop {
  z-index: 1000;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 5px 9px;
  border-radius: 6px;
  background: #1f2429;
  color: #f4f6f8;
  font-size: 11px;
  line-height: 1;
  white-space: nowrap;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.22);
  pointer-events: none;
}
.mixbar-pop::after {
  content: '';
  position: absolute;
  top: 100%;
  left: var(--arrow-left, 12px);
  margin-left: -4px;
  border: 4px solid transparent;
  border-top-color: #1f2429;
}
.mixbar-pop-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mixbar-pop-sep {
  margin: 0 3px 0 1px;
  color: #7a828b;
}
.mixbar-pop-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}
.mixbar-pop-name {
  font-weight: 600;
}
.mixbar-pop-pct {
  color: #b8bfc7;
}
</style>
