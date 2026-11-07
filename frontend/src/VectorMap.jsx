import React, { useMemo } from 'react'
import { ZONE_COLORS, FLIP_DISPLAY_Y } from './api.js'

/**
 * Crisp SVG renderer for the VECTOR map: room polygons (filled, with holes),
 * wall polylines, robot pose, and the editor's no-go / virtual-wall rects on
 * top. Sharp at any zoom (no pixelation). Optional raster underlay (built from
 * the occupancy grid) for verifying alignment.
 *
 * World coords are metres, y-up. SVG is y-down, so every point is reflected
 * about the vertical midline of the bounds: svgY = (maxy+miny) - y. Strokes use
 * vector-effect:non-scaling-stroke so they stay a constant pixel width.
 */
export default function VectorMap({ vector, pose, zones = [], rasterUrl = null, showRaster = false,
                                    selected = [], onRoomClick = null }) {
  const anySel = selected.length > 0
  const b = vector?.bounds
  const flipY = b ? b.maxy + b.miny : 0
  // FLIP_DISPLAY_Y=true -> plot world y directly (world min-y at top, matches the app);
  // false -> reflect about the bounds midline (world max-y at top, robot-native).
  const toXY = (p) => [p[0], FLIP_DISPLAY_Y ? p[1] : (flipY - p[1])]
  const viewBox = useMemo(() => {
    if (!b) return '0 0 1 1'
    return `${b.minx} ${b.miny} ${b.maxx - b.minx} ${b.maxy - b.miny}`
  }, [b])

  if (!vector || !b) return <div className="map-empty">no vector map yet</div>

  const span = Math.max(b.maxx - b.minx, b.maxy - b.miny)
  const fontSize = span / 32
  const poseR = span / 70

  // room polygon -> SVG path (exterior + holes, even-odd fill)
  const roomPath = (poly) => {
    const ring = (pts) => 'M' + pts.map((p) => toXY(p).join(',')).join('L') + 'Z'
    let d = ring(poly.exterior)
    for (const h of poly.holes) d += ' ' + ring(h)
    return d
  }
  const polyline = (pts) => pts.map((p) => toXY(p).join(',')).join(' ')
  return (
    <div className="mapcanvas-wrap">
      <svg className="vectormap" viewBox={viewBox}
           preserveAspectRatio="xMidYMid meet"
           xmlns="http://www.w3.org/2000/svg">
        {/* raster underlay for alignment check */}
        {showRaster && rasterUrl &&
          <image href={rasterUrl} x={b.minx} y={b.miny}
                 width={b.maxx - b.minx} height={b.maxy - b.miny}
                 preserveAspectRatio="none" style={{ imageRendering: 'pixelated', opacity: 0.55 }} />}

        {/* rooms */}
        {vector.rooms.map((r) => {
          const isSel = selected.includes(r.id)
          const op = showRaster ? 0.35 : (anySel ? (isSel ? 0.9 : 0.25) : 0.72)
          return (
            <g key={r.id} onClick={onRoomClick ? () => onRoomClick(r.id) : undefined}
               style={onRoomClick ? { cursor: 'pointer' } : undefined}>
              {r.polygons.map((pg, i) => (
                <path key={i} d={roomPath(pg)} fill={r.color}
                      fillOpacity={op} fillRule="evenodd"
                      stroke={isSel ? '#ffffff' : r.color} strokeWidth={isSel ? 3 : 2}
                      vectorEffect="non-scaling-stroke" />
              ))}
            </g>
          )
        })}

        {/* walls */}
        {vector.walls.map((w, i) => (
          <polyline key={i} points={polyline(w)} fill="none" pointerEvents="none"
                    stroke="#2a2d34" strokeWidth="1.5" strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke" />
        ))}

        {/* editor zones */}
        {zones.map((z, i) => {
          const [ax, ay] = toXY([z.x0, z.y0])
          const [bx, by] = toXY([z.x1, z.y1])
          const c = ZONE_COLORS[z.type] || ZONE_COLORS.nogo
          const rgb = `rgb(${c[0]},${c[1]},${c[2]})`
          return <rect key={z.id || i} pointerEvents="none" x={Math.min(ax, bx)} y={Math.min(ay, by)}
                       width={Math.abs(bx - ax)} height={Math.abs(by - ay)}
                       fill={rgb} fillOpacity="0.25" stroke={rgb} strokeWidth="2"
                       vectorEffect="non-scaling-stroke" />
        })}

        {/* room labels */}
        {vector.rooms.map((r) => {
          const [x, y] = toXY(r.label)
          return (
            <text key={`l${r.id}`} x={x} y={y} fontSize={fontSize} pointerEvents="none"
                  textAnchor="middle" dominantBaseline="central"
                  fill="#0b0d12" stroke="#fff" strokeWidth={fontSize * 0.06}
                  paintOrder="stroke" style={{ fontWeight: 700 }}>
              {r.name}
            </text>
          )
        })}

        {/* robot pose */}
        {pose &&
          <g>
            <circle cx={toXY([pose.x, pose.y])[0]} cy={toXY([pose.x, pose.y])[1]}
                    r={poseR} fill="#ff5c5c" stroke="#fff" strokeWidth="1.5"
                    vectorEffect="non-scaling-stroke" />
            <line x1={toXY([pose.x, pose.y])[0]} y1={toXY([pose.x, pose.y])[1]}
                  x2={toXY([pose.x + Math.cos(pose.theta || 0) * poseR * 2.4,
                            pose.y + Math.sin(pose.theta || 0) * poseR * 2.4])[0]}
                  y2={toXY([pose.x + Math.cos(pose.theta || 0) * poseR * 2.4,
                            pose.y + Math.sin(pose.theta || 0) * poseR * 2.4])[1]}
                  stroke="#fff" strokeWidth="2" vectorEffect="non-scaling-stroke" />
          </g>}
      </svg>
    </div>
  )
}
