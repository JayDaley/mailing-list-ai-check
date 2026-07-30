<script setup>
// "Detection bar with numbers": a fluid MixBar over a {label: count}
// distribution plus a caption line giving each label's count and share of the
// total. Replaces the old Avg-AI / Flagged stat tiles on the list and
// sender detail cards. Messages gated under the reliability floor close the bar
// as a grey segment and count towards the total behind every share, so the
// caption carries a "Too short" entry of its own.
import { computed } from 'vue'

import { fmtInt } from '../lib/format'
import {
  LABEL_COLORS,
  PRED_ORDER,
  LABEL_SHORT,
  TOO_SHORT_COLOR,
  foldToPrediction,
} from '../lib/labels'
import MixBar from './MixBar.vue'

const props = defineProps({
  // Object of prediction_short -> count. Unknown / zero labels render as 0.
  counts: { type: Object, default: () => ({}) },
  clickable: { type: Boolean, default: false },
  // Messages gated under the 50-word reliability floor (extraction status
  // `too_short`), never sent to Pangram and so carrying no label.
  tooShort: { type: Number, default: 0 },
})

const emit = defineEmits(['select'])

// The three prediction_short buckets, numerically coerced.
const folded = computed(() => foldToPrediction(props.counts))

const tooShortCount = computed(() => Math.max(0, Number(props.tooShort) || 0))

const scored = computed(() =>
  PRED_ORDER.reduce((sum, l) => sum + (Number(folded.value?.[l]) || 0), 0),
)

// The denominator of every share, matching the MixBar segment widths.
const total = computed(() => scored.value + tooShortCount.value)

const items = computed(() => {
  const share = (n) => (total.value ? Math.round((n / total.value) * 100) + '%' : '—')
  const parts = PRED_ORDER.map((l) => ({
    label: l,
    word: LABEL_SHORT[l],
    count: fmtInt(Number(folded.value?.[l]) || 0),
    pct: share(Number(folded.value?.[l]) || 0),
    color: LABEL_COLORS[l],
    click: props.clickable,
  }))
  if (tooShortCount.value > 0) {
    parts.push({
      label: 'too-short',
      // Not a label, so it never filters the messages table.
      word: 'Too short',
      count: fmtInt(tooShortCount.value),
      pct: share(tooShortCount.value),
      color: TOO_SHORT_COLOR,
      click: false,
    })
  }
  return parts
})
</script>

<template>
  <div class="mix-summary">
    <MixBar
      :counts="counts"
      :height="12"
      :clickable="clickable"
      :too-short="tooShort"
      @select="(l) => emit('select', l)"
    />
    <div v-if="total" class="mix-summary-caption">
      <span
        v-for="it in items"
        :key="it.label"
        class="mix-summary-item"
        :class="{ 'mix-summary-click': it.click }"
        :title="it.click ? 'Filter to ' + it.label : undefined"
        @click="it.click && emit('select', it.label)"
      >
        <span class="mix-summary-swatch" :style="{ background: it.color }"></span>
        <span>{{ it.word }} {{ it.count }} ({{ it.pct }})</span>
      </span>
    </div>
    <div v-else class="mix-summary-empty">no scored messages</div>
  </div>
</template>

<style scoped>
.mix-summary {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.mix-summary-caption {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 10.5px;
  color: var(--text-secondary);
  font-family: var(--mono);
}
.mix-summary-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.mix-summary-click {
  cursor: pointer;
}
.mix-summary-click:hover {
  color: var(--text);
}
.mix-summary-swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
}
.mix-summary-empty {
  font-size: 10.5px;
  color: var(--text-muted);
}
</style>
