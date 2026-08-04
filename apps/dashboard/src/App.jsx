import React, { useEffect, useRef, useState } from "react";
import { api, auth } from "./api.js";

// Live worker status/logs over the Hub WebSocket. Returns a map worker_id -> {substate, logs[]}.
function useEvents(enabled) {
  const [live, setLive] = useState({});
  const ref = useRef(null);
  useEffect(() => {
    if (!enabled) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
    ref.current = ws;
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (!m.worker_id) return;
      setLive((prev) => {
        const cur = prev[m.worker_id] || { substate: null, logs: [] };
        const next = { ...cur };
        if (m.type === "status_change" && m.substate) next.substate = m.substate;
        if (m.type === "terminal") { next.substate = m.substate || next.substate; next.outcome = m.outcome; }
        if (m.type === "log_line" && m.text) next.logs = [...cur.logs.slice(-40), m.text];
        return { ...prev, [m.worker_id]: next };
      });
    };
    return () => ws.close();
  }, [enabled]);
  return live;
}

export default function App() {
  const [me, setMe] = useState(null);
  const [ready, setReady] = useState(false);
  const [page, setPage] = useState("bookings");
  const events = useEvents(!!me);

  async function refreshMe() {
    if (!auth.token) { setMe(null); setReady(true); return; }
    try { setMe(await api("/auth/whoami")); }
    catch { auth.clear(); setMe(null); }
    finally { setReady(true); }
  }
  useEffect(() => { refreshMe(); }, []);

  if (!ready) return <div className="container muted">Loading…</div>;
  if (!me) return <Login onLogin={refreshMe} />;

  const pages = {
    bookings: <Bookings me={me} events={events} />,
    identities: <Identities me={me} />,
    proxies: <Proxies me={me} />,
    settings: <Settings me={me} />,
    sessions: <Sessions me={me} onChange={refreshMe} />,
  };

  return (
    <>
      <div className="topbar">
        <span className="brand">⛈ storm-hub</span>
        <nav className="nav">
          {["bookings", "identities", "proxies", "settings", "sessions"].map((p) => (
            <button key={p} className={page === p ? "active" : ""} onClick={() => setPage(p)}>
              {p[0].toUpperCase() + p.slice(1)}
            </button>
          ))}
        </nav>
        <span className="grow" />
        <span className={`seat ${me.is_operating ? "operate" : "view"}`}>
          {me.is_operating ? "OPERATE" : `${me.purpose}${me.session_status === "paused" ? " (paused)" : ""}`}
        </span>
        <span className="muted">{me.username} · {me.role}</span>
        <button className="btn ghost" onClick={async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); auth.clear(); setMe(null); }}>
          Logout
        </button>
      </div>
      <div className="container">
        {!me.is_operating && (
          <div className="banner info">
            You're in <b>{me.purpose}</b> mode{me.session_status === "paused" ? " (seat held by another operator — request handover in Sessions)" : ""}. Viewing is read-only.
          </div>
        )}
        {pages[page]}
      </div>
    </>
  );
}

function Login({ onLogin }) {
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [purpose, setPurpose] = useState("operate");
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr(""); setInfo(""); setBusy(true);
    try {
      const r = await api("/auth/login", { method: "POST", body: { username, password, purpose } });
      auth.set(r.token);
      if (r.notice) setInfo(r.notice);
      onLogin();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="login-wrap">
      <div className="card">
        <h2>⛈ storm-hub — sign in</h2>
        {err && <div className="banner err">{err}</div>}
        {info && <div className="banner info">{info}</div>}
        <form onSubmit={submit}>
          <div className="field"><label>Username</label><input value={username} onChange={(e) => setU(e.target.value)} autoFocus /></div>
          <div className="field"><label>Password</label><input type="password" value={password} onChange={(e) => setP(e.target.value)} /></div>
          <div className="field">
            <label>Purpose</label>
            <select value={purpose} onChange={(e) => setPurpose(e.target.value)}>
              <option value="operate">Operate (control)</option>
              <option value="view">View (read-only)</option>
            </select>
          </div>
          <button className="btn" disabled={busy}>{busy ? "…" : "Sign in"}</button>
        </form>
        <p className="muted" style={{ marginTop: 12, fontSize: 13 }}>
          First run? Bootstrap an admin via <code>POST /auth/bootstrap</code> in <a href="/docs">/docs</a>.
        </p>
      </div>
    </div>
  );
}

function useList(path) {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const reload = () => api(path).then(setRows).catch((e) => setErr(e.message));
  useEffect(() => { reload(); }, [path]);
  return { rows, err, reload };
}

