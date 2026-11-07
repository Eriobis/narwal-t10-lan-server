import React, { useEffect, useMemo, useState } from 'react'
import MapCanvas from './MapCanvas.jsx'
import { getEdits, saveEdits, roomEdit, ROOM_TINTS, ZONE_LABELS } from './api.js'

// Map editor. The ROBOT is the source of truth (transport=cloud):
//  - no-go zones / virtual walls: Save sends the full list to the robot
//    (/pita/plan_map/set user_forbidden); the robot's list is re-read afterwards.
//  - split / merge: computed on the robot's live room grid, PREVIEWED here, and
//    only sent when you press Apply. Room ids are reused by the robot, so a new
//    room starts unnamed.
// Names and clean order stay on this server.
const ZONE_TOOLS = [
  ['nogo', '⛔ No-go'], ['nosweep', '🚫🧹 No vacuum'], ['nomop', '🚫💧 No mop'], ['wall', '▮ Wall'],
]

export default function MapEditor({ map, rooms, pose, mapId }) {
  const [edits, setEdits] = useState({ room_names: {}, zones: [], clean_order: [], zones_rev: null })
  const [mode, setMode] = useState('view')   // view | nogo | nosweep | nomop | wall | split | merge
  const [draft, setDraft] = useState(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState(null) // {seg, info, body}
  const [pick, setPick] = useState(null)       // first room picked for merge

  const liveOverlay = useMemo(
    () => (rooms?.segmentation ? { data: rooms.segmentation.data } : null), [rooms])
  const overlay = preview ? { data: preview.seg } : liveOverlay
  const roomList = rooms?.rooms || []

  const loadEdits = () => mapId && getEdits(mapId).then((e) => {
    if (e) setEdits({ room_names: e.room_names || {}, zones: e.zones || [], clean_order: e.clean_order || [],
                      zones_rev: e.zones_rev ?? 0 })
  })
  useEffect(() => { loadEdits() }, [mapId])
  const flash = (t, ms = 4000) => { setMsg(t); if (ms) setTimeout(() => setMsg(''), ms) }

  const effectiveOrder = edits.clean_order.length
    ? edits.clean_order
    : (rooms?.clean_order?.sweep?.length ? rooms.clean_order.sweep : roomList.map((r) => r.id))
  const update = (patch) => setEdits((e) => ({ ...e, ...patch }))
  const nameOf = (id) =>
    edits.room_names[id] || (roomList.find((r) => r.id === id)?.name) || `Room ${id}`

  // room id under a world point (live room layer, occupancy-grid aligned)
  const roomAt = (wx, wy) => {
    const seg = liveOverlay?.data
    if (!seg || !map) return null
    const cx = Math.floor((wx - map.origin.x) / map.resolution)
    const cy = Math.floor((wy - map.origin.y) / map.resolution)
    if (cx < 0 || cy < 0 || cx >= map.width || cy >= map.height) return null
    const v = seg[cy * map.width + cx]
    return v != null && v >= 0 ? v : null
  }

  const askPreview = async (body) => {
    setBusy(true)
    const res = await roomEdit(body, false)
    setBusy(false)
    if (res.error) { flash('✗ ' + res.error, 6000); return }
    setPreview({ seg: res.preview, info: res.info, body })
  }

  // ---- drawing / picking ----
  const onPaint = (wx, wy, phase) => {
    if (mode === 'view' || preview || busy) return
    if (mode === 'delete') {
      if (phase !== 'down') return
      const rid = roomAt(wx, wy)
      if (rid != null) askPreview({ op: 'delete', room: rid })
      return
    }
    if (mode === 'merge') {
      if (phase !== 'down') return
      const rid = roomAt(wx, wy)
      if (rid == null) return
      if (pick == null) { setPick(rid); flash(`Merging into ${nameOf(rid)} — now tap the room to absorb`, 0); return }
      if (rid === pick) return
      setPick(null); setMsg('')
      askPreview({ op: 'merge', rooms: [pick, rid] })
      return
    }
    const type = mode === 'split' ? 'cut' : mode
    if (phase === 'down') setDraft({ type, x0: wx, y0: wy, x1: wx, y1: wy })
    else if (phase === 'move') setDraft((d) => (d ? { ...d, x1: wx, y1: wy } : d))
    else if (phase === 'up') {
      setDraft((d) => {
        if (!d) return null
        if (d.type === 'cut') {
          if (Math.hypot(d.x1 - d.x0, d.y1 - d.y0) > 0.3)
            askPreview({ op: 'split', line: [[d.x0, d.y0], [d.x1, d.y1]] })
        } else if (Math.abs(d.x1 - d.x0) > 0.1 && (Math.abs(d.y1 - d.y0) > 0.1 || d.type === 'wall')) {
          const z = { ...d, id: Math.random().toString(36).slice(2, 10) }
          setEdits((e) => ({ ...e, zones: [...e.zones, z] }))
        }
        return null
      })
    }
  }

  const applyPreview = async () => {
    setBusy(true)
    const res = await roomEdit(preview.body, true)
    setBusy(false)
    if (res.error || !res.ok) { flash('✗ robot refused: ' + (res.error || JSON.stringify(res.reply)), 8000); return }
    setPreview(null); setMode('view')
    flash('✓ Robot updated its rooms', 5000)
    setTimeout(loadEdits, 3500)                 // names were remapped server-side
  }

  const removeZone = (id) => update({ zones: edits.zones.filter((z) => z.id !== id) })
  const renameRoom = (id, name) => update({ room_names: { ...edits.room_names, [id]: name } })
  const moveOrder = (idx, dir) => {
    const arr = [...effectiveOrder]; const j = idx + dir
    if (j < 0 || j >= arr.length) return
    ;[arr[idx], arr[j]] = [arr[j], arr[idx]]
    update({ clean_order: arr })
  }

  const doSave = async () => {
    setBusy(true)
    const res = await saveEdits({ map_id: mapId, ...edits, clean_order: effectiveOrder })
    setBusy(false)
    const r = res?.robot
    flash(!res?.ok ? '✗ save failed'
      : r?.sent && r?.ok ? '✓ Saved · zones sent to the robot'
      : r?.stale ? '⚠ The robot\'s zones changed since this page loaded — reloaded them, please redo your zone change'
      : `✓ Saved · ${r?.detail || ''}`, 8000)
    if (r?.stale) loadEdits()
    else if (r?.sent && r?.ok) setTimeout(loadEdits, 3500)   // robot's list is authoritative
    else if (res?.edits) setEdits((e) => ({ ...e, zones_rev: res.edits.zones_rev }))
  }

  const describe = (info) => info.op === 'delete'
    ? `Delete ${nameOf(info.removed)}: the area stops being a room and the robot will not clean it (room ${info.removed} is freed).`
    : info.op === 'split'
    ? `Split ${nameOf(info.room)} into two: the new part becomes room ${info.new} (unnamed${edits.room_names[info.new] ? `, replaces the name “${edits.room_names[info.new]}”` : ''}).`
    : `Merge ${nameOf(info.removed)} into ${nameOf(info.kept)} (room ${info.removed} is freed).`

  const hint = {
    nogo: 'Drag a rectangle: no vacuum and no mop.', nosweep: 'Drag a rectangle: no vacuum (mopping allowed).',
    nomop: 'Drag a rectangle: no mop (vacuum allowed).', wall: 'Drag along the wall: a thin barrier the robot will not cross.',
    split: 'Draw a line across a room, from wall to wall. (Max 8 rooms: delete or merge one first if full.)',
    delete: 'Tap the room to delete.', merge: pick == null ? 'Tap the room to keep.' : 'Tap the room to merge into it.',
  }[mode]

  return (
    <div className="editor">
      <div className="toolbar">
        <button className={mode === 'view' ? 'tool on' : 'tool'} onClick={() => { setMode('view'); setPick(null) }}>✋ View</button>
        {ZONE_TOOLS.map(([m, label]) => (
          <button key={m} className={mode === m ? `tool on ${m}` : 'tool'} onClick={() => { setMode(m); setPick(null) }}>{label}</button>
        ))}
        <button className={mode === 'split' ? 'tool on' : 'tool'} onClick={() => { setMode('split'); setPick(null) }}>✂ Split</button>
        <button className={mode === 'merge' ? 'tool on' : 'tool'} onClick={() => { setMode('merge'); setPick(null) }}>⧉ Merge</button>
        <button className={mode === 'delete' ? 'tool on' : 'tool'} onClick={() => { setMode('delete'); setPick(null) }}>🗑 Delete room</button>
        <button className="tool save" disabled={busy} onClick={doSave}>💾 Save</button>
      </div>
      {mode !== 'view' && !preview && <p className="hint">{hint}</p>}
      {msg && <p className="hint">{msg}</p>}

      {preview &&
        <div className="editcard preview">
          <b>Preview</b> — {describe(preview.info)}
          <div className="cleanbar">
            <button className="big primary" disabled={busy} onClick={applyPreview}>{busy ? 'Sending…' : 'Apply to robot'}</button>
            <button className="ghost" disabled={busy} onClick={() => setPreview(null)}>Cancel</button>
          </div>
        </div>}

      <MapCanvas map={map} pose={pose} overlay={overlay} zones={preview ? [] : edits.zones}
                 draft={draft} editable={mode !== 'view' && !preview} onPaint={onPaint} />

      {edits.zones.length > 0 &&
        <div className="zonelist">
          <h2>Zones ({edits.zones.length})</h2>
          {edits.zones.map((z) => (
            <div key={z.id} className="zonerow">
              <span className={`zbadge ${z.type}`}>{ZONE_LABELS[z.type] || z.type}</span>
              <span className="muted small">
                ({z.x0.toFixed(2)},{z.y0.toFixed(2)})→({z.x1.toFixed(2)},{z.y1.toFixed(2)}) m
              </span>
              <button className="del" onClick={() => removeZone(z.id)}>✕</button>
            </div>
          ))}
          <p className="muted small">Removing a zone or drawing a new one takes effect on the robot when you press Save.</p>
        </div>}

      <div className="editcard">
        <h2>Rename rooms</h2>
        {roomList.map((r) => {
          const c = ROOM_TINTS[r.id % ROOM_TINTS.length]
          return (
            <div key={r.id} className="renamerow">
              <span className="swatch" style={{ background: `rgb(${c[0]},${c[1]},${c[2]})` }} />
              <span className="muted small">#{r.id}</span>
              <input value={edits.room_names[r.id] ?? ''} placeholder={`Room ${r.id}`}
                     onChange={(e) => renameRoom(r.id, e.target.value)} />
            </div>
          )
        })}
        {roomList.length === 0 && <p className="muted">no rooms in this map</p>}
      </div>

      <div className="editcard">
        <h2>Clean order</h2>
        {effectiveOrder.map((id, idx) => {
          const c = ROOM_TINTS[id % ROOM_TINTS.length]
          return (
            <div key={`${id}-${idx}`} className="orderrow">
              <span className="ordnum">{idx + 1}</span>
              <span className="swatch" style={{ background: `rgb(${c[0]},${c[1]},${c[2]})` }} />
              <span className="ordname">{nameOf(id)}</span>
              <button className="ord" onClick={() => moveOrder(idx, -1)} disabled={idx === 0}>▲</button>
              <button className="ord" onClick={() => moveOrder(idx, 1)} disabled={idx === effectiveOrder.length - 1}>▼</button>
            </div>
          )
        })}
      </div>

      <p className="muted small note">
        Rooms and no-go zones come from the robot and are written back to it.
        Room names and the clean order are kept on this server.
      </p>
    </div>
  )
}
