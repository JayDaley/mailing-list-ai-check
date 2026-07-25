<script setup>
// The documentation drawer, opened by the header's ⓘ button and sliding in from
// the left of the screen.
//
// Two columns: an index of the available Markdown files (GET /api/docs) and a
// viewer that renders the selected one (GET /api/docs/<path>) with `marked`.
// The API decides which files are on offer — README.md, CHANGELOG.md and the
// top level of docs/ — so this component never builds a path of its own.
//
// Links inside a rendered document are rewritten after each render: an http(s)
// or mailto link opens in a new tab; a relative link that resolves to another
// document in the index switches the viewer to it; anything else (a source file
// or a docs/ sub-directory page, neither of which the API serves) is shown as
// plain text so it cannot navigate away from the dashboard.
//
// Contract: props { open: Boolean }, emits ['close'].
import { ref, computed, watch, nextTick, onUnmounted } from 'vue'
import { marked } from 'marked'

import { get } from '../api'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

// The overlay and panel are mounted from the first open onwards and shown/hidden
// by an `is-open` class, so the slide is a plain CSS transition on elements that
// stay in the DOM. (A <Transition> around a v-if would tie removal to the
// transition finishing, which a backgrounded tab can stall indefinitely.)
const everOpened = ref(false)

// --- index ------------------------------------------------------------------
const docs = ref([])
const indexError = ref('')
const indexLoaded = ref(false)

async function loadIndex() {
  if (indexLoaded.value) return
  try {
    const data = await get('/docs')
    docs.value = data?.docs || []
    indexLoaded.value = true
    if (docs.value.length) select(docs.value[0].path)
  } catch (err) {
    indexError.value = err.message || 'could not load the documentation index'
  }
}

// The label shown in the index column: the file name, which identifies the file
// on disk more usefully here than its heading does.
function indexLabel(doc) {
  return doc.path
}

// --- selected document ------------------------------------------------------
const currentPath = ref('')
const markdown = ref('')
const docError = ref('')
const loadingDoc = ref(false)

const rendered = computed(() => (markdown.value ? marked.parse(markdown.value, { gfm: true }) : ''))

const body = ref(null) // the scrolling viewer column

async function select(path) {
  if (path === currentPath.value) return
  currentPath.value = path
  markdown.value = ''
  docError.value = ''
  loadingDoc.value = true
  try {
    const data = await get(`/docs/${path}`)
    // A slower earlier request must not overwrite a newer selection.
    if (currentPath.value !== path) return
    markdown.value = data?.markdown || ''
  } catch (err) {
    if (currentPath.value !== path) return
    docError.value = err.message || 'could not load the document'
  } finally {
    if (currentPath.value === path) loadingDoc.value = false
  }
}

// Scroll back to the top on every switch, and re-point the rendered links.
watch(rendered, async () => {
  await nextTick()
  if (body.value) body.value.scrollTop = 0
  rewriteLinks()
})

// --- link handling ----------------------------------------------------------

// Resolve an href written inside `fromPath` to a repo-root-relative path, so it
// can be matched against the index. Returns null for anything that is not a
// plain relative path (absolute URLs, in-page anchors, root-absolute paths).
function resolveDocPath(fromPath, href) {
  if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('#') || href.startsWith('/')) {
    return null
  }
  const base = fromPath.split('/').slice(0, -1)
  const parts = href.split('#')[0].split('?')[0].split('/')
  for (const part of parts) {
    if (part === '' || part === '.') continue
    if (part === '..') base.pop()
    else base.push(part)
  }
  return base.join('/')
}

function rewriteLinks() {
  const root = body.value
  if (!root) return
  const known = new Set(docs.value.map((d) => d.path))
  for (const a of root.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href')
    const target = resolveDocPath(currentPath.value, href)
    if (target && known.has(target)) {
      a.dataset.doc = target
      a.removeAttribute('target')
    } else if (target !== null || href.startsWith('#') || href.startsWith('/')) {
      // A repo path the API does not serve, or an anchor this viewer has no ids
      // for: keep the text, drop the link.
      delete a.dataset.doc
      a.classList.add('doc-plain')
    } else {
      a.target = '_blank'
      a.rel = 'noopener noreferrer'
    }
  }
}

// One delegated handler for the rendered document's links.
function onBodyClick(event) {
  const a = event.target.closest?.('a')
  if (!a || !body.value?.contains(a)) return
  if (a.dataset.doc) {
    event.preventDefault()
    select(a.dataset.doc)
  } else if (a.classList.contains('doc-plain')) {
    event.preventDefault()
  }
}

