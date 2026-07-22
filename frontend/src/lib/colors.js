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
