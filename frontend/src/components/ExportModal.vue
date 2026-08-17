<script setup>
// The export dialog: choose the export format, which lists to export and,
// optionally, a range of message dates. The parent (MessagesPane) owns the
// download itself; this component only collects the selection and emits it.
//
// Two formats are offered. The full export is the re-importable archive of
// whole messages; the stats export is a CSV bundle of scores and metadata for
// analysis elsewhere, with an option to leave sender identities and
// Message-IDs out of it. List and date selection are shared between them.
//
// Selecting no list means every list, which is what the API's absent `list`
// param means — the "All lists" row is therefore a shortcut that clears the
// per-list boxes rather than a fourth kind of selection.
//
// Props:
//   - open    {boolean}  render the overlay + modal when true
//   - lists   {Array}    [{name, message_count}] — the selectable lists
//   - preset  {Array}    list names to tick when the dialog opens
//   - busy    {boolean}  a download is in flight → controls disabled
// Emits:
//   - close                                   Cancel, Escape or a backdrop click
//   - export {format, lists, date_from, date_to}   the Export button;
//     `format` is 'full' or 'stats'
import { ref, computed, watch, onUnmounted } from 'vue'

import { fmtInt } from '../lib/format'

const props = defineProps({
  open: { type: Boolean, default: false },
  lists: { type: Array, default: () => [] },
  preset: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'export'])

const format = ref('full')
const selected = ref([])
const search = ref('')
const dateFrom = ref('')
const dateTo = ref('')

// Each opening starts from the pane's current list filter, the full format and
// an empty range, so the dialog never carries a stale selection into a later
// export.
watch(
  () => props.open,
  (open) => {
    if (open) {
      selected.value = props.preset.filter((n) => props.lists.some((l) => l.name === n))
      format.value = 'full'
      search.value = ''
      dateFrom.value = ''
      dateTo.value = ''
      document.addEventListener('keydown', onKeydown)
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
)
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

const allLists = computed(() => selected.value.length === 0)

const isStats = computed(() => format.value === 'stats')

// The intro describes the format in hand: the two produce different artifacts
// with different contents, and the dates apply to both.
const intro = computed(() =>
  isStats.value
    ? 'Exports scores and message metadata as CSV, without any message text. Leave the dates empty to export every message.'
    : 'Exports whole messages and their pipeline state, not the filtered view. Leave the dates empty to export every message.',
)

// There are hundreds of lists to scroll, so the picker is searched by substring
// (the Lists pane's box does the same). A search must never hide what is about
// to be exported, so while one is active the ticked lists are pulled to the top
// whether they match it or not; with no search the natural order is kept, so
// ticking a box does not make the row jump away from the pointer.
const shownLists = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return props.lists
  const ticked = props.lists.filter((l) => selected.value.includes(l.name))
  const rest = props.lists.filter(
    (l) => !selected.value.includes(l.name) && l.name.toLowerCase().includes(needle),
  )
  return [...ticked, ...rest]
})

function toggle(name) {
  const at = selected.value.indexOf(name)
  if (at === -1) selected.value.push(name)
  else selected.value.splice(at, 1)
}
function selectAll() {
  selected.value = []
}

// A range only makes sense read forwards; the ends are otherwise free-form, so
// this is the one check the dialog can make without asking the server.
const rangeInverted = computed(
  () => dateFrom.value !== '' && dateTo.value !== '' && dateFrom.value > dateTo.value,
)

const summary = computed(() => {
  const kind = isStats.value ? 'Stats' : 'Full'
  const scope = allLists.value
    ? 'All lists'
    : selected.value.length === 1
      ? `1 list`
      : `${selected.value.length} lists`
  if (!dateFrom.value && !dateTo.value) return `${kind} — ${scope}, all dates`
  if (dateFrom.value && dateTo.value)
    return `${kind} — ${scope}, ${dateFrom.value} to ${dateTo.value}`
  return dateFrom.value
    ? `${kind} — ${scope}, from ${dateFrom.value}`
    : `${kind} — ${scope}, up to ${dateTo.value}`
})

function requestClose() {
  if (props.busy) return
  emit('close')
}
function onKeydown(e) {
  if (e.key === 'Escape') requestClose()
}

