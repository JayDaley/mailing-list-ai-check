// Global filter state, two-way synced with the URL query string.
//
// This store is the single source of truth for every filterable view. The
// filter bar writes into it; the views read `asParams()` and pass it straight
// to the API (the param names match webapp/api.py: list, address, person,
// date_from, date_to, label, min_likelihood, max_likelihood, q, has_score,
// sort, order, page, per_page).
//
// URL sync (for agent 2): call `bindToRouter(router)` once from main.js after
// the router is created. From then on:
//   - store -> URL: any filter change pushes a new query onto the current route
//     (shareable / bookmarkable), so back/forward restores the view.
//   - URL -> store: navigations (including back/forward and pasted links) load
//     the query into the store.
// Changing any filter (except page itself) resets page to 1.

import { defineStore } from 'pinia'

// Fields that live in the URL. `page`/`per_page` are numbers; the rest strings.
const DEFAULTS = {
  list: '',
  address: '',
  person: '',
  date_from: '',
  date_to: '',
  label: '',
  min_likelihood: '',
  max_likelihood: '',
  q: '',
  has_score: '',
  sort: 'date',
  order: 'desc',
  page: 1,
  per_page: 60,
}

// Enforce the person ⟂ address invariant on a plain object of pending writes:
// setting a non-empty `person` clears `address` and vice versa. Mutates and
// returns `obj`. Applied inside every writer (setFilter / patch / buildQuery)
// so the two can never both be active regardless of who set them.
function applyPersonAddressInvariant(obj) {
  if ('person' in obj && obj.person !== '' && obj.person != null) {
    obj.address = ''
  } else if ('address' in obj && obj.address !== '' && obj.address != null) {
    obj.person = ''
  }
  return obj
}

// Keys that, when changed, should reset pagination to page 1.
const PAGE_RESETTING = new Set([
  'list',
  'address',
  'person',
  'date_from',
  'date_to',
  'label',
  'min_likelihood',
  'max_likelihood',
  'q',
  'has_score',
  'sort',
  'order',
  'per_page',
])

function coerce(key, value) {
  if (value === undefined || value === null) return DEFAULTS[key]
  if (key === 'page' || key === 'per_page') {
    const n = parseInt(value, 10)
    return Number.isFinite(n) && n > 0 ? n : DEFAULTS[key]
  }
  return String(value)
}

export const useFiltersStore = defineStore('filters', {
  state: () => ({ ...DEFAULTS, _syncing: false }),

  getters: {
    // Only the non-empty / non-default params, ready for the API.
    asParams(state) {
      const out = {}
      for (const key of Object.keys(DEFAULTS)) {
        const value = state[key]
        if (value === '' || value === null || value === undefined) continue
        out[key] = value
      }
      return out
    },

    // True when any user-facing filter (not sort/paging) is active.
    hasActiveFilters(state) {
      const keys = [
        'list', 'address', 'person', 'date_from', 'date_to',
        'label', 'min_likelihood', 'max_likelihood', 'q', 'has_score',
      ]
      return keys.some((k) => state[k] !== '' && state[k] !== null && state[k] !== undefined)
    },
  },

  actions: {
    // Set one filter. Resets page to 1 for page-resetting keys.
    // Enforces the person ⟂ address invariant: setting one clears the other.
    setFilter(key, value) {
      if (!(key in DEFAULTS)) return
      this[key] = coerce(key, value)
      if (key === 'person' && this.person !== '') this.address = ''
      else if (key === 'address' && this.address !== '') this.person = ''
      if (PAGE_RESETTING.has(key)) this.page = 1
    },

    // Set several filters at once (single page reset if any resetting key).
    // Enforces the person ⟂ address invariant across the merged writes.
    patch(patchObj) {
      let reset = false
      const merged = applyPersonAddressInvariant({ ...patchObj })
      for (const [key, value] of Object.entries(merged)) {
        if (!(key in DEFAULTS)) continue
        this[key] = coerce(key, value)
        if (PAGE_RESETTING.has(key)) reset = true
      }
      if (reset && !('page' in patchObj)) this.page = 1
    },

    setPage(page) {
      this.page = coerce('page', page)
    },

    clearAll() {
      Object.assign(this, { ...DEFAULTS })
    },

    // Build the canonical URL/API query for the current filters plus an
    // optional patch, WITHOUT mutating the store. Views that navigate to a
    // *different* route (drill-downs) must use this instead of writing the
    // store then reading `asParams`: writing-then-navigating races the URL-sync
    // subscription below (which fires a competing push to the current route)
    // and can wedge the router. Values are run through the same `coerce` used
    // on hydration so a patched value and its URL round-trip compare equal.
    // Any changed filter returns to page 1 unless the patch sets `page`.
    buildQuery(patch = {}) {
      const query = { ...this.asParams }
      const merged = applyPersonAddressInvariant({ ...patch })
      for (const [key, value] of Object.entries(merged)) {
        if (!(key in DEFAULTS)) continue
        query[key] = coerce(key, value)
      }
      // If the patch activated one of person/address, drop the other from the
      // carried-over params so the invariant holds in the built query too.
      if (merged.person !== '' && merged.person != null && 'person' in merged) delete query.address
      else if (merged.address !== '' && merged.address != null && 'address' in merged) delete query.person
      if (!('page' in patch)) query.page = DEFAULTS.page
      for (const [key, value] of Object.entries(query)) {
        if (value === '' || value === null || value === undefined) delete query[key]
      }
      return query
    },

    // Replace state from a URL query object (URL -> store). Does not push back.
    loadFromQuery(query) {
      const next = { ...DEFAULTS }
      for (const key of Object.keys(DEFAULTS)) {
        if (key in query && query[key] !== undefined) {
          next[key] = coerce(key, query[key])
        }
      }
      this._syncing = true
      Object.assign(this, next)
      this._syncing = false
    },

    // Wire up two-way sync. Call once, after the router exists.
    bindToRouter(router) {
      // URL -> store on every navigation.
      const apply = (route) => this.loadFromQuery(route.query)
      apply(router.currentRoute.value)
      router.afterEach((to) => apply(to))

      // store -> URL when any filter changes (skip while loading from URL).
      //
      // `flush: 'sync'` is essential: the `_syncing` guard is set/cleared
      // synchronously in `loadFromQuery`, so with Pinia's default async flush
      // the callback would run *after* `_syncing` was reset — the guard would
      // never fire, and every URL->store hydration would echo back a redundant
      // store->URL push. Sync flush brackets the guard around the mutation.
      // The `queryEquals` check is a second, independent backstop: even if a
      // push slips through, it is skipped once the URL already matches state.
      this.$subscribe(
        () => {
          if (this._syncing) return
          const params = this.asParams
          const current = router.currentRoute.value
          // Only push if the query actually differs, to avoid redundant history
          // and to guarantee the sync settles after one round trip.
          if (!queryEquals(current.query, params)) {
            router.push({ path: current.path, query: params })
          }
        },
        { flush: 'sync' },
      )
    },
  },
})

function queryEquals(a, b) {
  const ak = Object.keys(a)
  const bk = Object.keys(b)
  if (ak.length !== bk.length) return false
  for (const k of ak) {
    if (String(a[k]) !== String(b[k])) return false
  }
  return true
}
