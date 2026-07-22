// Shared label / score-band vocabulary for the dashboard.
//
// Every mix bar, badge, percent pill and label bar draws its colors from here so
// the whole screen reads as one system. Colors are the Okabe-Ito palette from the
// design handoff; the score bands (by fraction_ai) drive the percent pills and
// Avg-AI values (text color on a tint background).

// Segment order for every stacked mix bar (Human first, AI last).
export const LABEL_ORDER = ['Human', 'Mixed', 'AI-Assisted', 'AI']

// Label colors — used for badges and all mix/label bar fills.
export const LABEL_COLORS = {
  AI: '#d55e00',
  'AI-Assisted': '#e69f00',
  Mixed: '#56b4e9',
  Human: '#009e73',
  unscored: '#c7ccd1',
}

// Short caption words for the "Human · Mixed · Assisted · AI" mix-bar legend.
export const LABEL_SHORT = {
  Human: 'Human',
  Mixed: 'Mixed',
  'AI-Assisted': 'Assisted',
  AI: 'AI',
}

// The mix-bar caption text shared everywhere.
export const MIX_CAPTION = 'Human · Mixed · Assisted · AI'

// Return the score band ({name, text, bg}) for a fraction_ai in [0,1].
// null / undefined / NaN → the unscored band.
export function bandFor(fractionAi) {
  if (fractionAi == null || Number.isNaN(fractionAi)) {
    return { name: null, text: '#c7ccd1', bg: '#f0f1f3' }
  }
  if (fractionAi >= 0.8) return { name: 'AI', text: '#b34f00', bg: '#fae4d6' }
  if (fractionAi >= 0.5) return { name: 'AI-Assisted', text: '#9c6c00', bg: '#f9efda' }
  if (fractionAi >= 0.3) return { name: 'Mixed', text: '#2f7fae', bg: '#e4f2fb' }
  return { name: 'Human', text: '#00734f', bg: '#dff1ea' }
}

// The label color for a given label name (falls back to the unscored grey).
export function labelColor(label) {
  return LABEL_COLORS[label] || LABEL_COLORS.unscored
}
