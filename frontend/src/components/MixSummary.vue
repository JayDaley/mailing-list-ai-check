<script setup>
// "Detection bar with numbers": a fluid MixBar over a {label: count}
// distribution plus a caption line giving each label's count and share of the
// scored total. Replaces the old Avg-AI / Flagged stat tiles on the list and
// sender detail cards.
import { computed } from 'vue'

import { fmtInt } from '../lib/format'
import { LABEL_COLORS, LABEL_ORDER, LABEL_SHORT } from '../lib/labels'
import MixBar from './MixBar.vue'

const props = defineProps({
  // Object of label -> count. Unknown / zero labels render as 0.
  counts: { type: Object, default: () => ({}) },
  clickable: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

const total = computed(() =>
  LABEL_ORDER.reduce((sum, l) => sum + (Number(props.counts?.[l]) || 0), 0),
)

const items = computed(() =>
  LABEL_ORDER.map((l) => {
    const n = Number(props.counts?.[l]) || 0
    return {
      label: l,
      word: LABEL_SHORT[l],
      count: fmtInt(n),
      pct: total.value ? Math.round((n / total.value) * 100) + '%' : '—',
      color: LABEL_COLORS[l],
    }
  }),
)
</script>

<template>
  <div class="mix-summary">
    <MixBar
      :counts="counts"
      :height="12"
      :clickable="clickable"
      @select="(l) => emit('select', l)"
    />
    <div v-if="total" class="mix-summary-caption">
      <span
        v-for="it in items"
        :key="it.label"
        class="mix-summary-item"
        :class="{ 'mix-summary-click': clickable }"
        :title="clickable ? 'Filter to ' + it.label : undefined"
        @click="clickable && emit('select', it.label)"
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
