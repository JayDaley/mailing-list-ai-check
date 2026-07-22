// Tiny formatting helpers shared across the dashboard.

// Format an ISO date/datetime string as 'YYYY-MM-DD HH:mm' (UTC minutes),
// matching the prototype's compact table stamp. Returns '' for a falsy input
// and the raw string if it cannot be parsed.
export function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toISOString().slice(0, 16).replace('T', ' ')
}

// Format an integer with locale grouping (e.g. 1412 -> "1,412").
export function fmtInt(n) {
  if (n == null || Number.isNaN(n)) return ''
  return Number(n).toLocaleString()
}
