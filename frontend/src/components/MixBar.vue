<script setup>
// Stacked label-mix bar. Segments are drawn in `order` — by default the three
// prediction_short buckets (Human / Mixed / AI) — each sized by its share of
// the total and colored from LABEL_COLORS, on a #eef0f3 track. Used at 10px
// (toolbar, 200px wide) and 9px (list / sender rows).
//
// Hovering anywhere on the bar shows a popup with each bucket's share, e.g.
// "Human (60%) · Mixed (20%) · AI (20%)" — positioned by useHoverPop.
import { computed } from 'vue'

import { fmtInt } from '../lib/format'
import { useHoverPop } from '../lib/hoverPop'
import { PRED_ORDER, LABEL_COLORS, LABEL_SHORT } from '../lib/labels'

const props = defineProps({
  // Object of label -> message count. Labels outside `order` are ignored (but
  // see `fold`); zero values draw no segment.
  counts: { type: Object, default: () => ({}) },
  height: { type: Number, default: 10 },
  // A CSS width (e.g. '200px'), or null / '' for fluid (fills its flex slot).
  width: { type: [String, null], default: null },
  clickable: { type: Boolean, default: false },
  // Which labels to draw, in order.
  order: { type: Array, default: () => PRED_ORDER },
  // Fold an "AI-Assisted" count into "Mixed" (the prediction_short view).
  fold: { type: Boolean, default: true },
  // Display text per label in the hover popup. Defaults to the bucket names,
  // matching the analysis pills ("Human" / "Mixed" / "AI").
  phrases: { type: Object, default: () => LABEL_SHORT },
  // Show each bucket's message count alongside its share.
  showCounts: { type: Boolean, default: true },
  // Fill color per label (segments + popup dots).
  colors: { type: Object, default: () => LABEL_COLORS },
})

// The values actually drawn: optionally folding AI-Assisted into Mixed so a
// four-band {label: count} map renders as the three prediction_short buckets.
const effCounts = computed(() => {
  const c = props.counts || {}
  if (!props.fold) return c
  return {
    ...c,
    Mixed: (Number(c.Mixed) || 0) + (Number(c['AI-Assisted']) || 0),
    'AI-Assisted': 0,
  }
})

const emit = defineEmits(['select'])

const { wrapEl, popEl, hover, popStyle, arrowLeft, show: showPop, hide: hidePop } = useHoverPop()

const totalScored = computed(() =>
  props.order.reduce((sum, l) => sum + (Number(effCounts.value?.[l]) || 0), 0),
)

const colorFor = (label) => props.colors[label] || LABEL_COLORS[label]

const segments = computed(() => {
  const total = totalScored.value
  if (!total) return []
  return props.order.filter((l) => (Number(effCounts.value?.[l]) || 0) > 0).map((l) => {
    const n = Number(effCounts.value[l]) || 0
    const pct = (n / total) * 100
    return {
      label: l,
      color: colorFor(l),
      w: pct.toFixed(1) + '%',
    }
  })
})

// One entry per label (including zeros) for the hover popup.
const summaryParts = computed(() => {
  const total = totalScored.value
  return props.order.map((l) => {
    const n = Number(effCounts.value?.[l]) || 0
    return {
      label: props.phrases[l] || LABEL_SHORT[l] || l,
      color: colorFor(l),
      pct: total ? Math.round((n / total) * 100) : 0,
      count: n,
    }
  })
})

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
        class="hover-pop"
        role="tooltip"
        :style="{ ...popStyle, '--arrow-left': arrowLeft }"
      >
        <span v-for="(p, i) in summaryParts" :key="p.label" class="mixbar-pop-item">
          <span class="mixbar-pop-sep" v-if="i > 0" aria-hidden="true">·</span>
          <span class="mixbar-pop-dot" :style="{ background: p.color }"></span>
          <span class="mixbar-pop-name">{{ p.label }}</span>
          <span class="mixbar-pop-pct"
            >({{ p.pct }}%<template v-if="showCounts"> · {{ fmtInt(p.count) }}</template>)</span
          >
        </span>
      </span>
    </Teleport>
  </span>
</template>

<style scoped>
/* The popup shell (.hover-pop) is styled globally in assets/main.css. */
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