function Bookings({ me, events }) {
  const { rows, err, reload } = useList("/bookings");
  const [f, setF] = useState({ name: "", type: "test", posto_id: "2032", nationality: "CPV", residence: "CPV" });
  const [msg, setMsg] = useState("");
  const [sel, setSel] = useState(null);
  const canOperate = me.is_operating;

  async function create() {
    setMsg("");
    try { await api("/bookings", { method: "POST", body: f }); reload(); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <>
      <div className="card">
        <h2>New booking</h2>
        {msg && <div className="banner err">{msg}</div>}
        <div className="row" style={{ flexWrap: "wrap" }}>
          <div className="field"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
          <div className="field"><label>Type</label>
            <select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
              <option value="test">test</option><option value="real">real</option>
            </select></div>
          <div className="field"><label>Posto</label><input value={f.posto_id} onChange={(e) => setF({ ...f, posto_id: e.target.value })} style={{ width: 90 }} /></div>
          <div className="field"><label>Nat.</label><input value={f.nationality} onChange={(e) => setF({ ...f, nationality: e.target.value })} style={{ width: 70 }} /></div>
          <div className="field"><label>Res.</label><input value={f.residence} onChange={(e) => setF({ ...f, residence: e.target.value })} style={{ width: 70 }} /></div>
          <div className="field" style={{ alignSelf: "end" }}><button className="btn" disabled={!canOperate} onClick={create}>Create</button></div>
        </div>
        {!canOperate && <div className="muted">Operate seat required to create.</div>}
      </div>
      <div className="card">
        <h2>Bookings ({rows.length})</h2>
        {err && <div className="banner err">{err}</div>}
        <table><thead><tr><th>Name</th><th>Type</th><th>Posto</th><th>Status</th><th></th></tr></thead>
          <tbody>{rows.map((b) => (
            <tr key={b.id}><td>{b.name}</td><td><span className="pill">{b.type}</span></td><td>{b.posto_id}</td>
              <td>{b.status}</td>
              <td><button className="btn ghost" onClick={() => setSel(sel === b.id ? null : b.id)}>{sel === b.id ? "Hide" : "Workers"}</button></td></tr>
          ))}</tbody></table>
      </div>
      {sel && <BookingWorkers me={me} booking={rows.find((b) => b.id === sel)} events={events} />}
    </>
  );
}

function BookingWorkers({ me, booking, events }) {
  const workers = useList(`/bookings/${booking.id}/workers`);
  const identities = useList("/identities");
  const [attachId, setAttachId] = useState("");
  const [role, setRole] = useState("scout");
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  async function run(fn) { setMsg(""); try { await fn(); workers.reload(); } catch (e) { setMsg(e.message); } }
  const attach = () => run(() => api(`/bookings/${booking.id}/identities`,
    { method: "POST", body: { identity_id: attachId, assigned_role: booking.type === "test" ? role : undefined } }));

  return (
    <div className="card">
      <h2>Workers — {booking.name} <span className="muted">({booking.type} · posto {booking.posto_id})</span></h2>
      {msg && <div className="banner err">{msg}</div>}
      <div className="row" style={{ flexWrap: "wrap", marginBottom: 12 }}>
        <div className="field"><label>Attach identity</label>
          <select value={attachId} onChange={(e) => setAttachId(e.target.value)}>
            <option value="">— pick —</option>
            {identities.rows.filter((i) => booking.type === "real" || i.type === "test").map((i) => (
              <option key={i.id} value={i.id}>{i.full_name} ({i.type})</option>
            ))}
          </select></div>
        {booking.type === "test" && (
          <div className="field"><label>Role</label>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="scout">scout</option><option value="apply">apply</option>
            </select></div>
        )}
        <div className="field" style={{ alignSelf: "end" }}>
          <button className="btn" disabled={!op || !attachId} onClick={attach}>Attach → worker</button></div>
      </div>
      <table><thead><tr><th>Role</th><th>Status</th><th>Live substate</th><th>Actions</th></tr></thead>
        <tbody>{workers.rows.map((w) => {
          const live = events[w.id] || {};
          const substate = live.substate || w.substate || "—";
          return (
            <tr key={w.id}>
              <td><span className="pill">{w.role}</span></td>
              <td>{w.status}</td>
              <td className={live.outcome === "booked" ? "ok" : live.outcome === "failed" ? "err" : ""}>
                {substate}{live.outcome ? ` (${live.outcome})` : ""}
              </td>
              <td className="row">
                <button className="btn" disabled={!op} onClick={() => run(() => api(`/workers/${w.id}/launch`, { method: "POST" }))}>Launch</button>
                <button className="btn ghost" disabled={!op} onClick={() => run(() => api(`/workers/${w.id}/stop`, { method: "POST" }))}>Stop</button>
              </td>
            </tr>
          );
        })}</tbody></table>
      {workers.rows.length === 0 && <div className="muted">No workers yet — attach an identity above.</div>}
    </div>
  );
}

function Identities({ me }) {
  const { rows, err, reload } = useList("/identities");
  const [f, setF] = useState({ full_name: "", type: "test", passport_no: "", nationality: "CPV" });
  const [msg, setMsg] = useState("");
  async function create() {
    setMsg("");
    try { await api("/identities", { method: "POST", body: f }); reload(); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <>
      <div className="card">
        <h2>New identity</h2>
        {msg && <div className="banner err">{msg}</div>}
        <div className="row" style={{ flexWrap: "wrap" }}>
          <div className="field"><label>Full name</label><input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></div>
          <div className="field"><label>Type</label>
            <select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
              <option value="test">test</option><option value="real">real</option>
            </select></div>
          <div className="field"><label>Passport</label><input value={f.passport_no} onChange={(e) => setF({ ...f, passport_no: e.target.value })} /></div>
          <div className="field"><label>Nat.</label><input value={f.nationality} onChange={(e) => setF({ ...f, nationality: e.target.value })} style={{ width: 70 }} /></div>
          <div className="field" style={{ alignSelf: "end" }}><button className="btn" disabled={!me.is_operating} onClick={create}>Create</button></div>
        </div>
      </div>
      <ImportCsv me={me} path="/identities/import" label="Import identities CSV"
        hint="Columns: identity_id, first_name, last_name, traveldoc/passport_no, nationality, account_type/type, username, password, email, email_pass"
        makeBody={(csv) => ({ csv })} onDone={reload} />
      <div className="card">
        <h2>Identities ({rows.length})</h2>
        {err && <div className="banner err">{err}</div>}
        <table><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Passport</th><th>Nat.</th></tr></thead>
          <tbody>{rows.map((i) => (
            <tr key={i.id}><td>{i.full_name}</td><td><span className="pill">{i.type}</span></td><td>{i.status}</td>
              <td className="muted">{i.passport_no || "—"}</td><td>{i.nationality || "—"}</td></tr>
          ))}</tbody></table>
      </div>
    </>
  );
}

function ImportCsv({ me, path, label, hint, makeBody, onDone }) {
  const [text, setText] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  async function go() {
    setErr(""); setRes(null);
    try { setRes(await api(path, { method: "POST", body: makeBody(text) })); onDone && onDone(); }
    catch (e) { setErr(e.message); }
  }
  return (
    <div className="card">
      <h2>{label}</h2>
      {err && <div className="banner err">{err}</div>}
      {res && <div className="banner info">Created {res.created}{res.sub_accounts != null ? ` (+${res.sub_accounts} sub-accounts)` : ""}. {res.errors?.length ? `${res.errors.length} skipped/errors.` : ""}</div>}
      <div className="muted" style={{ marginBottom: 6, fontSize: 12 }}>{hint}</div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8 }}
        placeholder="paste CSV / lines here" />
      <div style={{ marginTop: 8 }}><button className="btn" disabled={!me.is_operating || !text.trim()} onClick={go}>Import</button></div>
    </div>
  );
}

