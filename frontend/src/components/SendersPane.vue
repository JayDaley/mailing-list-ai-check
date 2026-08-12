<script setup>
// The right / lower "Senders" pane. Two modes driven by the shared filters:
//
//   1. Senders table (default): one row per person (a linked address group) or
//      per unlinked address, searchable and sortable, with infinite scroll and
//      the ⇄ link-management popover. A `list` filter narrows the table to
//      senders who posted to that list (counts and mix bars scoped to it).
//   2. Sender detail (a `person`/`address` filter): the sender's aggregates
//      across all lists from GET /api/summary — posts, detection-mix summary,
//      and per-list activity with a detection bar and two reply rugs per list.
//
// Hidden entirely in anonymous mode (Dashboard v-if's it away).
//
// Data:   GET /api/senders?q&sort&order&page&list  (paged, appended on scroll)
//         GET /api/persons                    (the "add to existing sender" list)
//         GET /api/persons/suggestions        (same-name unlinked-address groups)
//         GET /api/summary?person|address     (the sender detail card)
//         GET /api/senders/reply-rugs?person|address  (the two reply rugs)
// Mutations: POST/PUT/DELETE /api/persons     (link / rename / detach / unlink)
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get, postJson, putJson, del } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { rugBarColor } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import MixBar from './MixBar.vue'
import MixSummary from './MixSummary.vue'

const filters = useFiltersStore()
const route = useRoute()
const router = useRouter()

// --- fetch state ------------------------------------------------------------
const senders = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)

const q = ref('') // committed search term
const sort = ref('count')
const order = ref('desc')

const persons = ref([]) // [{id, canonical_name, ...}]
const suggestions = ref([]) // [{display_name, address_ids, emails}]

let fetchToken = 0
async function fetchPage(pageNo, append) {
  const token = ++fetchToken
  loading.value = true
  try {
    const params = {
      q: q.value || undefined,
      sort: sort.value,
      order: order.value,
      page: pageNo,
      per_page: 60,
      list: filters.list || undefined,
    }
    const data = await get('/senders', params)
    if (token !== fetchToken) return
    senders.value = append ? senders.value.concat(data.senders || []) : data.senders || []
    total.value = data.total || 0
    page.value = pageNo
  } catch {
    if (token === fetchToken && !append) {
      senders.value = []
      total.value = 0
    }
  } finally {
    if (token === fetchToken) loading.value = false
  }
}

function refresh() {
  fetchPage(1, false)
}
function loadMore() {
  if (loading.value || senders.value.length >= total.value) return
  fetchPage(page.value + 1, true)
}

async function loadPersons() {
  try {
    persons.value = (await get('/persons'))?.persons || []
  } catch {
    persons.value = []
  }
}
async function loadSuggestions() {
  try {
    suggestions.value = (await get('/persons/suggestions'))?.suggestions || []
  } catch {
    suggestions.value = []
  }
}

// A list filter narrows the table to that list's senders.
watch(
  () => filters.list,
  () => refresh(),
)

onMounted(() => {
  refresh()
  loadPersons()
  loadSuggestions()
  loadDetail()
  loadReplyRugs()
})

// --- sender detail mode -------------------------------------------------------
const detailMode = computed(() => !!(filters.person || filters.address))

// The detail aggregates over the sender alone (across all lists), regardless of
// the other filters active on the messages table. A linked address rolls up to
// its owning person.
const detailParams = computed(() => {
  if (!detailMode.value) return null
  if (filters.person) return { person: filters.person }
  const owner = persons.value.find((p) =>
    (p.addresses || []).some((a) => a.email === filters.address),
  )
  return owner ? { person: String(owner.id) } : { address: filters.address }
})

