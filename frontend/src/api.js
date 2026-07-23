// Tiny fetch client for the /api JSON blueprint (see webapp/api.py).
//
// Every endpoint returns JSON. On a failure the API responds with
// {"error": "..."} and a non-2xx status; we surface that message as a thrown
// Error so callers can show it in their loading/error state.

const BASE = '/api'

function buildQuery(params) {
  if (!params) return ''
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    usp.append(key, value)
  }
  const s = usp.toString()
  return s ? `?${s}` : ''
}

async function handle(res) {
  let data = null
  try {
    data = await res.json()
  } catch {
    // Non-JSON response (should not happen for the API, but be defensive).
    if (!res.ok) throw new Error(`Request failed (${res.status})`)
    return null
  }
  if (!res.ok || (data && typeof data === 'object' && 'error' in data)) {
    const msg = data && data.error ? data.error : `Request failed (${res.status})`
    throw new Error(msg)
  }
  return data
}

export function get(path, params) {
  return fetch(`${BASE}${path}${buildQuery(params)}`, {
    headers: { Accept: 'application/json' },
  }).then(handle)
}

// Absolute URL for a path (base-aware). Handy for downloads / <a href> where a
// raw fetch is done outside these JSON helpers.
export function apiUrl(path, params) {
  return `${BASE}${path}${buildQuery(params)}`
}

// POST a multipart FormData body (e.g. a file upload). Same JSON error handling
// as the other helpers: {"error": ...} + non-2xx becomes a thrown Error.
export function postForm(path, formData, params) {
  return fetch(`${BASE}${path}${buildQuery(params)}`, {
    method: 'POST',
    headers: { Accept: 'application/json' },
    body: formData,
  }).then(handle)
}

export function postJson(path, body) {
  return fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(handle)
}

export function putJson(path, body) {
  return fetch(`${BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(handle)
}

export function del(path) {
  return fetch(`${BASE}${path}`, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
  }).then(handle)
}

export default { get, postJson, putJson, del }
