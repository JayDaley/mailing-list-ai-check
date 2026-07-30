// Shared label / score-band vocabulary for the dashboard.
//
// Every mix bar, badge, percent pill and label bar draws its colors from here so
// the whole screen reads as one system. Colors are Observable 10; the score bands
// (by fraction_ai) drive the percent pills and Avg-AI values (text color on a
// tint background).

// Pangram's prediction_short vocabulary — the three categories it emits, and
// exactly what the store's `label` column holds (nothing is derived from it).
export const PRED_ORDER = ['Human', 'Mixed', 'AI']

// Coerce a {label: count} map into the three prediction_short buckets with
// numeric counts (missing or non-numeric values become 0).
export function foldToPrediction(counts) {
  const c = counts || {}
  return {
    Human: Number(c.Human) || 0,
    Mixed: Number(c.Mixed) || 0,
    AI: Number(c.AI) || 0,
  }
}

// The Observable 10 categorical palette (the dashboard's whole color vocabulary).
export const OBSERVABLE_10 = {
  blue: '#4269d0',
  orange: '#efb118',
  red: '#ff725c',
  teal: '#6cc5b0',
  green: '#3ca951',
  pink: '#ff8ab7',
  purple: '#a463f2',
  lightBlue: '#97bbf5',
  brown: '#9c6b4e',
  grey: '#9498a0',
}

// Label colors — used for badges and all mix/label bar fills. The three
// prediction_short buckets map to blue (Human) / orange (Mixed) / red (AI).
export const LABEL_COLORS = {
  AI: OBSERVABLE_10.red,
  Mixed: OBSERVABLE_10.orange,
  Human: OBSERVABLE_10.blue,
  unscored: OBSERVABLE_10.grey,
}

// Messages gated under the 50-word reliability floor (extraction status
// `too_short`) are never sent to Pangram, so they carry no label. They get the
// Observable-10 grey: the trailing segment of every aggregate detection bar and
// the bar of a too-short message in a rug plot.
export const TOO_SHORT_COLOR = OBSERVABLE_10.grey

// A message that is neither scored nor gated (no extraction yet, an empty or a
// failed one) gets a lighter neutral in the rugs, so the two cases stay apart:
// the same neutral the dashboard uses elsewhere for "no value".
export const UNSCORED_RUG_COLOR = '#c7ccd1'

// Short caption words for the "Human · Mixed · AI" mix-bar legend.
export const LABEL_SHORT = {
  Human: 'Human',
  Mixed: 'Mixed',
  AI: 'AI',
}

// The label color for a given label name (falls back to the unscored grey).
export function labelColor(label) {
  return LABEL_COLORS[label] || LABEL_COLORS.unscored
}

// The fill for one rug-plot bar: its label color when scored, the too-short grey
// when the extraction was gated under the reliability floor, and the lighter
// unscored neutral otherwise.
export function rugBarColor(label, tooShort = false) {
  if (tooShort) return TOO_SHORT_COLOR
  return LABEL_COLORS[label] || UNSCORED_RUG_COLOR
}

// The prediction bucket for a per-window label. Pangram's window vocabulary is
// its own ("Human Written", "AI-Generated", "Lightly/Moderately AI-Assisted"):
// the assisted bands belong to Mixed, matching how the document-level
// prediction_short treats assisted text.
export function windowBucket(label) {
  if (label === 'Human Written') return 'Human'
  if (label === 'AI-Generated') return 'AI'
  if (label && label.includes('AI-Assisted')) return 'Mixed'
  return null
}
