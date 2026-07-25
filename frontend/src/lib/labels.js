// Shared label / score-band vocabulary for the dashboard.
//
// Every mix bar, badge, percent pill and label bar draws its colors from here so
// the whole screen reads as one system. Colors are Observable 10; the score bands
// (by fraction_ai) drive the percent pills and Avg-AI values (text color on a
// tint background).

// Segment order for every stacked mix bar (Human first, AI last).
export const LABEL_ORDER = ['Human', 'Mixed', 'AI-Assisted', 'AI']

// Pangram's prediction_short vocabulary — the three categories it actually
// emits. The dashboard's four-band `label` rebadges assisted-dominant "Mixed"
// verdicts as "AI-Assisted"; the aggregate detection bars fold that back (see
// foldToPrediction) so they show only these three.
export const PRED_ORDER = ['Human', 'Mixed', 'AI']

// Fold a four-band {label: count} map into the three prediction_short buckets:
// "AI-Assisted" counts merge into "Mixed" (its original prediction_short, per
// the label rebadge rule). Human / AI pass through unchanged.
export function foldToPrediction(counts) {
  const c = counts || {}
  return {
    Human: Number(c.Human) || 0,
    Mixed: (Number(c.Mixed) || 0) + (Number(c['AI-Assisted']) || 0),
    AI: Number(c.AI) || 0,
  }
}

// The prediction_short for a stored four-band label ("AI-Assisted" → "Mixed").
export function predictionShort(label) {
  return label === 'AI-Assisted' ? 'Mixed' : label
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
// prediction_short buckets map to blue (Human) / orange (Mixed) / red (AI);
// "AI-Assisted" is a rebadged Mixed, so it keeps the Mixed orange.
export const LABEL_COLORS = {
  AI: OBSERVABLE_10.red,
  'AI-Assisted': OBSERVABLE_10.orange,
  Mixed: OBSERVABLE_10.orange,
  Human: OBSERVABLE_10.blue,
  unscored: OBSERVABLE_10.grey,
}

// Lighter tints of the label colors (the score-band backgrounds). Used by
// bandFor for the percent-pill backgrounds (dark text sits on them).
export const LABEL_TINTS = {
  AI: '#ffe4de',
  'AI-Assisted': '#fcf1d6',
  Mixed: '#fcf1d6',
  Human: '#e3e9f8',
}

// Short caption words for the "Human · Mixed · Assisted · AI" mix-bar legend.
export const LABEL_SHORT = {
  Human: 'Human',
  Mixed: 'Mixed',
  'AI-Assisted': 'Assisted',
  AI: 'AI',
}

// Return the score band ({name, text, bg}) for a fraction_ai in [0,1].
// null / undefined / NaN → the unscored band.
export function bandFor(fractionAi) {
  if (fractionAi == null || Number.isNaN(fractionAi)) {
    return { name: null, text: OBSERVABLE_10.grey, bg: '#f0f1f3' }
  }
  if (fractionAi >= 0.8) return { name: 'AI', text: '#c2412b', bg: LABEL_TINTS.AI }
  if (fractionAi >= 0.5) return { name: 'AI-Assisted', text: '#8a6300', bg: LABEL_TINTS['AI-Assisted'] }
  if (fractionAi >= 0.3) return { name: 'Mixed', text: '#8a6300', bg: LABEL_TINTS.Mixed }
  return { name: 'Human', text: '#2d4b9e', bg: LABEL_TINTS.Human }
}

// The label color for a given label name (falls back to the unscored grey).
export function labelColor(label) {
  return LABEL_COLORS[label] || LABEL_COLORS.unscored
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
