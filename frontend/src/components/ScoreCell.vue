<script setup>
// The score display used in the messages table: a label badge (white text on the
// label color) plus a percent pill (mono, band tint/color from fraction_ai).
// Unscored rows render an em dash in the faint color.
import { computed } from 'vue'
import { LABEL_COLORS, bandFor } from '../lib/labels'

const props = defineProps({
  // A score object { label, fraction_ai } or null/undefined when unscored.
  score: { type: Object, default: null },
})

const scored = computed(() => props.score != null && props.score.fraction_ai != null)
const label = computed(() => props.score?.label || '')
const badgeBg = computed(() => LABEL_COLORS[label.value] || LABEL_COLORS.unscored)
const band = computed(() => bandFor(props.score?.fraction_ai))
const pct = computed(() => Math.round((props.score?.fraction_ai ?? 0) * 100) + '%')
</script>

<template>
  <span
    v-if="scored"
    style="display: inline-flex; align-items: center; gap: 6px; justify-content: flex-end;"
  >
    <span
      :style="{
        display: 'inline-block',
        padding: '0 6px',
        borderRadius: '3px',
        fontSize: '10.5px',
        fontWeight: 700,
        color: '#ffffff',
        background: badgeBg,
      }"
      >{{ label }}</span
    >
    <span
      :style="{
        display: 'inline-block',
        minWidth: '36px',
        textAlign: 'right',
        padding: '0 5px',
        borderRadius: '3px',
        fontSize: '11px',
        fontWeight: 700,
        background: band.bg,
        color: band.text,
        fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
      }"
      >{{ pct }}</span
    >
  </span>
  <span v-else style="color: #b3b9c0;">—</span>
</template>
