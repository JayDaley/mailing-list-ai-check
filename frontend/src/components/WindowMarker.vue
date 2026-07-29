<script setup>
// A Pangram window's number in a small box. The same box appears in three
// places: the analysis table (where it links to the window in the text), inline
// at the point where the window starts, and beside the window's bracket in the
// right-hand wire gutter.
//
// The box is Observable 10 grey and lights up in the palette's light blue while
// the window is active — hovering any of the three highlights all of them, and
// the window's bracket with them.
//
// Hovering also shows the window's row from the analysis table, one field per
// line.
import { computed } from 'vue'

import { useHoverPop } from '../lib/hoverPop'
import { fmtInt } from '../lib/format'
import { OBSERVABLE_10 } from '../lib/labels'

const props = defineProps({
  // A window from the message detail's score.windows.
  win: { type: Object, required: true },
  variant: { type: String, default: 'box' },
  // DOM id, so the analysis table can scroll a window into view.
  markerId: { type: String, default: null },
  // True while this window is hovered (anywhere it appears).
  active: { type: Boolean, default: false },
  // Render as a link that jumps to the window's inline marker.
  clickable: { type: Boolean, default: false },
})

const emit = defineEmits(['activate', 'deactivate', 'jump'])

const { wrapEl, popEl, hover, popStyle, arrowLeft, show, hide } = useHoverPop()

const boxStyle = computed(() => ({
  background: props.active ? OBSERVABLE_10.lightBlue : OBSERVABLE_10.grey,
  color: props.active ? '#1c2024' : '#ffffff',
  cursor: props.clickable ? 'pointer' : 'default',
}))

const lines = computed(() => {
  const w = props.win
  const s = Number(w.ai_assistance_score)
  const score = Number.isFinite(s) ? s.toFixed(2) : '—'
  const out = [`Window ${w.index}`, `${score} (${w.confidence || '—'})`]
  if (w.label) out.push(w.label)
  // Detector v4 only: a window scored under v3 carries no humanizer verdict,
  // and one that is not humanized adds no line.
  if (w.is_humanized) {
    const h = Number(w.humanizer_score)
    out.push(`humanized (${Number.isFinite(h) ? h.toFixed(2) : '—'})`)
  }
  const size = []
  if (w.chars != null) size.push(`${fmtInt(w.chars)} chars`)
  if (w.word_count != null) size.push(`${fmtInt(w.word_count)} words`)
  if (size.length) out.push(size.join(' · '))
  return out
})

function onEnter() {
  emit('activate', props.win.index)
  show()
}
function onLeave() {
  emit('deactivate', props.win.index)
  hide()
}
</script>

<template>
  <!-- Single root: a fragment would drop the class the parent passes in. The
       teleport renders nothing here, so it does not affect the box. -->
  <span
    :id="markerId"
    ref="wrapEl"
    class="win-num"
    :class="`win-num-${variant}`"
    :style="boxStyle"
    :role="clickable ? 'link' : null"
    :tabindex="clickable ? 0 : null"
    :title="clickable ? 'Show this window in the text' : null"
    @mouseenter="onEnter"
    @mouseleave="onLeave"
    @click="clickable && emit('jump', win)"
    @keydown.enter="clickable && emit('jump', win)"
    >{{ win.index
    }}<Teleport to="body"
      ><span
        v-if="hover"
        ref="popEl"
        class="hover-pop hover-pop-stack"
        role="tooltip"
        :style="{ ...popStyle, '--arrow-left': arrowLeft }"
      >
        <span v-for="(l, i) in lines" :key="i" :class="{ 'hover-pop-head': i === 0 }">{{
          l
        }}</span>
      </span></Teleport
    ></span
  >
</template>

<style scoped>
.win-num {
  display: inline-block;
  min-width: 15px;
  padding: 0 3px;
  border-radius: 2px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 9.5px;
  font-weight: 700;
  line-height: 14px;
  text-align: center;
  user-select: none;
}
/* Inline in the text, at the window's first character. */
.win-num-box {
  margin-right: 3px;
  vertical-align: 1px;
}
/* Beside the window's bracket in the wire gutter. */
.win-num-wire {
  position: absolute;
  left: 7px;
  top: 0;
}
</style>
