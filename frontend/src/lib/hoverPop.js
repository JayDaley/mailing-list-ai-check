// Shared hover-popup positioning for the dashboard's dark tooltips.
//
// The popup is teleported to <body> with fixed positioning so scroll-clipping
// panes (overflow: hidden) can't cut it off; its coordinates are computed from
// the trigger's on-screen rect on hover.
//
// It is rendered off-screen first (position: fixed, so it shrink-wraps its
// content) and measured only then: measuring while it is still in normal flow
// would report the full body width and pin it to the left edge.
//
// Usage: bind `wrapEl` to the trigger, `popEl` to the popup, call `show` on
// mouseenter and `hide` on mouseleave, and spread `popStyle` onto the popup
// along with `--arrow-left: arrowLeft`.
import { ref, nextTick } from 'vue'

export function useHoverPop() {
  const wrapEl = ref(null)
  const popEl = ref(null)
  const hover = ref(false)
  const popStyle = ref({})
  const arrowLeft = ref('12px')

  async function show() {
    hover.value = true
    popStyle.value = { position: 'fixed', left: '-9999px', top: '0px' }
    await nextTick()
    const wrap = wrapEl.value
    const pop = popEl.value
    if (!wrap || !pop) return
    const r = wrap.getBoundingClientRect()
    const gap = 6
    const margin = 8
    const pw = pop.offsetWidth
    const vw = window.innerWidth || document.documentElement.clientWidth || r.right + pw + margin
    let left = r.left
    const maxLeft = vw - pw - margin
    if (left > maxLeft) left = Math.max(margin, maxLeft)
    popStyle.value = {
      position: 'fixed',
      left: left + 'px',
      top: r.top - gap + 'px',
      transform: 'translateY(-100%)',
    }
    // Keep the arrow pointing at (roughly) the start of the trigger.
    const a = Math.min(Math.max(r.left - left + 12, 8), pw - 8)
    arrowLeft.value = a + 'px'
  }

  function hide() {
    hover.value = false
  }

  return { wrapEl, popEl, hover, popStyle, arrowLeft, show, hide }
}
