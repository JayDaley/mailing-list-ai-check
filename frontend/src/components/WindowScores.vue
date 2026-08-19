<script setup>
// The "AI Score (Confidence)" cell: Pangram's per-window ai_assistance_score
// and confidence, one entry per window in document order, each framed by a
// 2px border in the window's verdict colour (Human / Mixed / AI).
//
// Pangram emits no document-level score — the only continuous score it produces
// is per window — so the cell lists every window rather than a single figure.
// Scores are shown at two decimal places: the window labels are banded at
// roughly 0.25 / 0.5 / 0.75, and the digits beyond two are float32 noise (see
// docs/pangram-output.md).
//
// The row is clipped to the column width; hovering shows the count and the
// full list.
import { computed } from 'vue'

import { LABEL_COLORS, windowBucket } from '../lib/labels'
import { useHoverPop } from '../lib/hoverPop'

const props = defineProps({
  // Array of {ai_assistance_score, confidence, label} in document order.
  windows: { type: Array, default: () => [] },
})

const { wrapEl, popEl, hover, popStyle, arrowLeft, show, hide } = useHoverPop()

// Confidence is abbreviated to its initial (High → H) to fit several windows in
// the column; the drawer's analysis table spells it out.
const CONFIDENCE_INITIAL = { High: 'H', Medium: 'M', Low: 'L' }

const entries = computed(() =>
  props.windows.map((w) => {
    const s = Number(w?.ai_assistance_score)
    const conf = CONFIDENCE_INITIAL[w?.confidence] || w?.confidence || '—'
    const bucket = windowBucket(w?.label)
    return {
      text: `${Number.isFinite(s) ? s.toFixed(2) : '—'} (${conf})`,
      color: bucket ? LABEL_COLORS[bucket] : LABEL_COLORS.unscored,
    }
  }),
)

const popText = computed(() => {
  const n = entries.value.length
  return `${n} window${n === 1 ? '' : 's'}: ${entries.value.map((e) => e.text).join(' · ')}`
})
</script>

<template>
  <span
    v-if="entries.length"
    ref="wrapEl"
    class="winscores"
    @mouseenter="show"
    @mouseleave="hide"
  >
    <span
      v-for="(e, i) in entries"
      :key="i"
      class="winscore-chip"
      :style="{ borderColor: e.color }"
      >{{ e.text }}</span
    >
  </span>
  <span v-else class="winscores-dash">—</span>

  <Teleport to="body">
    <span
      v-if="hover"
      ref="popEl"
      class="hover-pop"
      role="tooltip"
      :style="{ ...popStyle, '--arrow-left': arrowLeft }"
      >{{ popText }}</span
    >
  </Teleport>
</template>

<style scoped>
.winscores {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  min-width: 0;
  font-family: var(--mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  color: #1c2024;
}
/* One window's score/confidence pair, framed in the window's verdict colour. */
.winscore-chip {
  flex: none;
  border: 2px solid;
  border-radius: 3px;
  padding: 0 3px;
  line-height: 14px;
  white-space: nowrap;
}
.winscores-dash {
  color: #b3b9c0;
}
</style>
