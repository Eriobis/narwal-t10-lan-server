import React, { useEffect, useRef, useState, useCallback } from 'react'
import { FLIP_DISPLAY_Y, ROOM_TINTS, ZONE_COLORS } from './api.js'


/**
 * Crisp, devicePixelRatio-aware occupancy-grid renderer with an optional room
 * overlay, editable no-go / virtual-wall rectangles, and the robot pose.
 *
 * Retina strategy: paint the grid 1:1 into an OFFSCREEN canvas at native cell
 * resolution, then blit it onto the on-screen canvas (sized cssW * dpr) with
 * smoothing OFF. Zones, draft rectangle and pose are drawn in display space.
 *
 * Props:
 *   map      {width,height,resolution,origin,data}
 *   pose     {x,y,theta}
 *   overlay  {data:[roomId per cell, aligned to map]} | null
 *   zones    [{type:'nogo'|'wall', x0,y0,x1,y1}]  (world metres)
 *   draft    one in-progress zone | null
 *   editable bool  -> enables pointer drawing
 *   onPaint  (worldX, worldY, phase:'down'|'move'|'up') => void
 */
export default function MapCanvas({
  map, pose, overlay = null, zones = [], draft = null, editable = false, onPaint,
}) {
  const canvasRef = useRef(null)
  const wrapRef = useRef(null)
  const [resizeTick, setResizeTick] = useState(0)

  // world <-> display helpers (depend on map + current canvas size)
  const worldToPx = useCallback((wx, wy, cw, ch) => {
    const cx = (wx - map.origin.x) / map.resolution
    const cy = (wy - map.origin.y) / map.resolution
    const sy = FLIP_DISPLAY_Y ? cy : (map.height - 1 - cy)
    return [(cx / map.width) * cw, (sy / map.height) * ch]
  }, [map])

  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap || !map) return

    const dpr = window.devicePixelRatio || 1
    const { width: gw, height: gh, data } = map
    const cssW = wrap.clientWidth
    const cssH = Math.round(cssW * (gh / gw))
    canvas.style.width = cssW + 'px'
    canvas.style.height = cssH + 'px'
    canvas.width = Math.round(cssW * dpr)
    canvas.height = Math.round(cssH * dpr)

    // ---- offscreen native render (occupancy + room overlay) ----
    const off = document.createElement('canvas')
    off.width = gw; off.height = gh
    const octx = off.getContext('2d')
    const img = octx.createImageData(gw, gh)
    const ov = overlay && overlay.data
    for (let y = 0; y < gh; y++) {
      for (let x = 0; x < gw; x++) {
        const gi = (FLIP_DISPLAY_Y ? y : (gh - 1 - y)) * gw + x // row order per FLIP_DISPLAY_Y
        const v = data[gi]
        let r, g, b
        if (v >= 65) { r = 18; g = 20; b = 26 }          // wall
        else {
          const rid = ov ? ov[gi] : -1
          if (rid != null && rid >= 0) {                  // room tint
            const c = ROOM_TINTS[rid % ROOM_TINTS.length]
            r = c[0]; g = c[1]; b = c[2]
          } else if (v < 0) { r = 45; g = 47; b = 54 }   // unknown
          else { r = 208; g = 212; b = 218 }             // free
        }
        const p = (y * gw + x) * 4
        img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255
      }
    }
    octx.putImageData(img, 0, 0)

    const ctx = canvas.getContext('2d')
    ctx.imageSmoothingEnabled = false
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(off, 0, 0, canvas.width, canvas.height)

    // ---- zones + draft (display space) ----
    const drawRect = (z, dashed) => {
      const [ax, ay] = worldToPx(z.x0, z.y0, canvas.width, canvas.height)
      const [bx, by] = worldToPx(z.x1, z.y1, canvas.width, canvas.height)
      if (z.type === 'cut') {                      // split line: draw the segment itself
        ctx.strokeStyle = 'rgba(255,255,255,0.95)'
        ctx.lineWidth = Math.max(3 * dpr, 2)
        ctx.setLineDash([10 * dpr, 6 * dpr])
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke()
        ctx.setLineDash([])
        return
      }
      const x = Math.min(ax, bx), y = Math.min(ay, by)
      const w = Math.abs(bx - ax), h = Math.abs(by - ay)
      const c = ZONE_COLORS[z.type] || ZONE_COLORS.nogo
      ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},0.28)`
      ctx.fillRect(x, y, w, h)
      ctx.strokeStyle = `rgba(${c[0]},${c[1]},${c[2]},0.95)`
      ctx.lineWidth = Math.max(2 * dpr, 1.5)
      if (dashed) ctx.setLineDash([8 * dpr, 6 * dpr]); else ctx.setLineDash([])
      ctx.strokeRect(x, y, w, h)
      ctx.setLineDash([])
    }
    zones.forEach((z) => drawRect(z, false))
    if (draft) drawRect(draft, true)

    // ---- robot pose ----
    if (pose && map.resolution && map.origin) {
      const [px, py] = worldToPx(pose.x, pose.y, canvas.width, canvas.height)
      const rad = Math.max(5 * dpr, canvas.width * 0.012)
      ctx.fillStyle = 'rgba(255,92,92,0.95)'
      ctx.beginPath(); ctx.arc(px, py, rad, 0, Math.PI * 2); ctx.fill()
      ctx.strokeStyle = 'rgba(255,255,255,0.9)'
      ctx.lineWidth = Math.max(1.5 * dpr, 1)
      ctx.beginPath(); ctx.moveTo(px, py)
      ctx.lineTo(px + Math.cos(pose.theta || 0) * rad * 2,
                 py + (FLIP_DISPLAY_Y ? 1 : -1) * Math.sin(pose.theta || 0) * rad * 2)
      ctx.stroke()
    }
  }, [map, pose, overlay, zones, draft, worldToPx, resizeTick])

  // resize / rotation -> redraw
  useEffect(() => {
    let raf = 0
    const onResize = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => setResizeTick((t) => t + 1))
    }
    window.addEventListener('resize', onResize)
    window.addEventListener('orientationchange', onResize)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      window.removeEventListener('orientationchange', onResize)
    }
  }, [])

  // ---- pointer -> world coordinates for editing ----
  const pxToWorld = (ev) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const fx = (ev.clientX - rect.left) / rect.width
    const fy = (ev.clientY - rect.top) / rect.height
    const cellX = fx * map.width
    const cellY = FLIP_DISPLAY_Y ? (fy * map.height) : (map.height - 1 - fy * map.height)
    return [map.origin.x + cellX * map.resolution,
            map.origin.y + cellY * map.resolution]
  }
  const handler = (phase) => (ev) => {
    if (!editable || !onPaint) return
    ev.preventDefault()
    const [wx, wy] = pxToWorld(ev)
    onPaint(wx, wy, phase)
  }

  return (
    <div ref={wrapRef} className="mapcanvas-wrap">
      {map
        ? <canvas
            ref={canvasRef}
            className="mapcanvas"
            style={{ touchAction: editable ? 'none' : 'auto', cursor: editable ? 'crosshair' : 'default' }}
            onPointerDown={handler('down')}
            onPointerMove={handler('move')}
            onPointerUp={handler('up')}
            onPointerLeave={handler('up')}
          />
        : <div className="map-empty">waiting for map…</div>}
    </div>
  )
}
