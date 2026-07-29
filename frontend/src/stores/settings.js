// Server-side settings, as opposed to the local display preferences in ui.js.
//
// At present the store holds one value: which Pangram detector new scoring uses.
// The API reports it as `pangram_model`, either "pangram-4" (the current
// default) or "default" (Pangram 3). It is shared state — the header switch and
// the Pangram upgrade modal both write it, and both read it back through this
// store so the header stays in step.

import { defineStore } from 'pinia'

import { get, putJson } from '../api'

const MODEL_V4 = 'pangram-4'
const MODEL_V3 = 'default'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    // Assume the current default until the fetch says otherwise; a failed fetch
    // therefore leaves the header switch off, and a later toggle still writes.
    pangramModel: MODEL_V4,
    loaded: false,
  }),

  getters: {
    // The header switch is labelled "Use Pangram v3 (old)", so it is on exactly
    // when the configured model is the pre-Pangram-4 default.
    usingPangramV3: (state) => state.pangramModel === MODEL_V3,
  },

  actions: {
    async load() {
      try {
        const res = await get('/settings')
        if (res?.pangram_model) this.pangramModel = res.pangram_model
        this.loaded = true
      } catch {
        // Never block the dashboard on this fetch.
      }
    },

    // Write the model and adopt whatever the server reports back. The local
    // value is updated first so the header reacts immediately, and rolled back
    // if the write fails; the error is re-thrown for the caller to display.
    async setPangramModel(model) {
      const next = model === MODEL_V3 ? MODEL_V3 : MODEL_V4
      const previous = this.pangramModel
      this.pangramModel = next
      try {
        const res = await putJson('/settings', { pangram_model: next })
        if (res?.pangram_model) this.pangramModel = res.pangram_model
        this.loaded = true
      } catch (err) {
        this.pangramModel = previous
        throw err
      }
    },

    setUsePangramV3(on) {
      return this.setPangramModel(on ? MODEL_V3 : MODEL_V4)
    },
  },
})