function Proxies({ me }) {
  const { rows, err, reload } = useList("/proxies");
  const [prov, setProv] = useState("isp");
  const [kind, setKind] = useState("static_res");
  return (
    <>
      <div className="card">
        <h2>Import proxies</h2>
        <div className="row" style={{ marginBottom: 8 }}>
          <div className="field"><label>Provider</label>
            <select value={prov} onChange={(e) => setProv(e.target.value)}>
              <option value="isp">isp</option><option value="soax">soax</option>
            </select></div>
          <div className="field"><label>Kind</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="static_res">static_res</option><option value="datacenter">datacenter</option><option value="mobile">mobile</option>
            </select></div>
        </div>
        <ImportInline me={me} makeBody={(text) => ({ provider: prov, kind, text })} onDone={reload} />
      </div>
      <div className="card">
        <h2>Proxies ({rows.length})</h2>
        {err && <div className="banner err">{err}</div>}
        <table><thead><tr><th>Provider</th><th>Kind</th><th>Endpoint</th><th>Active</th><th>Max/IP</th></tr></thead>
          <tbody>{rows.map((p) => (
            <tr key={p.id}><td><span className="pill">{p.provider}</span></td><td>{p.kind}</td>
              <td className="muted">{p.endpoint}</td><td className={p.active ? "ok" : "muted"}>{String(p.active)}</td><td>{p.max_sessions}</td></tr>
          ))}</tbody></table>
      </div>
    </>
  );
}

