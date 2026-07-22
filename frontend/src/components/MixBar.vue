<script setup>
// Stacked label-mix bar. Segments are drawn in LABEL_ORDER, each sized by its
// share of the total scored count, colored from LABEL_COLORS, on a #eef0f3
// track. Used at 10px (toolbar, 200px wide) and 9px (list / sender rows).
import { computed } from 'vue'
import { LABEL_ORDER, LABEL_COLORS } from '../lib/labels'

const props = defineProps({
  // Object of label -> count. Unknown / zero labels are ignored.
  counts: { type: Object, default: () => ({}) },
  height: { type: Number, default: 10 },
  // A CSS width (e.g. '200px'), or null / '' for fluid (fills its flex slot).
  width: { type: [String, null], default: null },
  clickable: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

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
      title:
        `${l}: ${n} of ${total} scored (${Math.round(pct)}%)` +
        (props.clickable ? ' — click to filter' : ''),
    }
  })
})

const trackStyle = computed(() => ({
  display: 'flex',
  height: props.height + 'px',
  width: props.width || undefined,
  flex: props.width ? 'none' : '1 1 auto',
  borderRadius: '2px',
  overflow: 'hidden',
  background: '#eef0f3',
}))
</script>

<template>
  <span :style="trackStyle" title="Detection mix of scored messages">
    <span
      v-for="seg in segments"
      :key="seg.label"
      :title="seg.title"
      :style="{
        display: 'block',
        width: seg.w,
        background: seg.color,
        cursor: clickable ? 'pointer' : 'default',
      }"
      @click="clickable && emit('select', seg.label)"
    ></span>
  </span>
</template>