function submit() {
  if (props.busy || rangeInverted.value) return
  emit('export', {
    format: format.value,
    lists: [...selected.value],
    date_from: dateFrom.value,
    date_to: dateTo.value,
  })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="ex-overlay" @click.self="requestClose">
      <div class="ex-modal" role="dialog" aria-modal="true" aria-label="Export">
        <div class="ex-title">Export</div>
        <div class="ex-intro">{{ intro }}</div>

        <div class="ex-section-label">Format</div>
        <div class="ex-formats">
          <label class="ex-format">
            <input v-model="format" type="radio" value="full" :disabled="busy" />
            <span class="ex-format-name">Full</span>
            <span class="ex-format-ext">.jsonl.zst</span>
          </label>
          <label class="ex-format">
            <input v-model="format" type="radio" value="stats" :disabled="busy" />
            <span class="ex-format-name">Stats</span>
            <span class="ex-format-ext">.zip</span>
          </label>
        </div>

        <div class="ex-section-head">
          <span class="ex-section-label">Lists</span>
          <input
            v-model="search"
            type="search"
            class="ex-search"
            placeholder="search lists…"
            :disabled="busy"
          />
        </div>
        <div class="ex-lists">
          <label class="ex-row ex-row-all">
            <input type="radio" :checked="allLists" :disabled="busy" @change="selectAll" />
            <span class="ex-name">All lists</span>
          </label>
          <label v-for="l in shownLists" :key="l.name" class="ex-row">
            <input
              type="checkbox"
              :checked="selected.includes(l.name)"
              :disabled="busy"
              @change="toggle(l.name)"
            />
            <span class="ex-name">{{ l.name }}</span>
            <span class="ex-count">{{ fmtInt(l.message_count || 0) }}</span>
          </label>
          <div v-if="!lists.length" class="ex-empty">no lists with messages</div>
          <div v-else-if="!shownLists.length" class="ex-empty">no matching lists</div>
        </div>

        <div class="ex-section-label">Message dates</div>
        <div class="ex-dates">
          <label class="ex-date">
            <span>From</span>
            <input v-model="dateFrom" type="date" :disabled="busy" />
          </label>
          <label class="ex-date">
            <span>To</span>
            <input v-model="dateTo" type="date" :disabled="busy" />
          </label>
        </div>
        <!-- A bare 'to' date compares against stored timestamps, so the day
             itself falls outside the range. Say so rather than surprise. -->
        <div v-if="dateTo" class="ex-note">
          The 'to' date is exclusive of that day's messages, which carry a time.
        </div>
        <div v-if="rangeInverted" class="ex-error">The 'from' date is after the 'to' date.</div>

        <div class="ex-footer">
          <span class="ex-summary">{{ summary }}</span>
          <button type="button" class="ex-btn" :disabled="busy" @click="requestClose">
            Cancel
          </button>
          <button
            type="button"
            class="ex-btn ex-btn-primary"
            :disabled="busy || rangeInverted"
            @click="submit"
          >
            {{ busy ? 'exporting…' : 'Export' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ex-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.ex-modal {
  width: 420px;
  max-width: calc(100vw - 32px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 14px 16px;
  color: var(--text-secondary);
  font-size: 11.5px;
}
.ex-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
  margin-bottom: 6px;
}
.ex-intro {
  font-size: 10.5px;
  color: var(--text-muted);
  margin-bottom: 12px;
}
.ex-section-label {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin-bottom: 5px;
}
.ex-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 12px;
}
.ex-formats {
  display: flex;
  gap: 18px;
}
.ex-format {
  display: flex;
  align-items: center;
  gap: 7px;
  cursor: pointer;
}
.ex-format-name {
  color: var(--text-name);
  font-weight: 600;
}
.ex-format-ext {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-muted);
}
.ex-search {
  width: 150px;
  font-family: var(--font);
  font-size: 11px;
  padding: 2px 6px;
  margin-bottom: 5px;
  border: 1px solid var(--border-input);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text);
}
.ex-lists {
  max-height: 190px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 3px;
  margin-bottom: 12px;
}
.ex-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 8px;
  cursor: pointer;
}
.ex-row:hover {
  background: var(--hover-row);
}
.ex-row-all {
  border-bottom: 1px solid var(--border-row);
}
.ex-name {
  font-family: var(--mono);
  color: var(--text-name);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ex-count {
  font-size: 10px;
  color: var(--text-muted);
}
.ex-empty {
  padding: 6px 8px;
  color: var(--text-muted);
}
.ex-dates {
  display: flex;
  gap: 12px;
}
.ex-date {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ex-date input {
  font-family: var(--font);
  font-size: 11px;
  padding: 2px 4px;
  border: 1px solid var(--border-input);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text);
}
.ex-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 6px;
}
.ex-error {
  font-size: 10.5px;
  color: var(--danger);
  margin-top: 6px;
}
.ex-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
}
.ex-summary {
  flex: 1;
  min-width: 0;
  font-size: 10px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ex-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
}
.ex-btn-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.ex-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