function ImportInline({ me, makeBody, onDone }) {
  const [text, setText] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  async function go() {
    setErr(""); setRes(null);
    try { setRes(await api("/proxies/import", { method: "POST", body: makeBody(text) })); onDone && onDone(); }
    catch (e) { setErr(e.message); }
  }
  return (
    <>
      {err && <div className="banner err">{err}</div>}
      {res && <div className="banner info">Imported {res.created}. {res.errors?.length ? `${res.errors.length} skipped.` : ""}</div>}
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>One per line: <code>http://user:pass@host:port</code> or <code>host:port:user:pass</code></div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8 }} />
      <div style={{ marginTop: 8 }}><button className="btn" disabled={!me.is_operating || !text.trim()} onClick={go}>Import</button></div>
    </>
  );
}

function GlobalSettingsEditor({ me }) {
  const [current, setCurrent] = useState({});
  const [key, setKey] = useState("CAPSOLVER_KEYS");
  const [val, setVal] = useState("");
  const [msg, setMsg] = useState("");
  const reload = () => api("/settings/global").then(setCurrent).catch(() => {});
  useEffect(() => { reload(); }, []);
  async function save() {
    setMsg("");
    try { await api("/settings/global", { method: "PUT", body: { values: { [key]: val } } }); setVal(""); reload(); setMsg("saved"); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <div className="card">
      <h2>Global settings</h2>
      {msg && <div className={`banner ${msg === "saved" ? "info" : "err"}`}>{msg}</div>}
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        Stored in the DB (not env). Set <code>CAPSOLVER_KEYS</code> here; secret values are masked on read.
      </div>
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="field"><label>Key</label><input value={key} onChange={(e) => setKey(e.target.value)} style={{ width: 240 }} /></div>
        <div className="field grow"><label>Value</label><input value={val} onChange={(e) => setVal(e.target.value)} style={{ width: "100%" }} placeholder="value" /></div>
        <div className="field" style={{ alignSelf: "end" }}><button className="btn" disabled={!me.is_operating || !key || !val} onClick={save}>Save</button></div>
      </div>
      <table><thead><tr><th>Key</th><th>Value</th></tr></thead>
        <tbody>{Object.entries(current).map(([k, v]) => (
          <tr key={k}><td><code>{k}</code></td><td className="muted">{String(v)}</td></tr>
        ))}{Object.keys(current).length === 0 && <tr><td colSpan={2} className="muted">none set</td></tr>}</tbody></table>
    </div>
  );
}

function Settings({ me }) {
  const [cat, setCat] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api("/settings/catalog").then(setCat).catch((e) => setErr(e.message)); }, []);
  if (err) return <div className="banner err">{err}</div>;
  if (!cat) return <div className="muted">Loading…</div>;
  const layers = ["global", "booking", "template"];
  return (
    <>
      <GlobalSettingsEditor me={me} />
      <div className="card"><h2>Settings taxonomy</h2>
        <div className="muted">{cat._meta.total} settings · precedence: {cat._meta.precedence}</div></div>
      {layers.map((l) => (
        <div className="card" key={l}>
          <h2>{l} ({cat[l].length})</h2>
          <table><thead><tr><th>Key</th><th>Type</th><th>Default</th><th>Group</th><th>Override at</th></tr></thead>
            <tbody>{cat[l].map((s) => (
              <tr key={s.key}><td><code>{s.key}</code></td><td className="muted">{s.type}</td>
                <td className="muted">{String(s.default)}</td><td>{s.group}</td>
                <td className="muted">{s.override_layers.join(", ") || "—"}</td></tr>
            ))}</tbody></table>
        </div>
      ))}
    </>
  );
}

function Sessions({ me, onChange }) {
  const { rows, err, reload } = useList("/sessions");
  const [msg, setMsg] = useState("");
  async function act(fn) { setMsg(""); try { await fn(); reload(); onChange(); } catch (e) { setMsg(e.message); } }
  return (
    <div className="card">
      <h2>Active sessions</h2>
      {err && <div className="banner err">{err}</div>}
      {msg && <div className="banner err">{msg}</div>}
      <div className="row" style={{ marginBottom: 10 }}>
        {!me.is_operating && me.purpose === "operate" && (
          <button className="btn" onClick={() => act(() => api("/sessions/handover/request", { method: "POST" }))}>
            Request operate seat
          </button>
        )}
      </div>
      <table><thead><tr><th>User</th><th>Purpose</th><th>Status</th><th></th></tr></thead>
        <tbody>{rows.map((s) => (
          <tr key={s.session_id}><td>{s.user}</td><td><span className="pill">{s.purpose}</span></td><td>{s.status}</td>
            <td>{me.role === "admin" && s.purpose === "operate" && s.status === "active" && (
              <button className="btn ghost" onClick={() => act(() => api(`/sessions/${s.session_id}/force-release`, { method: "POST" }))}>Force-release</button>
            )}</td></tr>
        ))}</tbody></table>
    </div>
  );
}