const detailSummary = ref(null)
const detailLoading = ref(false)
const detailError = ref(null)
let detailToken = 0
async function loadDetail() {
  const params = detailParams.value
  if (!params) return
  const token = ++detailToken
  detailLoading.value = true
  detailError.value = null
  try {
    const data = await get('/summary', params)
    if (token === detailToken) detailSummary.value = data
  } catch (err) {
    if (token === detailToken) {
      detailSummary.value = null
      detailError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    if (token === detailToken) detailLoading.value = false
  }
}

// --- reply rugs (per list, last 50 messages each way) ------------------------
// GET /api/senders/reply-rugs returns one entry per list the sender posted to:
// `replied_to` (the messages this sender's replies on that list point at) and
// `reply_from` (other senders' replies to this sender's messages there). Both
// arrive newest-first, like GET /messages; the rugs read oldest → newest, so
// each is reversed here — the same handling as the per-list rug in ListsPane.
const replyRugs = ref({}) // list name -> {repliedTo: [bar], replyFrom: [bar]}
let rugToken = 0

// One bar per message, coloured by its prediction bucket — or grey when the
// extraction was gated under the reliability floor (extraction_status
// `too_short`), which the tooltip names where the bucket would otherwise sit.
function rugBar(m) {
  const pred = m.prediction_short || m.label || null
  const tooShort = m.extraction_status === 'too_short'
  return {
    id: m.id,
    color: rugBarColor(pred, tooShort),
    title:
      `${fmtDate(m.date)} · ${tooShort ? 'Too short' : pred || 'unscored'} — ` +
      `${m.subject || '(no subject)'}`,
  }
}

async function loadReplyRugs() {
  const params = detailParams.value
  if (!params) return
  const token = ++rugToken
  try {
    const data = await get('/senders/reply-rugs', params)
    if (token !== rugToken) return
    const byList = {}
    for (const entry of data?.by_list || []) {
      byList[entry.list] = {
        repliedTo: (entry.replied_to || []).slice().reverse().map(rugBar),
        replyFrom: (entry.reply_from || []).slice().reverse().map(rugBar),
      }
    }
    replyRugs.value = byList
  } catch {
    if (token === rugToken) replyRugs.value = {}
  }
}

function openRugMessage(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

// Refetch whenever the driving filter (or the address→person resolution, once
// /api/persons arrives) changes.
const detailKey = computed(() => JSON.stringify(detailParams.value))
watch(detailKey, () => {
  loadDetail()
  loadReplyRugs()
})

const detailCard = computed(() => {
  const s = detailSummary.value
  if (!s) return null
  let name = ''
  let emails = []
  if (filters.person) {
    const p = persons.value.find((x) => String(x.id) === String(filters.person))
    name = p ? p.canonical_name : '?'
    emails = p ? (p.addresses || []).map((a) => a.email) : []
  } else {
    // Address filter: a linked address shows its owning person; otherwise the
    // address's display name — from the loaded senders table if it's there,
    // else from the summary's by_address rows — falling back to the address.
    const owner = persons.value.find((p) =>
      (p.addresses || []).some((a) => a.email === filters.address),
    )
    if (owner) {
      name = owner.canonical_name
      emails = (owner.addresses || []).map((a) => a.email)
    } else {
      const row = senders.value.find(
        (e) => e.type === 'address' && (e.emails || []).includes(filters.address),
      )
      const addr = (s.by_address || []).find((a) => a.email === filters.address)
      name = row?.name || addr?.display_name || filters.address
      emails = [filters.address]
    }
  }
  return {
    name,
    emails,
    total: fmtInt(s.total),
    mix: s.label_distribution || {},
    tooShort: s.too_short || 0,
    byList: (s.by_list || []).map((r) => ({
      list: r.list,
      count: fmtInt(r.count),
      counts: r.label_counts || {},
      // Gated under the reliability floor: the mix bar's trailing grey segment.
      tooShort: r.too_short_count || 0,
      // The rugs load separately, so a row may render before (or without) them.
      repliedTo: replyRugs.value[r.list]?.repliedTo || [],
      replyFrom: replyRugs.value[r.list]?.replyFrom || [],
      click: () => filters.setFilter('list', r.list),
    })),
  }
})

function closeDetail() {
  filters.patch({ person: '', address: '' })
}

const paneSub = computed(() =>
  detailMode.value
    ? 'sender across all lists'
    : filters.list
      ? `senders on ${filters.list} · manage address links with ⇄`
      : 'by emails sent · manage address links with ⇄',
)

// --- search (debounced) -----------------------------------------------------
const qLocal = ref('')
let qTimer = null
function setQ(e) {
  qLocal.value = e.target.value
  clearTimeout(qTimer)
  const val = e.target.value
  qTimer = setTimeout(() => {
    q.value = val.trim()
    refresh() // resets to page 1
  }, 250)
}

// --- sorting ----------------------------------------------------------------
function sortName() {
  if (sort.value === 'name') order.value = order.value === 'asc' ? 'desc' : 'asc'
  else {
    sort.value = 'name'
    order.value = 'asc'
  }
  refresh()
}
function sortCount() {
  if (sort.value === 'count') order.value = order.value === 'desc' ? 'asc' : 'desc'
  else {
    sort.value = 'count'
    order.value = 'desc'
  }
  refresh()
}
// By the AI share of the sender's mix (the API's `ai` sort), highest first.
function sortAi() {
  if (sort.value === 'ai') order.value = order.value === 'desc' ? 'asc' : 'desc'
  else {
    sort.value = 'ai'
    order.value = 'desc'
  }
  refresh()
}
const nameInd = computed(() => (sort.value === 'name' ? (order.value === 'asc' ? ' ▲' : ' ▼') : ''))
const countInd = computed(() => (sort.value === 'count' ? (order.value === 'asc' ? ' ▲' : ' ▼') : ''))
const aiInd = computed(() => (sort.value === 'ai' ? (order.value === 'asc' ? ' ▲' : ' ▼') : ''))

// --- popover ----------------------------------------------------------------
const openPop = ref(null) // row key ('p<id>' | 'a<id>') of the open popover
const assignId = ref('') // selected person id in the "add to existing" select

function keyFor(e) {
  return e.type === 'person' ? 'p' + e.person_id : 'a' + e.address_id
}
function togglePop(key) {
  openPop.value = openPop.value === key ? null : key
  assignId.value = ''
}
function closePop() {
  openPop.value = null
  assignId.value = ''
}

// --- rows -------------------------------------------------------------------
const rows = computed(() => {
  const list = senders.value
  return list.map((e, i) => {
    const key = keyFor(e)
    const isPerson = e.type === 'person'
    // Flip the popover above for rows near the end of the loaded set.
    const up = i > list.length - 5 && list.length > 8
    let sib = null
    if (!isPerson) {
      const sug = suggestions.value.find((s) => (s.address_ids || []).includes(e.address_id))
      if (sug) {
        const others = (sug.emails || []).filter((em) => !e.emails.includes(em))
        sib = {
          count: sug.address_ids.length - 1,
          allIds: sug.address_ids,
          emails: others.join(', '),
        }
      }
    }
    return {
      key,
      entry: e,
      isPerson,
      name: e.name,
      personId: e.person_id,
      addressId: e.address_id,
      linkedNote: isPerson ? '⇄ ' + e.emails.length : '',
      emails: e.emails.join(', '),
      count: fmtInt(e.message_count),
      counts: e.label_counts || {},
      tooShort: e.too_short_count || 0,
      linkColor: isPerson ? '#2f6feb' : '#8a929b',
      linkTitle: isPerson ? 'Linked sender — manage addresses' : 'Link this address to a sender',
      popTop: up ? 'auto' : '22px',
      popBottom: up ? '22px' : 'auto',
      chips: isPerson
        ? e.emails.map((email, idx) => ({ email, id: e.address_ids[idx] }))
        : [],
      sib,
    }
  })
})

const empty = computed(() => !loading.value && total.value === 0)
const loadedNote = computed(
  () =>
    `${fmtInt(senders.value.length)} of ${fmtInt(total.value)} loaded` +
    (senders.value.length < total.value ? ' · scroll for more' : ''),
)

function onScroll(e) {
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 240) loadMore()
}

// --- select / filter --------------------------------------------------------
function showSender(row) {
  if (row.isPerson) filters.setFilter('person', String(row.personId))
  else filters.setFilter('address', row.entry.emails[0])
}

// --- mutations --------------------------------------------------------------
async function afterMutation(closePopover) {
  await Promise.all([loadPersons(), loadSuggestions()])
  await fetchPage(1, false) // reset to page 1, preserving q/sort/order
  if (closePopover) closePop()
}

async function renamePerson(row, ev) {
  const v = ev.target.value.trim()
  if (v && v !== row.name) {
    try {
      await putJson('/persons/' + row.personId, { canonical_name: v })
      await afterMutation(false)
    } catch {
      ev.target.value = row.name
    }
  } else {
    ev.target.value = row.name
  }
}

async function detachAddress(row, addressId) {
  await putJson('/persons/' + row.personId, { remove_address_ids: [addressId] })
  await afterMutation(false)
}

async function unlinkPerson(row) {
  if (!confirm('Unlink this sender? Its addresses become independent again.')) return
  await del('/persons/' + row.personId)
  if (String(filters.person) === String(row.personId)) filters.setFilter('person', '')
  await afterMutation(true)
}

async function linkSiblings(row) {
  await postJson('/persons', { canonical_name: row.name, address_ids: row.sib.allIds })
  await afterMutation(true)
}

async function newSender(row) {
  await postJson('/persons', { canonical_name: row.name, address_ids: [row.addressId] })
  await afterMutation(true)
}

async function assignToExisting(row) {
  if (!assignId.value) return
  await putJson('/persons/' + assignId.value, { add_address_ids: [row.addressId] })
  await afterMutation(true)
}
</script>

<template>
  <div class="card">
    <div class="pane-header">
      <span class="pane-title">Senders</span>
      <span class="pane-subtitle">{{ paneSub }}</span>
      <span style="flex: 1;"></span>
      <input
        v-if="!detailMode"
        type="search"
        placeholder="search senders…"
        :value="qLocal"
        class="senders-search"
        @input="setQ"
      />
    </div>

    <!-- sender detail -->
    <div v-if="detailMode" class="pane-body senders-detail">
      <div v-if="detailLoading && !detailCard" class="detail-status">loading…</div>
      <div v-else-if="detailError" class="detail-status detail-error">{{ detailError }}</div>
      <template v-else-if="detailCard">
        <div class="detail-head">
          <div class="detail-name">{{ detailCard.name }}</div>
          <button class="close-x" title="Clear selection" @click="closeDetail">×</button>
        </div>
        <div v-for="e in detailCard.emails" :key="e" class="detail-email mono">{{ e }}</div>
        <div class="detail-stats">
          <div class="tile">
            <div class="tile-val">{{ detailCard.total }}</div>
            <div class="tile-cap">Posts</div>
          </div>
          <MixSummary
            :counts="detailCard.mix"
            :too-short="detailCard.tooShort"
            :clickable="true"
            class="detail-mix"
            @select="(l) => filters.setFilter('label', l)"
          />
        </div>
        <div class="section-head">Activity by list</div>
        <div class="minirow-head">
          <span>List</span>
          <span style="text-align: right;">Msgs</span>
          <span>Analysis</span>
          <span>Replied to</span>
          <span>Reply from</span>
        </div>
        <div
          v-for="r in detailCard.byList"
          :key="r.list"
          class="minirow"
          title="Filter to this list"
          @click="r.click"
        >
          <span class="minirow-name mono">{{ r.list }}</span>
          <span class="minirow-count mono">{{ r.count }}</span>
          <MixBar :counts="r.counts" :too-short="r.tooShort" :height="9" />
          <span v-if="!r.repliedTo.length" class="minirow-none">—</span>
          <span v-else class="mini-rug" :title="`last ${r.repliedTo.length} · oldest → newest`">
            <span
              v-for="b in r.repliedTo"
              :key="b.id"
              class="mini-rug-bar"
              :style="{ background: b.color }"
              :title="b.title"
              @click.stop="openRugMessage(b.id)"
            ></span>
          </span>
          <span v-if="!r.replyFrom.length" class="minirow-none">—</span>
          <span v-else class="mini-rug" :title="`last ${r.replyFrom.length} · oldest → newest`">
            <span
              v-for="b in r.replyFrom"
              :key="b.id"
              class="mini-rug-bar"
              :style="{ background: b.color }"
              :title="b.title"
              @click.stop="openRugMessage(b.id)"
            ></span>
          </span>
        </div>
      </template>
    </div>

    <div v-else class="pane-body senders-scroll" @scroll="onScroll">
      <!-- sticky column header -->
      <div class="senders-head">
        <span class="sortable" @click="sortName">Sender{{ nameInd }}</span>
        <span>Address</span>
        <span class="sortable" style="text-align: right;" @click="sortCount">Emails{{ countInd }}</span>
        <span class="sortable" title="Sort by AI share" @click="sortAi"
          >Aggregate analysis{{ aiInd }}</span
        >
        <span style="text-align: right;">Link</span>
      </div>

      <div>
        <div v-for="row in rows" :key="row.key" class="senders-row">
          <span
            class="sender-name"
            :title="row.emails"
            @click="showSender(row)"
            >{{ row.name }} <span class="linked-note">{{ row.linkedNote }}</span></span
          >
          <span class="sender-addr mono" :title="row.emails">{{ row.emails }}</span>
          <span class="sender-count mono">{{ row.count }}</span>
          <MixBar :counts="row.counts" :too-short="row.tooShort" :height="9" />
          <span style="text-align: right;">
            <button
              class="link-btn"
              :title="row.linkTitle"
              :style="{ color: row.linkColor }"
              @click="togglePop(row.key)"
            >
              ⇄
            </button>
          </span>

          <!-- popover -->
          <div
            v-if="openPop === row.key"
            class="pop"
            :style="{ top: row.popTop, bottom: row.popBottom }"
          >
            <!-- linked person -->
            <template v-if="row.isPerson">
              <div class="pop-caption">Linked sender · {{ row.chips.length }} addresses</div>
              <input
                type="text"
                class="pop-rename"
                :value="row.name"
                title="Rename sender"
                @blur="(e) => renamePerson(row, e)"
              />
              <div class="pop-chips">
                <span v-for="c in row.chips" :key="c.id" class="pop-chip mono">
                  {{ c.email }}
                  <button class="pop-chip-x" title="Detach address" @click="detachAddress(row, c.id)">
                    ×
                  </button>
                </span>
              </div>
              <button class="pop-danger" @click="unlinkPerson(row)">Unlink all addresses</button>
            </template>

            <!-- unlinked address -->
            <template v-else>
              <div class="pop-caption">Link this address</div>
              <div class="pop-email mono">{{ row.emails }}</div>
              <div v-if="row.sib" class="pop-sib">
                <button class="pop-primary" @click="linkSiblings(row)">
                  Link with {{ row.sib.count }} same-name address{{ row.sib.count > 1 ? 'es' : '' }}
                </button>
                <div class="pop-sib-emails mono">{{ row.sib.emails }}</div>
              </div>
              <div v-if="persons.length" class="pop-assign-row">
                <select
                  class="pop-select"
                  :value="assignId"
                  @change="(e) => (assignId = e.target.value)"
                >
                  <option value="">add to existing sender…</option>
                  <option v-for="p in persons" :key="p.id" :value="String(p.id)">
                    {{ p.canonical_name }}
                  </option>
                </select>
                <button class="pop-primary pop-link-btn" :disabled="!assignId" @click="assignToExisting(row)">
                  Link
                </button>
              </div>
              <button class="pop-secondary" @click="newSender(row)">New sender from this address</button>
            </template>
          </div>
        </div>
      </div>

      <div v-if="empty" class="senders-empty">No senders match this search.</div>
    </div>

    <div v-if="!detailMode" class="senders-footer">{{ loadedNote }}</div>

    <!-- click-catcher closes the open popover -->
    <div v-if="openPop" class="pop-catcher" @click="closePop"></div>
  </div>
</template>

<style scoped>
.mono {
  font-family: var(--mono);
}

/* --- sender detail mode --- */
.senders-detail {
  padding: 10px 12px;
}
.detail-status {
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 4px 0;
}
.detail-error {
  color: var(--danger);
}
.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.detail-name {
  font-size: 14px;
  font-weight: 700;
}
.close-x {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}
.detail-email {
  font-size: 10.5px;
  color: var(--text-secondary);
}
.detail-stats {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 8px;
}
.detail-mix {
  flex: 1;
  padding-top: 1px;
}
.tile {
  background: var(--tile);
  border-radius: 4px;
  padding: 5px 8px;
  flex: none;
  min-width: 52px;
}
.tile-val {
  font-size: 14px;
  font-weight: 700;
  font-family: var(--mono);
}
.tile-cap {
  font-size: 9.5px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 700;
}
.section-head {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin: 12px 0 3px;
}
/* Activity-by-list table. Five columns: list, message count, the detection mix
   bar, and the two reply rugs (each capped at 50 bars — see .mini-rug). */
.minirow-head,
.minirow {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 36px 96px 104px 104px;
  gap: 6px;
  align-items: center;
}
.minirow-head {
  border-bottom: 1px solid var(--border);
  padding: 0 0 2px;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}
.minirow {
  border-bottom: 1px solid var(--border-row);
  cursor: pointer;
  padding: 3px 0;
  font-size: 11.5px;
}
.minirow:hover {
  background: var(--hover-row);
}
.minirow-name {
  font-weight: 500;
  color: var(--text-name);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.minirow:hover .minirow-name {
  color: var(--accent);
}
.minirow-count {
  text-align: right;
  color: var(--text-secondary);
}
.minirow-none {
  color: var(--text-muted);
  font-family: var(--mono);
}

/* --- reply rugs (table-cell scale of the per-list rug in ListsPane) --- */
.mini-rug {
  display: flex;
  gap: 1px;
  height: 14px;
  align-items: stretch;
  overflow: hidden;
}
.mini-rug-bar {
  flex: 1;
  min-width: 1px;
  max-width: 6px;
  border-radius: 1px;
  cursor: pointer;
}
.mini-rug-bar:hover {
  opacity: 0.75;
}
.senders-search {
  font-size: 11px;
  height: 21px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 150px;
  box-sizing: border-box;
}
.senders-scroll {
  padding: 0 12px 10px;
}

/* --- sticky column header --- */
.senders-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) 58px 150px 34px;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding: 8px 0 2px;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--surface);
}
.senders-head .sortable {
  cursor: pointer;
}
.senders-head .sortable:hover {
  color: var(--accent);
}

