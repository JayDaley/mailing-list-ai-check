// The shared filtered message result set.
//
// This is the single source of truth for the rows visible in the messages table
// AND for the drawer's prev/next stepping through "the current filtered+sorted
// result set". It fetches pages from GET /api/messages using the filters store's
// `asParams`, overriding `page` as needed for infinite scroll.
//
// Contract (relied on by MessagesPane and the DetailDrawer agent):
//   state:   items[]  (message rows as returned by /api/messages)
//            total    (full match count before pagination)
//            loading, error
//            page     (last page loaded)
//   actions: refresh()  reset to page 1 and replace items
//            loadMore()  fetch page+1 and append (no-op while loading or done)
//   getter:  hasMore   (items.length < total)

import { defineStore } from 'pinia'

import { get } from '../api'
import { useFiltersStore } from './filters'

export const useMessagesStore = defineStore('messages', {
  state: () => ({
    items: [],
    total: 0,
    loading: false,
    error: null,
    page: 1,
  }),

  getters: {
    hasMore(state) {
      return state.items.length < state.total
    },
  },

  actions: {
    // Reset to page 1 and fetch, replacing items. Used on mount and whenever any
    // non-page filter/sort key changes.
    async refresh() {
      const filters = useFiltersStore()
      this.loading = true
      this.error = null
      try {
        const params = { ...filters.asParams, page: 1 }
        const data = await get('/messages', params)
        this.items = data.messages || []
        this.total = data.total || 0
        this.page = 1
      } catch (err) {
        this.error = err instanceof Error ? err.message : String(err)
        this.items = []
        this.total = 0
        this.page = 1
      } finally {
        this.loading = false
      }
    },

    // Fetch the next page and append. No-op while loading or when everything is
    // already loaded.
    async loadMore() {
      if (this.loading || this.items.length >= this.total) return
      const filters = useFiltersStore()
      const nextPage = this.page + 1
      this.loading = true
      this.error = null
      try {
        const params = { ...filters.asParams, page: nextPage }
        const data = await get('/messages', params)
        this.items = this.items.concat(data.messages || [])
        this.total = data.total || 0
        this.page = nextPage
      } catch (err) {
        this.error = err instanceof Error ? err.message : String(err)
      } finally {
        this.loading = false
      }
    },
  },
})