// --- open / close -----------------------------------------------------------
function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      everOpened.value = true
      document.addEventListener('keydown', onKeydown)
      loadIndex()
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
  { immediate: true },
)
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="everOpened"
      class="docs-overlay"
      :class="{ 'is-open': open }"
      @click="emit('close')"
    ></div>
    <aside
      v-if="everOpened"
      class="docs-panel"
      :class="{ 'is-open': open }"
      role="dialog"
      aria-modal="true"
      aria-label="Documentation"
      :aria-hidden="open ? 'false' : 'true'"
    >
      <div class="docs-topbar">
        <span class="docs-title">Documentation</span>
        <span class="docs-spacer"></span>
        <button type="button" class="drawer-close-btn" @click="emit('close')">Close</button>
      </div>

      <div class="docs-cols">
        <nav class="docs-index">
          <p v-if="indexError" class="docs-msg docs-msg-error">{{ indexError }}</p>
          <p v-else-if="!docs.length" class="docs-msg">no documentation files found</p>
          <button
            v-for="d in docs"
            :key="d.path"
            type="button"
            class="docs-index-item"
            :class="{ 'is-current': d.path === currentPath }"
            :title="d.title"
            @click="select(d.path)"
          >
            <span class="docs-index-name mono">{{ indexLabel(d) }}</span>
          </button>
        </nav>

        <div ref="body" class="docs-view" @click="onBodyClick">
          <p v-if="docError" class="docs-msg docs-msg-error">{{ docError }}</p>
          <p v-else-if="loadingDoc" class="docs-msg">loading…</p>
          <!-- Local repository documentation, read from disk by the API. -->
          <article v-else class="docs-md" v-html="rendered"></article>
        </div>
      </div>
    </aside>
  </Teleport>
</template>

<style scoped>
/* Closed state is the default; `is-open` slides the panel in from the left and
   fades the overlay up. `visibility` is transitioned too so a closed drawer
   takes no clicks and casts no shadow, without unmounting it. */
.docs-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 18, 22, 0.35);
  z-index: 40;
  opacity: 0;
  visibility: hidden;
  /* not transitioned: a closing overlay must stop taking clicks immediately */
  pointer-events: none;
  transition:
    opacity 0.18s ease-out,
    visibility 0.18s;
}
.docs-overlay.is-open {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
}
.docs-panel {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: min(1040px, 94vw);
  background: var(--surface);
  box-shadow: 8px 0 32px rgba(15, 18, 22, 0.18);
  z-index: 41;
  display: flex;
  flex-direction: column;
  transform: translateX(-100%);
  visibility: hidden;
  pointer-events: none;
  transition:
    transform 0.18s ease-out,
    visibility 0.18s;
}
.docs-panel.is-open {
  transform: none;
  visibility: visible;
  pointer-events: auto;
}

.docs-topbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
  flex: none;
}
.docs-title {
  font-weight: 700;
  font-size: 12px;
}
.docs-spacer {
  flex: 1;
}

.docs-cols {
  flex: 1;
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  min-height: 0;
}
.docs-index {
  border-right: 1px solid var(--border);
  background: var(--toolbar);
  overflow-y: auto;
  padding: 6px 0;
  display: flex;
  flex-direction: column;
}
.docs-index-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 5px 12px;
  border: 0;
  background: none;
  cursor: pointer;
  color: var(--text-name);
  font-size: 12px;
}
.docs-index-item:hover {
  background: var(--hover-row);
  color: var(--accent);
}
.docs-index-item.is-current {
  background: var(--surface);
  color: var(--text);
  font-weight: 600;
  box-shadow: inset 2px 0 0 var(--accent);
}
.docs-index-name {
  font-size: 11px;
}

.docs-view {
  overflow-y: auto;
  padding: 16px 24px 40px;
  min-width: 0;
}
.docs-msg {
  padding: 18px 4px;
  color: var(--text-muted);
}
.docs-msg-error {
  color: var(--danger);
}

/* --- rendered Markdown (v-html, hence :deep) --- */
.docs-md {
  max-width: 76ch;
  font-size: 13px;
  line-height: 1.62;
}
.docs-md :deep(h1),
.docs-md :deep(h2),
.docs-md :deep(h3),
.docs-md :deep(h4) {
  line-height: 1.3;
  margin: 1.5em 0 0.5em;
}
.docs-md :deep(h1) {
  font-size: 19px;
  margin-top: 0;
}
.docs-md :deep(h2) {
  font-size: 15px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
}
.docs-md :deep(h3) {
  font-size: 13px;
}
.docs-md :deep(h4) {
  font-size: 12px;
  color: var(--text-secondary);
}
.docs-md :deep(p),
.docs-md :deep(ul),
.docs-md :deep(ol),
.docs-md :deep(blockquote) {
  margin: 0.7em 0;
}
.docs-md :deep(ul),
.docs-md :deep(ol) {
  padding-left: 1.4em;
}
.docs-md :deep(li) {
  margin: 0.2em 0;
}
.docs-md :deep(blockquote) {
  border-left: 3px solid var(--border);
  padding-left: 12px;
  color: var(--text-secondary);
}
.docs-md :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.6em 0;
}
.docs-md :deep(code) {
  font-family: var(--mono);
  font-size: 11.5px;
  background: var(--tile);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 0.5px 4px;
}
.docs-md :deep(pre) {
  background: var(--tile);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: 0.8em 0;
}
.docs-md :deep(pre code) {
  background: none;
  border: 0;
  padding: 0;
  font-size: 11.5px;
  line-height: 1.5;
}
.docs-md :deep(table) {
  border-collapse: collapse;
  margin: 0.9em 0;
  font-size: 12px;
  display: block;
  overflow-x: auto;
  max-width: 100%;
}
.docs-md :deep(th),
.docs-md :deep(td) {
  border: 1px solid var(--border);
  padding: 3px 8px;
  text-align: left;
  vertical-align: top;
}
.docs-md :deep(th) {
  background: var(--tile);
  font-weight: 600;
}
.docs-md :deep(img) {
  max-width: 100%;
}
/* a repo path the API does not serve — rendered as text, not a link */
.docs-md :deep(a.doc-plain) {
  color: inherit;
  text-decoration: none;
  cursor: default;
}
</style>
