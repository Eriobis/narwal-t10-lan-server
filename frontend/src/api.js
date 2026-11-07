// REST + WebSocket client for the Narwal T10 LAN server.
// Same-origin in production (served by FastAPI); dev uses Vite's proxy.

export async function getJSON(path) {
  const r = await fetch(path)
  if (!r.ok) return null
  return r.json()
}

export async function sendCommand(cmd, args = {}) {
  const r = await fetch('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd, args }),
  })
  return r.ok ? r.json() : null
}

// URL for the robot's rendered map image; cache-bust with ?t= so a fresh
// map_image event forces a reload.
export function mapImageUrl(token) {
  return `/api/map_image?t=${token || Date.now()}`
}

export async function getVector() {
  return getJSON('/api/map_vector')
}

export async function getHistory(limit = 50) {
  const r = await getJSON(`/api/history?limit=${limit}`)
  return r?.sessions || []
}

// Start a clean; rooms = array of ids (in order), or null for the whole house.
// Vacuum vs mop is the physical module fitted on the robot, not a command.
export function clean(rooms = null) {
  return sendCommand('clean', { rooms: rooms && rooms.length ? rooms : null })
}

// Display orientation (PRESENTATION ONLY — world / robot coordinates are never
// changed by this; only how the map is drawn on screen and how screen clicks map
// back to world). true = world min-y at the TOP of the view (matches the Narwal
// app / how the house is pictured). false = world max-y at top (the robot's own
// pita_grid.map.png orientation). Flip here and every world<->screen mapping follows.
export const FLIP_DISPLAY_Y = false

// Room tint palette (kept in sync with the server-side seg colors).
export const ROOM_TINTS = [
  [70, 130, 220], [230, 150, 40], [70, 200, 120], [200, 70, 160], [230, 210, 60],
  [120, 200, 230], [180, 120, 230], [230, 90, 90], [120, 180, 90], [200, 140, 200],
]

export const ZONE_COLORS = {
  nogo: [255, 70, 70],      // no vacuum + no mop
  nosweep: [255, 160, 40],  // no vacuum only
  nomop: [40, 170, 255],    // no mop only
  wall: [255, 0, 40],       // virtual wall (thin no-go strip on the robot)
  cut: [255, 255, 255],     // split line (editor only)
}

export const ZONE_LABELS = {
  nogo: 'no-go', nosweep: 'no vacuum', nomop: 'no mop', wall: 'virtual wall',
}

// Room split / merge on the robot's live room map.
// body: {op:'split', line:[[x,y],[x,y]]} | {op:'merge', rooms:[keep, absorb]}; apply=false -> preview only.
export async function roomEdit(body, apply = false) {
  const r = await fetch('/api/rooms/edit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, apply }),
  })
  const j = await r.json().catch(() => ({}))
  return r.ok ? j : { error: j.error || `HTTP ${r.status}` }
}

// Build a pixelated raster PNG dataURL from an occupancy grid (+ optional room
// overlay) for use as an SVG underlay. Row order follows FLIP_DISPLAY_Y.
export function rasterDataUrl(map, overlay) {
  if (!map) return null
  const { width: w, height: h, data } = map
  const cv = document.createElement('canvas')
  cv.width = w; cv.height = h
  const ctx = cv.getContext('2d')
  const img = ctx.createImageData(w, h)
  const ov = overlay && overlay.data
  const TINTS = [
    [70, 130, 220], [230, 150, 40], [70, 200, 120], [200, 70, 160], [230, 210, 60],
    [120, 200, 230], [180, 120, 230], [230, 90, 90], [120, 180, 90], [200, 140, 200],
  ]
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const gi = (FLIP_DISPLAY_Y ? y : (h - 1 - y)) * w + x
      const v = data[gi]
      let r, g, bl
      if (v >= 65) { r = 18; g = 20; bl = 26 }
      else {
        const rid = ov ? ov[gi] : -1
        if (rid != null && rid >= 0) { const c = TINTS[rid % TINTS.length]; r = c[0]; g = c[1]; bl = c[2] }
        else if (v < 0) { r = 45; g = 47; bl = 54 } else { r = 208; g = 212; bl = 218 }
      }
      const p = (y * w + x) * 4
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = bl; img.data[p + 3] = 255
    }
  }
  ctx.putImageData(img, 0, 0)
  try { return cv.toDataURL('image/png') } catch (_e) { return null }
}

export async function getEdits(mapId) {
  return getJSON('/api/edits' + (mapId ? `?map_id=${encodeURIComponent(mapId)}` : ''))
}

export async function saveEdits(edits) {
  const r = await fetch('/api/edits', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(edits),
  })
  return r.ok ? r.json() : null
}

// Open the live WebSocket. onEvent({type,data}) is called for every message.
// Auto-reconnects with backoff. Returns a close() function.
export function openSocket(onEvent, onOpenChange) {
  let ws = null
  let closed = false
  let backoff = 500

  const connect = () => {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}/ws`)

    ws.onopen = () => { backoff = 500; onOpenChange && onOpenChange(true) }
    ws.onmessage = (ev) => {
      try { onEvent(JSON.parse(ev.data)) } catch (_e) { /* ignore */ }
    }
    ws.onclose = () => {
      onOpenChange && onOpenChange(false)
      if (!closed) {
        setTimeout(connect, backoff)
        backoff = Math.min(backoff * 2, 8000)
      }
    }
    ws.onerror = () => { try { ws.close() } catch (_e) {} }
  }

  connect()
  return {
    close() { closed = true; try { ws && ws.close() } catch (_e) {} },
    send(obj) { try { ws && ws.readyState === 1 && ws.send(JSON.stringify(obj)) } catch (_e) {} },
  }
}
