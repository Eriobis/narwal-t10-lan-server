import React, { useEffect, useMemo, useRef, useState } from 'react'
import MapCanvas from './MapCanvas.jsx'
import VectorMap from './VectorMap.jsx'
import MapEditor from './MapEditor.jsx'
import { getJSON, sendCommand, openSocket, mapImageUrl, getVector, getEdits, rasterDataUrl, ROOM_TINTS,
         getHistory, clean } from './api.js'
import HistoryPanel from './HistoryPanel.jsx'

const STATE_LABEL = {
  cleaning: 'Cleaning', docked: 'Docked', paused: 'Paused',
  idle: 'Idle', returning: 'Returning to dock', error: 'Error',
}

function fmtTime(s) {
  s = s || 0
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

export default function App() {
  const [status, setStatus] = useState(null)
  const [map, setMap] = useState(null)
  const [rooms, setRooms] = useState(null)
  const [imgToken, setImgToken] = useState(0)   // bumps to reload map_rotate.png
  const [tab, setTab] = useState('grid')        // 'grid' | 'image' | 'edit'
  const [wsUp, setWsUp] = useState(false)
  const [vector, setVector] = useState(null)    // vector map (SVG source)
  const [showRaster, setShowRaster] = useState(false)
  const [viewZones, setViewZones] = useState([]) // editor zones shown read-only
  const [history, setHistory] = useState([])     // completed cleaning sessions, newest first
  const [selected, setSelected] = useState([])   // room ids picked for a targeted clean
  const sockRef = useRef(null)

  // Live WebSocket wiring (+ one REST bootstrap so the first paint is instant).
  useEffect(() => {
    let alive = true
    getJSON('/api/status').then((s) => alive && s && setStatus(s))
    getJSON('/api/map').then((m) => alive && m && !m.error && setMap(m))
    getJSON('/api/rooms').then((r) => alive && r && !r.error && setRooms(r))
    getHistory().then((h) => alive && setHistory(h))

    const sock = openSocket((ev) => {
      if (!ev || !ev.type) return
      if (ev.type === 'status') setStatus(ev.data)
      else if (ev.type === 'map') setMap(ev.data)
      else if (ev.type === 'rooms') setRooms(ev.data)
      else if (ev.type === 'map_image') setImgToken(Date.now())
      else if (ev.type === 'edits') {        // renames / zones saved -> refresh labels + zones
        setViewZones(ev.data?.zones || [])
        getVector().then((v) => v && !v.error && setVector(v))
      }
      else if (ev.type === 'history_all') setHistory(ev.data || [])
      else if (ev.type === 'history') setHistory((h) => [ev.data, ...h.filter((x) => x.id !== ev.data.id)])
    }, setWsUp)
    sockRef.current = sock

    return () => { alive = false; sock.close() }
  }, [])

  const cmd = (c, args) => sendCommand(c, args)
  const toggleRoom = (id) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const cleanSelected = () => { clean(selected); setSelected([]) }
  const sweepSet = new Set(rooms?.sweep_rooms || [])
  const mopSet = new Set(rooms?.mop_rooms || [])
  const roomName = (id) => rooms?.rooms?.find((r) => r.id === id)?.name || `Room ${id}`

  const overlay = useMemo(
    () => (rooms?.segmentation ? { data: rooms.segmentation.data } : null),
    [rooms])
  const mapId = status?.current_map_id || map?.map_id

  // fetch the vector map + editor zones when the map changes
  useEffect(() => {
    if (!map) return
    getVector().then((v) => setVector(v && !v.error ? v : null))
    getEdits(mapId).then((e) => setViewZones(e?.zones || []))
  }, [map?.map_id, mapId])

  // raster underlay (built from occupancy + room overlay) for the vector view
  const rasterUrl = useMemo(
    () => (showRaster && map ? rasterDataUrl(map, overlay) : null),
    [showRaster, map, overlay])

  const battery = status?.battery ?? 0
  const state = status?.state ?? 'idle'
  const online = status?.online
  const batColor = battery <= 15 ? 'var(--err)' : battery <= 35 ? 'var(--warn)' : 'var(--ok)'

  return (
    <div className="app">
      <header className="topbar">
        <span className={`dot ${online ? 'on' : 'off'}`} />
        <div className="title">
          <strong>Narwal T10</strong>
          <small>{status?.transport ? `transport: ${status.transport}` : ''}
            {status?.ha_connected ? ' · HA ✓' : ''}
            {wsUp ? ' · live' : ' · reconnecting…'}</small>
        </div>
      </header>

      <main className="content">
        {/* ---- MAP (centrepiece) ---- */}
        <section className="card map-card">
          <div className="tabs">
            <button className={tab === 'grid' ? 'tab on' : 'tab'}
                    onClick={() => setTab('grid')}>Map</button>
            <button className={tab === 'image' ? 'tab on' : 'tab'}
                    onClick={() => setTab('image')}>Robot map</button>
            <button className={tab === 'edit' ? 'tab on' : 'tab'}
                    onClick={() => setTab('edit')}>Edit</button>
          </div>
          {tab === 'grid' &&
            <>
              {vector
                ? <VectorMap vector={vector} pose={status?.pose} zones={viewZones}
                             rasterUrl={rasterUrl} showRaster={showRaster}
                             selected={selected} onRoomClick={toggleRoom} />
                : <MapCanvas map={map} pose={status?.pose} overlay={overlay} />}
              <div className="map-legend">
                {vector
                  ? <>
                      <label className="rastertoggle">
                        <input type="checkbox" checked={showRaster}
                               onChange={(e) => setShowRaster(e.target.checked)} />
                        raster underlay
                      </label>
                      <span className="muted small">
                        {vector.counts.rooms} rooms · {vector.counts.walls} wall lines
                      </span>
                    </>
                  : <>
                      <span><i className="sw free" /> free</span>
                      <span><i className="sw wall" /> wall</span>
                      <span><i className="sw unk" /> unknown</span>
                      <span><i className="sw bot" /> robot</span>
                    </>}
              </div>
            </>}
          {tab === 'image' && <RobotImage token={imgToken} />}
          {tab === 'edit' &&
            (map
              ? <MapEditor map={map} rooms={rooms} pose={status?.pose} mapId={mapId} />
              : <div className="map-empty">no map to edit yet</div>)}
        </section>

        {/* ---- STATUS ---- */}
        <section className="card">
          <div className="staterow">
            <div className={`statebadge s-${state}`}>{STATE_LABEL[state] || state}</div>
            <div className="batwrap">
              <div className="bat"><i style={{ width: battery + '%', background: batColor }} /></div>
              <span className="batpct">{battery}%</span>
            </div>
          </div>
          <div className="stats">
            <Stat label="Module" value={status?.mode === 'mop' ? '💧 mop pad' : '🧹 brush'} />
            <Stat label="Fan" value={status?.fan_speed || '—'} />
            <Stat label="Area" value={`${status?.clean_area_m2 ?? 0} m²`} />
            <Stat label="Time" value={fmtTime(status?.clean_time_s)} />
            <Stat label="Error" value={status?.error || 'none'} />
          </div>
        </section>

        {/* ---- WHOLE-HOUSE CONTROLS (big touch targets) ---- */}
        <section className="card controls">
          <button className="big primary wide" onClick={() => clean()}>▶ Clean all</button>
          {state === 'paused'
            ? <button className="big" onClick={() => cmd('resume')}>▶ Resume</button>
            : <button className="big" onClick={() => cmd('pause')}>⏸ Pause</button>}
          <button className="big" onClick={() => cmd('stop')}>■ Stop</button>
          <button className="big dock" onClick={() => cmd('dock')}>⌂ Dock</button>
          <button className="big ghost" onClick={() => cmd('locate')}>🔔 Locate</button>
        </section>

        {/* ---- PER-ROOM CLEAN ---- */}
        <section className="card">
          <h2>Clean rooms</h2>
          <p className="muted small">
            Tap rooms here or on the map, then clean. Vacuum or mop depends on the module
            fitted (currently <b>{status?.mode === 'mop' ? 'mop pad' : 'brush'}</b>).
          </p>
          {rooms?.rooms?.length
            ? <div className="rooms">
                {rooms.rooms.map((r) => {
                  const [cr, cg, cb] = ROOM_TINTS[r.id % ROOM_TINTS.length]
                  const on = selected.includes(r.id)
                  return (
                    <button key={r.id} className={on ? 'room sel' : 'room'}
                            style={{ borderColor: `rgb(${cr},${cg},${cb})` }}
                            onClick={() => toggleRoom(r.id)}>
                      <span className="swatch" style={{ background: `rgb(${cr},${cg},${cb})` }} />
                      {r.name}
                      <span className="caps">
                        {sweepSet.has(r.id) && <i title="robot vacuums this room">V</i>}
                        {mopSet.has(r.id) && <i className="m" title="robot mops this room">M</i>}
                      </span>
                    </button>
                  )
                })}
              </div>
            : <p className="muted">no rooms captured yet</p>}
          <div className="cleanbar">
            <button className="big primary" disabled={!selected.length} onClick={cleanSelected}>
              Clean {selected.length || ''} selected
            </button>
            {selected.length > 0 &&
              <button className="ghost" onClick={() => setSelected([])}>clear</button>}
          </div>
        </section>

        {/* ---- ROBOT'S SAVED SETUP (kept from the Narwal app) ---- */}
        {(rooms?.sweep_rooms?.length || rooms?.mop_rooms?.length) &&
          <section className="card">
            <h2>Saved cleaning setup</h2>
            <p className="muted small">Preserved from the robot's clean scheme — the order it cleans rooms in.</p>
            <div className="order">
              <span className="olabel">🧹 Vacuum</span>
              {(rooms.sweep_rooms || []).map((id, i) => <span key={i} className="chip">{i + 1}. {roomName(id)}</span>)}
            </div>
            <div className="order">
              <span className="olabel">💧 Mop</span>
              {(rooms.mop_rooms || []).map((id, i) => <span key={i} className="chip m">{i + 1}. {roomName(id)}</span>)}
            </div>
            <div className="cleanbar">
              <button onClick={() => clean(rooms.sweep_rooms)}>Run saved order</button>
            </div>
          </section>}

        {/* ---- HISTORY ---- */}
        <HistoryPanel sessions={history} />
      </main>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div className="stat">
      <span className="stat-l">{label}</span>
      <span className="stat-v">{value}</span>
    </div>
  )
}

function RobotImage({ token }) {
  const [ok, setOk] = useState(true)
  useEffect(() => { setOk(true) }, [token])   // retry when a fresh image arrives
  return (
    <div className="mapcanvas-wrap">
      {ok
        ? <img className="robot-img" alt="robot map (map_rotate.png)"
               src={mapImageUrl(token)} onError={() => setOk(false)} />
        : <div className="map-empty">no robot-rendered map yet (map_rotate.png)</div>}
    </div>
  )
}
