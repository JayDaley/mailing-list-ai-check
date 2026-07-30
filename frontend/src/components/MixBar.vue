<script setup>
// Stacked label-mix bar. Segments are drawn in `order` — by default the three
// prediction_short buckets (Human / Mixed / AI) — each sized by its share of
// the total and colored from LABEL_COLORS, on a #eef0f3 track. Messages gated
// under the reliability floor (`tooShort`) close the bar as one grey segment and
// count towards the total behind every share. Used at 10px (toolbar, 200px wide)
// and 9px (list / sender rows).
//
// Hovering anywhere on the bar shows a popup with each bucket's share, e.g.
// "Human (60%) · Mixed (20%) · AI (20%)" — positioned by useHoverPop.
import { computed } from 'vue'

import { fmtInt } from '../lib/format'
import { useHoverPop } from '../lib/hoverPop'
import { PRED_ORDER, LABEL_COLORS, LABEL_SHORT, TOO_SHORT_COLOR } from '../lib/labels'

// Popup name of the too-short segment (never a filterable label, so it carries
// no click).
const TOO_SHORT_LABEL = 'Too short'

const props = defineProps({
  // Object of label -> message count. Labels outside `order` are ignored;
  // zero values draw no segment.
  counts: { type: Object, default: () => ({}) },
  height: { type: Number, default: 10 },
  // A CSS width (e.g. '200px'), or null / '' for fluid (fills its flex slot).
  width: { type: [String, null], default: null },
  clickable: { type: Boolean, default: false },
  // Which labels to draw, in order.
  order: { type: Array, default: () => PRED_ORDER },
  // Display text per label in the hover popup. Defaults to the bucket names,
  // matching the analysis pills ("Human" / "Mixed" / "AI").
  phrases: { type: Object, default: () => LABEL_SHORT },
  // Show each bucket's message count alongside its share.
  showCounts: { type: Boolean, default: true },
  // Fill color per label (segments + popup dots).
  colors: { type: Object, default: () => LABEL_COLORS },
  // Messages gated under the 50-word reliability floor (extraction status
  // `too_short`): a trailing grey segment, and part of the total every share is
  // computed over.
  tooShort: { type: Number, default: 0 },
})

// The values actually drawn (the store's labels are already the three
// prediction_short buckets).
const effCounts = computed(() => props.counts || {})

const emit = defineEmits(['select'])

const { wrapEl, popEl, hover, popStyle, arrowLeft, show: showPop, hide: hidePop } = useHoverPop()

const totalScored = computed(() =>
  props.order.reduce((sum, l) => sum + (Number(effCounts.value?.[l]) || 0), 0),
)

const tooShortCount = computed(() => Math.max(0, Number(props.tooShort) || 0))

// The denominator behind every width and percentage: the scored messages plus
// the gated ones the trailing segment stands for.
const total = computed(() => totalScored.value + tooShortCount.value)

const colorFor = (label) => props.colors[label] || LABEL_COLORS[label]

const segments = computed(() => {
  const denom = total.value
  if (!denom) return []
  const parts = props.order
    .filter((l) => (Number(effCounts.value?.[l]) || 0) > 0)
    .map((l) => {
      const n = Number(effCounts.value[l]) || 0
      return {
        label: l,
        color: colorFor(l),
        w: ((n / denom) * 100).toFixed(1) + '%',
        clickable: props.clickable,
      }
    })
  if (tooShortCount.value > 0) {
    parts.push({
      label: TOO_SHORT_LABEL,
      color: TOO_SHORT_COLOR,
      w: ((tooShortCount.value / denom) * 100).toFixed(1) + '%',
      clickable: false,
    })
  }
  return parts
})

// One entry per label (including zeros) for the hover popup, plus the too-short
// entry when there is one.
const summaryParts = computed(() => {
  const denom = total.value
  const share = (n) => (denom ? Math.round((n / denom) * 100) : 0)
  const parts = props.order.map((l) => {
    const n = Number(effCounts.value?.[l]) || 0
    return {
      label: props.phrases[l] || LABEL_SHORT[l] || l,
      color: colorFor(l),
      pct: share(n),
      count: n,
    }
  })
  if (tooShortCount.value > 0) {
    parts.push({
      label: TOO_SHORT_LABEL,
      color: TOO_SHORT_COLOR,
      pct: share(tooShortCount.value),
      count: tooShortCount.value,
    })
  }
  return parts
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
          cursor: seg.clickable ? 'pointer' : 'default',
        }"
        @click="seg.clickable && emit('select', seg.label)"
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
