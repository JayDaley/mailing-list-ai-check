// Instance capability flags, read once at start-up from GET /api/capabilities.
//
// They mirror the deployment settings the server enforces regardless (read-only
// mode and the two export switches); the UI reads them only to hide controls
// that would otherwise 403. The defaults are permissive, so a failed fetch — or
// an older server without the endpoint — leaves every control shown and working
// rather than hiding something that is actually available.

import { defineStore } from 'pinia'

import { get } from '../api'

export const useCapabilitiesStore = defineStore('capabilities', {
  state: () => ({
    publicReadonly: false,
    allowExport: true,
    allowStatsExport: true,
    loaded: false,
  }),

  getters: {
    // The export button is worth showing when at least one format is offered.
    canExport: (state) => state.allowExport || state.allowStatsExport,
  },

  actions: {
    async load() {
      try {
        const res = await get('/capabilities')
        if (res && typeof res === 'object') {
          this.publicReadonly = !!res.public_readonly
          this.allowExport = res.allow_export !== false
          this.allowStatsExport = res.allow_stats_export !== false
        }
        this.loaded = true
      } catch {
        // Never block the dashboard on this fetch; keep the permissive defaults.
      }
    },
  },
})
