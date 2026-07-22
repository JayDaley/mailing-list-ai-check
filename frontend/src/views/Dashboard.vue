<script setup>
// The single-screen dashboard: layout owner for the three panes and the drawer.
//
// Fills the viewport below the 40px header. The messages pane takes a draggable
// share of the height (ui.topPct); below a 12px row-resize handle sit the
// lists pane (draggable width ui.leftPct) and the senders pane (flex 1),
// separated by a 12px col-resize handle. Only the pane bodies scroll.
//
// The drawer is driven by the route: `/messages/:id` opens it on that message.
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useUiStore } from '../stores/ui'
import MessagesPane from '../components/MessagesPane.vue'
import ListsPane from '../components/ListsPane.vue'
import SendersPane from '../components/SendersPane.vue'
import DetailDrawer from '../components/DetailDrawer.vue'

const ui = useUiStore()
const route = useRoute()
const router = useRouter()

// Refs to the boxes the drag math measures against.
const contentEl = ref(null)
const lowerEl = ref(null)

// Half the 12px handle, subtracted from each basis so the split lands centered.
const topBasis = computed(() => `calc(${ui.topPct}% - 6px)`)
const ctxFlex = computed(() =>
  ui.anonymous ? '1 1 auto' : `0 0 calc(${ui.leftPct}% - 6px)`,
)

// --- drag handles (pointer events) ---
function startDrag(event, axis) {
  event.preventDefault()
  const el = axis === 'row' ? contentEl.value : lowerEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  document.body.style.cursor = axis === 'row' ? 'row-resize' : 'col-resize'
  document.body.style.userSelect = 'none'

  const move = (ev) => {
    if (axis === 'row') {
      ui.setTopPct(((ev.clientY - rect.top) / rect.height) * 100)
    } else {
      ui.setLeftPct(((ev.clientX - rect.left) / rect.width) * 100)
    }
  }
  const up = () => {
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}

// --- drawer wiring ---
const drawerId = computed(() => {
  const raw = route.params.id
  if (raw == null) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
})

function openMessage(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

function closeDrawer() {
  router.push({ path: '/', query: route.query })
}
</script>

<template>
  <div class="dashboard-root">
    <div ref="contentEl" class="dashboard-content">
      <!-- Messages pane -->
      <div class="pane-slot" :style="{ flex: `0 0 ${topBasis}` }">
        <MessagesPane @open="openMessage" />
      </div>

      <!-- horizontal drag handle -->
      <div class="drag-handle-row" @pointerdown="(e) => startDrag(e, 'row')"></div>

      <!-- Lower row -->
      <div ref="lowerEl" class="dashboard-lower">
        <div class="pane-slot" :style="{ flex: ctxFlex }">
          <ListsPane />
        </div>

        <template v-if="!ui.anonymous">
          <div class="drag-handle-col" @pointerdown="(e) => startDrag(e, 'col')"></div>
          <div class="pane-slot" style="flex: 1 1 auto;">
            <SendersPane />
          </div>
        </template>
      </div>
    </div>

    <DetailDrawer v-if="drawerId" :message-id="drawerId" @close="closeDrawer" />
  </div>
</template>

<style scoped>
.dashboard-root {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.dashboard-content {
  flex: 1;
  min-height: 0;
  padding: 10px 16px 16px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
}
.dashboard-lower {
  flex: 1;
  min-height: 0;
  display: flex;
}
/* A pane slot is a flex box that clips its card so only the body scrolls. */
.pane-slot {
  display: flex;
  min-height: 0;
  min-width: 0;
}
.drag-handle-row {
  height: 12px;
  flex: none;
  cursor: row-resize;
}
.drag-handle-col {
  width: 12px;
  flex: none;
  cursor: col-resize;
}
</style>
