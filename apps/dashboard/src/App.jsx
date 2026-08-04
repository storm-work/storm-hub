import React, { useEffect, useState } from "react";
import { api, auth } from "./api.js";

export default function App() {
  const [me, setMe] = useState(null);
  const [ready, setReady] = useState(false);
  const [page, setPage] = useState("bookings");

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
    bookings: <Bookings me={me} />,
    identities: <Identities me={me} />,
    settings: <Settings />,
    sessions: <Sessions me={me} onChange={refreshMe} />,
  };

  return (
    <>
      <div className="topbar">
        <span className="brand">⛈ storm-hub</span>
        <nav className="nav">
          {["bookings", "identities", "settings", "sessions"].map((p) => (
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

function Bookings({ me }) {
  const { rows, err, reload } = useList("/bookings");
  const [f, setF] = useState({ name: "", type: "test", posto_id: "2032", nationality: "CPV", residence: "CPV" });
  const [msg, setMsg] = useState("");
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
        <table><thead><tr><th>Name</th><th>Type</th><th>Posto</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>{rows.map((b) => (
            <tr key={b.id}><td>{b.name}</td><td><span className="pill">{b.type}</span></td><td>{b.posto_id}</td>
              <td>{b.status}</td><td className="muted">{(b.created_at || "").slice(0, 19).replace("T", " ")}</td></tr>
          ))}</tbody></table>
      </div>
    </>
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

function Settings() {
  const [cat, setCat] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api("/settings/catalog").then(setCat).catch((e) => setErr(e.message)); }, []);
  if (err) return <div className="banner err">{err}</div>;
  if (!cat) return <div className="muted">Loading…</div>;
  const layers = ["global", "booking", "template"];
  return (
    <>
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