/* --- rows --- */
.senders-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) 58px 150px 34px;
  gap: 6px;
  align-items: center;
  border-bottom: 1px solid var(--border-row);
  padding: 3px 0;
  font-size: 11.5px;
}
.senders-row:hover {
  background: var(--hover-row);
}
.sender-name {
  font-weight: 500;
  color: var(--text-name);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sender-name:hover {
  color: var(--accent);
}
.linked-note {
  color: var(--text-muted);
  font-weight: 400;
  font-size: 10px;
}
.sender-addr {
  color: var(--text-secondary);
  font-size: 10.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sender-count {
  text-align: right;
  color: var(--text-secondary);
}
.link-btn {
  border: 1px solid var(--border);
  background: var(--surface);
  cursor: pointer;
  font-size: 11px;
  line-height: 1;
  border-radius: 3px;
  padding: 2px 5px;
}
.link-btn:hover {
  border-color: var(--accent);
  color: var(--accent) !important;
}

/* --- popover --- */
.pop {
  position: absolute;
  right: 28px;
  z-index: 30;
  width: 300px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(15, 18, 22, 0.16);
  padding: 9px 10px;
  cursor: default;
}
.pop-caption {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-bottom: 5px;
}
.pop-rename {
  font-size: 11.5px;
  font-weight: 600;
  padding: 2px 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 100%;
  margin-bottom: 6px;
  box-sizing: border-box;
}
.pop-rename:focus {
  border-color: var(--accent);
  outline: none;
}
.pop-chips {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 8px;
}
.pop-chip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  background: var(--tile);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 1px 3px 1px 6px;
  font-size: 10px;
}
.pop-chip-x {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 11px;
  line-height: 1;
  padding: 0 2px;
}
.pop-chip-x:hover {
  color: var(--danger);
}
.pop-danger {
  border: 1px solid #e3c2c2;
  background: none;
  color: var(--danger);
  cursor: pointer;
  font-size: 10px;
  font-weight: 700;
  border-radius: 3px;
  padding: 3px 8px;
}
.pop-email {
  font-size: 10.5px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.pop-sib {
  margin-bottom: 8px;
}
.pop-primary {
  font-size: 10.5px;
  font-weight: 700;
  padding: 3px 8px;
  border: none;
  border-radius: 3px;
  background: var(--accent);
  color: #ffffff;
  cursor: pointer;
}
.pop-sib .pop-primary {
  width: 100%;
}
.pop-sib-emails {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 3px;
}
.pop-assign-row {
  display: flex;
  gap: 5px;
  align-items: center;
  margin-bottom: 6px;
}
.pop-select {
  font-size: 11px;
  height: 24px;
  padding: 0 2px;
  border: 1px solid var(--border-input);
  border-radius: 3px;
  background: var(--surface);
  color: var(--text);
  flex: 1;
  min-width: 0;
}
.pop-link-btn {
  height: 24px;
  padding: 0 8px;
  flex: none;
}
.pop-primary:disabled {
  opacity: 0.5;
  cursor: default;
}
.pop-secondary {
  font-size: 10.5px;
  font-weight: 600;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
}

/* --- empty / footer / catcher --- */
.senders-empty {
  font-size: 10.5px;
  color: var(--text-muted);
  padding: 4px 0;
}
.senders-footer {
  flex: none;
  padding: 3px 12px;
  border-top: 1px solid var(--border);
  background: var(--toolbar);
  font-size: 10.5px;
  color: var(--text-muted);
  font-family: var(--mono);
}
.pop-catcher {
  position: fixed;
  inset: 0;
  z-index: 29;
}
</style>
