import React, { useState } from 'react'

function fmtDur(s) {
  s = Math.max(0, s || 0)
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60
  return h ? `${h}h ${m}m` : m ? `${m}m ${r}s` : `${r}s`
}

function fmtWhen(ts) {
  const d = new Date(ts * 1000)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return sameDay ? `Today ${t}` : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}`
}

/** List of completed cleaning sessions (newest first), persisted server-side. */
export default function HistoryPanel({ sessions = [] }) {
  const [showAll, setShowAll] = useState(false)
  const list = showAll ? sessions : sessions.slice(0, 8)
  const totalArea = sessions.reduce((a, s) => a + (s.area_m2 || 0), 0)

  return (
    <section className="card">
      <h2>History</h2>
      {sessions.length === 0
        ? <p className="muted">No cleaning sessions recorded yet.</p>
        : <>
            <p className="muted small">
              {sessions.length} session{sessions.length > 1 ? 's' : ''} · {totalArea.toFixed(1)} m² total
            </p>
            <ul className="history">
              {list.map((s) => (
                <li key={s.id} className={s.completed ? '' : 'partial'}>
                  <span className={`hmode ${s.mode}`}>{s.mode === 'mop' ? '💧' : '🧹'}</span>
                  <div className="hmain">
                    <div className="htitle">
                      {s.room_names?.length ? s.room_names.join(', ') : 'Whole house'}
                    </div>
                    <div className="hsub">
                      {fmtWhen(s.started_at)} · {fmtDur(s.duration_s)} · {s.area_m2} m²
                      {!s.completed && <span className="hflag"> · stopped</span>}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
            {sessions.length > 8 &&
              <button className="ghost" onClick={() => setShowAll((v) => !v)}>
                {showAll ? 'show less' : `show all ${sessions.length}`}
              </button>}
          </>}
    </section>
  )
}
