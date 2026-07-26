// Likelihood colour scale: green (0, human) -> amber (0.5) -> red (1, AI).
//
// Shared by the Overview and (agent 2) the Explorer/Detail views so the whole
// dashboard reads a fraction_ai value the same way. Hand-rolled HSL lerp, no
// dependency. Pass a fraction_ai in [0, 1]; null/undefined -> neutral grey.

const NEUTRAL = '#c7ccd1'

function clamp01(x) {
  if (x < 0) return 0
  if (x > 1) return 1
  return x
}

// Interpolate hue 130 (green) -> 45 (amber) -> 5 (red) as t goes 0 -> 1.
export function likelihoodColor(fraction) {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) {
    return NEUTRAL
  }
  const t = clamp01(fraction)
  let hue
  if (t < 0.5) {
    // green -> amber
    hue = 130 + (45 - 130) * (t / 0.5)
  } else {
    // amber -> red
    hue = 45 + (5 - 45) * ((t - 0.5) / 0.5)
  }
  const sat = 68
  const light = 45
  return `hsl(${hue.toFixed(0)}, ${sat}%, ${light}%)`
}

// A softer background tint of the same scale (for cells/badges).
export function likelihoodTint(fraction) {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) {
    return '#f0f1f3'
  }
  const t = clamp01(fraction)
  let hue
  if (t < 0.5) {
    hue = 130 + (45 - 130) * (t / 0.5)
  } else {
    hue = 45 + (5 - 45) * ((t - 0.5) / 0.5)
  }
  return `hsl(${hue.toFixed(0)}, 70%, 92%)`
}

// --- reply-rate heat scale ----------------------------------------------------
// The chars/minute a reply implies (see the reply-timing analysis in store.py)
// is tinted in ten steps of the Observable-10 purple, one per hundred: 100-199,
// 200-299, ... 900-999, and 1000+. Each step mixes the full colour with white
// in sRGB, from a tenth of it at 100-199 up to the colour itself at 1000+.
// Below 100 chars/minute — the threshold under which the rate is not flagged at
// all — and for an unknown rate there is no tint.
const RATE_PURPLE_RGB = [164, 99, 242] // #a463f2
const RATE_TINT_FLOOR = 100 // chars/minute of the first tinted band
const RATE_TINT_BANDS = 10 // bands, the last one open-ended
//: Above this rate the tint is dark enough that the muted grey cell text drops
//: below white's contrast against it, so those cells take white text instead.
const RATE_WHITE_TEXT_FLOOR = 700

function mixWithWhite([r, g, b], fraction) {
  const hex = (c) =>
    Math.round(255 + (c - 255) * fraction)
      .toString(16)
      .padStart(2, '0')
  return `#${hex(r)}${hex(g)}${hex(b)}`
}

// The band index (0-based) of a chars/minute rate, or null when untinted.
function rateBand(cpm) {
  if (cpm === null || cpm === undefined || Number.isNaN(cpm) || cpm < RATE_TINT_FLOOR) {
    return null
  }
  return Math.min(Math.floor(cpm / 100) - 1, RATE_TINT_BANDS - 1)
}

// Cell background for a chars/minute rate; null means "leave it untinted".
export function rateTint(cpm) {
  const band = rateBand(cpm)
  return band === null ? null : mixWithWhite(RATE_PURPLE_RGB, (band + 1) / RATE_TINT_BANDS)
}

// Text colour override for a tinted cell; null means "keep the cell's own".
export function rateTextColor(cpm) {
  return rateBand(cpm) !== null && cpm >= RATE_WHITE_TEXT_FLOOR ? '#ffffff' : null
}

// Fixed colours per Pangram label, used by label distribution bars & badges.
export const LABEL_COLORS = {
  AI: '#d64545',
  'AI-Assisted': '#e08a1e',
  Mixed: '#c9a227',
  Human: '#2e8b57',
}

export function labelColor(label) {
  return LABEL_COLORS[label] || NEUTRAL
}
