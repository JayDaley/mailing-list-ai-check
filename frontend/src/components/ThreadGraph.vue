<script setup>
// Text-free SVG reply-thread graph for one list (GET /api/lists/thread-graph).
//
// One filled circle per message, coloured like the rug plots (prediction
// bucket / too-short grey / unscored neutral), laid out left to right by IMAP
// receipt order (`seq`, the 0-based rank inside the requested window). Each
// thread — a connected reply component — occupies its own horizontal lane, with
// a straight line from every reply to its in-window parent (parent and child
// always share a lane).
//
// Under each of those lines, a reply whose implied writing rate is flagged gets
// a wider tinted underlay in the reply's own chars/minute colour — the same
// bands and colours the messages table's chars/min column uses (rateTint), so
// the two read as one scale. An unflagged or unknown rate draws no underlay.
//
// The drawing carries no text, so it scales freely: the viewBox is sized from
// the data and the SVG fills whatever container it is given, preserving aspect
// ratio. Hovering a circle shows the message's sender, subject and date in an
// HTML tooltip (teleported to <body> so no ancestor overflow clips it, and
// positioned beside the cursor); clicking emits `select` with the message id.
// Window controls and captions belong to the host: this component takes
// threads/total in and emits `select` out, nothing more.
import { computed, ref } from 'vue'

import { rateTint } from '../lib/colors'
import { fmtDate } from '../lib/format'
import { rugBarColor } from '../lib/labels'

const props = defineProps({
  threads: { type: Array, default: () => [] }, // [{messages: [...]}] oldest first
  total: { type: Number, default: 0 }, // window size (receipt ranks 0..total-1)
})
const emit = defineEmits(['select'])

// Layout constants, in viewBox units (the SVG scales them to its container).
const DX = 10 // horizontal step per receipt rank
const DY = 14 // vertical step per thread lane
const R = 3.5 // node radius
const PAD = 8 // margin around the drawing

const layout = computed(() => {
  const nodes = []
  const byId = new Map()
  props.threads.forEach((t, lane) => {
    for (const m of t.messages || []) {
      const node = {
        id: m.id,
        x: PAD + m.seq * DX,
        y: PAD + lane * DY,
        color: rugBarColor(m.prediction_short, m.extraction_status === 'too_short'),
        parentId: m.parent_id,
        // Tooltip content (three lines), resolved once here.
        from: m.from_name || m.from_email || '(unknown)',
        subject: m.subject || '(no subject)',
        date: fmtDate(m.date),
        // The rate a reply implies belongs to the edge that carries it.
        glow: rateTint(m.timing_cpm),
      }
      nodes.push(node)
      byId.set(node.id, node)
    }
  })
  // One edge per reply whose parent is in the window (keyed by the reply).
  const edges = []
  for (const n of nodes) {
    if (n.parentId == null) continue
    const p = byId.get(n.parentId)
    if (p) edges.push({ id: n.id, x1: p.x, y1: p.y, x2: n.x, y2: n.y, glow: n.glow })
  }
  const cols = Math.max(props.total, 1)
  const lanes = Math.max(props.threads.length, 1)
  return {
    nodes,
    edges,
    // Every tinted underlay is drawn before any line, so no later edge's glow
    // can paint over an earlier edge's line.
    glows: edges.filter((e) => e.glow),
    width: PAD * 2 + (cols - 1) * DX,
    height: PAD * 2 + (lanes - 1) * DY,
  }
})

// --- hover tooltip -----------------------------------------------------------
// Fixed-position box offset from the pointer so it never sits under it. The
// clamping is left to CSS clamp(): 100vw/100vh resolve in the layout engine,
// which knows the real viewport even where window.innerWidth reports 0.
const TIP_WIDTH = 260 // px, matches .tg-tip max-width
const TIP_HEIGHT = 66 // px, a three-line box (used only to clamp the bottom)
const TIP_GAP = 14 // px, pointer-to-corner offset

const tip = ref(null) // the hovered node, or null
const tipStyle = ref({})

function positionTip(event) {
  tipStyle.value = {
    left: `clamp(6px, ${Math.round(event.clientX + TIP_GAP)}px, calc(100vw - ${TIP_WIDTH + 6}px))`,
    top: `clamp(6px, ${Math.round(event.clientY + TIP_GAP)}px, calc(100vh - ${TIP_HEIGHT + 6}px))`,
  }
}
function showTip(node, event) {
  tip.value = node
  positionTip(event)
}
function moveTip(event) {
  if (tip.value) positionTip(event)
}
function hideTip() {
  tip.value = null
}
</script>

<template>
  <div class="tg-wrap" @mouseleave="hideTip">
    <svg
      class="tg-svg"
      :viewBox="`0 0 ${layout.width} ${layout.height}`"
      preserveAspectRatio="xMinYMin meet"
      role="img"
    >
      <line
        v-for="e in layout.glows"
        :key="`g${e.id}`"
        class="tg-edge-glow"
        :x1="e.x1"
        :y1="e.y1"
        :x2="e.x2"
        :y2="e.y2"
        :stroke="e.glow"
      />
      <line
        v-for="e in layout.edges"
        :key="`e${e.id}`"
        class="tg-edge"
        :x1="e.x1"
        :y1="e.y1"
        :x2="e.x2"
        :y2="e.y2"
      />
      <circle
        v-for="n in layout.nodes"
        :key="n.id"
        :cx="n.x"
        :cy="n.y"
        :r="R"
        :fill="n.color"
        class="tg-node"
        @mouseenter="showTip(n, $event)"
        @mousemove="moveTip"
        @mouseleave="hideTip"
        @click="emit('select', n.id)"
      />
    </svg>
    <Teleport to="body">
      <div v-if="tip" class="tg-tip" :style="tipStyle">
        <div class="tg-tip-from">{{ tip.from }}</div>
        <div class="tg-tip-subj">{{ tip.subject }}</div>
        <div class="tg-tip-date">{{ tip.date }}</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.tg-wrap {
  width: 100%;
  height: 100%;
}
.tg-svg {
  display: block;
  width: 100%;
  height: 100%;
}
.tg-edge {
  stroke: var(--border);
  stroke-width: 1.2;
}
.tg-edge-glow {
  stroke-width: 5;
  stroke-linecap: round;
  opacity: 0.75;
}
.tg-node {
  cursor: pointer;
}
.tg-node:hover {
  opacity: 0.75;
}

/* --- hover tooltip (teleported, above the lightbox overlay) --- */
.tg-tip {
  position: fixed;
  z-index: 400;
  max-width: 260px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.16);
  padding: 5px 7px;
  font-size: 11px;
  color: var(--text-secondary);
  pointer-events: none;
}
.tg-tip-from {
  font-weight: 600;
  color: var(--text-name);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tg-tip-subj {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tg-tip-date {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-muted);
}
</style>
