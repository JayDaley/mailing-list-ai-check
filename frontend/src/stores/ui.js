// Small UI-preference store: "anonymous mode" (hides sender-identifying UI),
// the draggable pane split percentages (topPct / leftPct), the table row
// density, and the Senders pane's "Show all" switch. All are persisted to
// localStorage so the choices survive reloads.
//
// The first three are display-only. showAllSenders is the exception: it is a
// preference in the same sense, but the filtering happens server-side (it sets
// GET /api/senders?include_excluded), so changing it must refetch the pane.

import { defineStore } from 'pinia'

const STORAGE_KEY = 'mail-ai:anonymous'
const SHOW_ALL_SENDERS_KEY = 'mail-ai:showAllSenders'
const TOP_KEY = 'mail-ai:topPct'
const LEFT_KEY = 'mail-ai:leftPct'
const DENSITY_KEY = 'mail-ai:density'

// Clamp bounds for the pane splits (percent of the content / lower-row box).
const TOP_MIN = 18
const TOP_MAX = 80
const LEFT_MIN = 20
const LEFT_MAX = 75

function clamp(value, min, max) {
  const n = Number(value)
  if (!Number.isFinite(n)) return min
  return Math.min(max, Math.max(min, n))
}

function loadBool(key) {
  try {
    return localStorage.getItem(key) === 'true'
  } catch {
    return false
  }
}

function loadNum(key, fallback, min, max) {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null) return fallback
    return clamp(parseFloat(raw), min, max)
  } catch {
    return fallback
  }
}

function loadDensity() {
  try {
    const raw = localStorage.getItem(DENSITY_KEY)
    return raw === 'comfortable' ? 'comfortable' : 'compact'
  } catch {
    return 'compact'
  }
}

function persist(key, value) {
  try {
    localStorage.setItem(key, String(value))
  } catch {
    // Persistence is best-effort; ignore failures (private mode, tests).
  }
}

export const useUiStore = defineStore('ui', {
  state: () => ({
    anonymous: loadBool(STORAGE_KEY),
    topPct: loadNum(TOP_KEY, 58, TOP_MIN, TOP_MAX),
    leftPct: loadNum(LEFT_KEY, 42, LEFT_MIN, LEFT_MAX),
    density: loadDensity(),
    // Off by default: senders who can never be scored are hidden until asked for.
    showAllSenders: loadBool(SHOW_ALL_SENDERS_KEY),
  }),

  actions: {
    setAnonymous(value) {
      this.anonymous = !!value
      persist(STORAGE_KEY, this.anonymous)
    },

    setShowAllSenders(value) {
      this.showAllSenders = !!value
      persist(SHOW_ALL_SENDERS_KEY, this.showAllSenders)
    },

    setTopPct(value) {
      this.topPct = clamp(value, TOP_MIN, TOP_MAX)
      persist(TOP_KEY, this.topPct)
    },

    setLeftPct(value) {
      this.leftPct = clamp(value, LEFT_MIN, LEFT_MAX)
      persist(LEFT_KEY, this.leftPct)
    },

    setDensity(value) {
      this.density = value === 'comfortable' ? 'comfortable' : 'compact'
      persist(DENSITY_KEY, this.density)
    },
  },
})
