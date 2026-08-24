import React, { useEffect, useRef, useState } from "react";
import { api, auth, openFile } from "./api.js";

// Live status over the single shared Hub WebSocket. Returns { byWorker, bySubAccount } --
// byWorker: worker_id -> {substate, logs[]} (booking workers); bySubAccount: sub_account_id ->
// {phase, outcome} (apps/registrar's reg_progress/reg_result events, keyed differently since a
// registration run has no Worker row -- see apps/hub/main.py's run_registration).
function useEvents(enabled) {
  const [byWorker, setByWorker] = useState({});
  const [bySubAccount, setBySubAccount] = useState({});
  const ref = useRef(null);
  useEffect(() => {
    if (!enabled) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    // The WebSocket constructor cannot set an Authorization header, so /ws/events takes the token as
    // a query parameter and validates it before accepting the socket.
    const ws = new WebSocket(
      `${proto}://${location.host}/ws/events?token=${encodeURIComponent(auth.token || "")}`);
    ref.current = ws;
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.sub_account_id && (m.type === "reg_progress" || m.type === "reg_result")) {
        setBySubAccount((prev) => {
          const cur = prev[m.sub_account_id] || {};
          const next = { ...cur };
          if (m.type === "reg_progress") next.phase = m.detail;
          if (m.type === "reg_result") { next.outcome = m.outcome; next.phase = m.outcome; }
          return { ...prev, [m.sub_account_id]: next };
        });
        return;
      }
      if (!m.worker_id) return;
      setByWorker((prev) => {
        const cur = prev[m.worker_id] || { substate: null, logs: [] };
        const next = { ...cur };
        // A `started` event is a NEW process for this worker id, so everything the previous life
        // reported is stale from this moment on. Nothing used to clear it, so a worker that once
        // terminated `failed` carried outcome:"failed" for the rest of the session -- and the row
        // then rendered the new process's live substate next to the dead one's outcome, e.g.
        // "logging_in (failed)". Confirmed 2026-08-20 on booking "Test 3018": six workers were
        // actively relaunching and re-attempting login (FELICIANO VARELA had 10 starts, ERNESTO
        // PIRES 10) while the dashboard showed every one of them permanently failed, which is what
        // made a fleet that was retrying look like a fleet that was stuck.
        if (m.type === "started") { next.outcome = null; next.substate = null; }
        if (m.type === "status_change" && m.substate) next.substate = m.substate;
        if (m.type === "terminal") { next.substate = m.substate || next.substate; next.outcome = m.outcome; }
        if (m.type === "log_line" && m.text) next.logs = [...cur.logs.slice(-100), m.text];
        return { ...prev, [m.worker_id]: next };
      });
    };
    return () => ws.close();
  }, [enabled]);
  return { byWorker, bySubAccount };
}

// Generic client-side search + click-to-sort for any row array. Small and reused across every
// table page instead of a bespoke filter per page (conversation_extract #224: "sort/filter/search
// by table or by column features for all tables").
function useSortSearch(rows, initialSort) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState(initialSort || null); // {key, dir}
  const filtered = q
    ? rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q.toLowerCase()))
    : rows;
  const sorted = sort
    ? [...filtered].sort((a, b) => {
        const av = a[sort.key], bv = b[sort.key];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        const cmp = av > bv ? 1 : av < bv ? -1 : 0;
        return sort.dir === "desc" ? -cmp : cmp;
      })
    : filtered;
  return { q, setQ, sort, setSort, rows: sorted };
}

function SearchBox({ q, setQ, placeholder = "search…" }) {
  return <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder}
    style={{ width: 200 }} />;
}

function Sortable({ label, k, sort, setSort, w }) {
  const active = sort && sort.key === k;
  const next = () => setSort(active && sort.dir === "asc" ? { key: k, dir: "desc" } : { key: k, dir: "asc" });
  return (
    <th style={{ cursor: "pointer", userSelect: "none", width: w }} onClick={next} title="click to sort">
      {label}{active ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
    </th>
  );
}

// The general page-structure pattern for fold-out side panels, from now on: a page never renders
// its own <SidePanel> -- App owns that (mounted in .app-body, outside .container, so it's a real
// full-height docked sidebar flush to the viewport edge, not a floating card next to the table). A
// page calls the setLeftPanel/setRightPanel callbacks App passes it: setLeftPanel({title, content})
// to open, setLeftPanel(null) to close. See Identities() for the reference usage.
function SidePanel({ side, title, onClose, children }) {
  // onClose omitted (e.g. Proxies' pool tree, which is permanent page navigation, not an on-demand
  // action panel like Identities'/Bookings' fold-outs) -> no × button, nothing to dismiss it with.
  // The fold/unfold toggle is independent of onClose -- available on every SidePanel (permanent nav
  // panels included), since collapsing to a thin rail is a viewing preference, not a dismissal.
  const [collapsed, setCollapsed] = useState(false);
  const foldGlyph = side === "left" ? (collapsed ? "»" : "«") : (collapsed ? "«" : "»");
  return (
    <div className={`side-panel ${side}${collapsed ? " collapsed" : ""}`}>
      <div className="side-panel-header">
        {!collapsed && <h3>{title}</h3>}
        <button className="panel-fold" onClick={() => setCollapsed(!collapsed)}
          title={collapsed ? "expand" : "collapse"}>{foldGlyph}</button>
        {!collapsed && onClose && <button className="panel-close" onClick={onClose} title="close">×</button>}
      </div>
      {!collapsed && children}
    </div>
  );
}

// Reusable transient dialog (as opposed to SidePanel's persistent docked panel) -- "dedicated
// actions" for something clicked, then gone. Closes on backdrop click or the × button.
function Modal({ title, onClose, children }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box" onClick={(e) => e.stopPropagation()}>
        <div className="side-panel-header">
          <h3>{title}</h3>
          <button className="panel-close" onClick={onClose} title="close">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [ready, setReady] = useState(false);
  const [page, setPage] = useState("bookings");
  const [leftPanel, setLeftPanel] = useState(null);   // {title, content} | null
  const [rightPanel, setRightPanel] = useState(null); // {title, content} | null
  const events = useEvents(!!me);

  async function refreshMe() {
    if (!auth.token) { setMe(null); setReady(true); return; }
    try { setMe(await api("/auth/whoami")); }
    catch { auth.clear(); setMe(null); }
    finally { setReady(true); }
  }
  useEffect(() => { refreshMe(); }, []);

  function goPage(p) {
    setPage(p);
    setLeftPanel(null);
    setRightPanel(null);
  }

  if (!ready) return <div className="container muted">Loading…</div>;
  if (!me) return <Login onLogin={refreshMe} />;

  // Pages a VIEWER may not reach. Everything here either mutates system-wide config (templates,
  // settings), exposes infrastructure/credential-adjacent detail (ops), or is an administrative
  // record (audit, sessions). A viewer keeps the run-watching pages: bookings, identities,
  // registration, proxies.
  //
  // Enforcement still lives on the server -- require_operator / the admin checks on those endpoints
  // are what actually protect them. This hides what a viewer cannot use, so the UI stops offering
  // dead ends; it is not the security boundary.
  const HIDDEN_FOR_VIEWER = new Set(["templates", "settings", "ops", "audit", "sessions"]);
  const visiblePages = ["bookings", "identities", "registration", "proxies",
                        "templates", "settings", "ops", "audit", "sessions"]
    .filter((p) => me.role !== "viewer" || !HIDDEN_FOR_VIEWER.has(p));

  const panelProps = { setLeftPanel, setRightPanel };
  const pages = {
    bookings: <Bookings me={me} events={events} {...panelProps} />,
    identities: <Identities me={me} {...panelProps} />,
    registration: <Registration me={me} events={events} />,
    proxies: <Proxies me={me} {...panelProps} />,
    templates: <Templates me={me} />,
    settings: <Settings me={me} />,
    ops: <Ops />,
    audit: <Audit />,
    sessions: <Sessions me={me} onChange={refreshMe} />,
  };

  return (
    <>
      <div className="topbar">
        <span className="brand">⛈ storm-hub</span>
        <nav className="nav">
          {visiblePages.map((p) => (
            <button key={p} className={page === p ? "active" : ""} onClick={() => goPage(p)}>
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
      <div className="app-body">
        {leftPanel && <SidePanel side="left" title={leftPanel.title}
          onClose={leftPanel.closable === false ? undefined : () => setLeftPanel(null)}>{leftPanel.content}</SidePanel>}
        <div className="container">
          {!me.is_operating && (
            <div className="banner info">
              You're in <b>{me.purpose}</b> mode{me.session_status === "paused" ? " (seat held by another operator — request handover in Sessions)" : ""}. Viewing is read-only.
            </div>
          )}
          {/* Never render a page the current role can't reach -- `page` is component state, so it
              can outlive a role change or point at a hidden page after a re-login. */}
          {visiblePages.includes(page) ? pages[page] : pages[visiblePages[0]]}
        </div>
        {rightPanel && <SidePanel side="right" title={rightPanel.title} onClose={() => setRightPanel(null)}>{rightPanel.content}</SidePanel>}
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
      </div>
    </div>
  );
}

function useList(path, { pollMs = 0 } = {}) {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const reload = () => { if (!path) return; api(path).then(setRows).catch((e) => setErr(e.message)); };
  // `pollMs`: re-fetch on a timer. Worker/booking STATUS comes from REST -- only the live substate
  // arrives over the WS -- so without this the page froze on whatever it loaded with. Confirmed
  // 2026-08-14: after clicking Start, the fleet launch correctly ran in the background for several
  // minutes while the table kept showing the pre-launch snapshot ("0 of 6 workers running,
  // failed: 5"), so a launch that was working looked like a launch that did nothing.
  useEffect(() => {
    if (!path) { setRows([]); return undefined; }
    reload();
    if (!pollMs) return undefined;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, pollMs]);
  return { rows, err, reload };
}

function Bookings({ me, events, setLeftPanel, setRightPanel }) {
  const { rows, err, reload } = useList("/bookings", { pollMs: 5000 });
  const [selectedId, setSelectedId] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const booking = rows.find((b) => b.id === selectedId) || null;
  // ONE fetch, shared by the overview's run summary and the Workers tab. They each used to poll
  // /bookings/{id}/workers on their own 5s timer, started at different moments -- so the two panels
  // could legitimately disagree by up to a full poll interval, which read as a status bug.
  const workers = useList(booking ? `/bookings/${booking.id}/workers` : null, { pollMs: 5000 });

  // The booking list is permanent page navigation (like Proxies' pool tree), not an on-demand
  // action panel -- foldable via SidePanel's collapse toggle, but never auto-dismissed.
  useEffect(() => {
    setLeftPanel({
      title: "Bookings",
      closable: false,
      content: <BookingListPanel me={me} bookings={rows} selectedId={selectedId}
        onSelect={setSelectedId} onNew={() => setShowCreate(true)} />,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, selectedId]);
  useEffect(() => () => setLeftPanel(null), []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      {err && <div className="banner err">{err}</div>}
      {showCreate && (
        <Modal title="New booking" onClose={() => setShowCreate(false)}>
          <BookingCreateForm me={me} onCreated={(id) => { setShowCreate(false); reload(); setSelectedId(id); }} />
        </Modal>
      )}
      {booking ? (
        <>
          <BookingOverview me={me} booking={booking} events={events} workers={workers}
            onBookingChanged={reload} onDeleted={() => { setSelectedId(null); reload(); }}
            onSelectBooking={(id) => { setSelectedId(id); reload(); }} />
          <BookingOpsTabs me={me} booking={booking} events={events} workers={workers}
            setRightPanel={setRightPanel} onBookingChanged={reload} />
        </>
      ) : (
        <div className="card muted">
          {rows.length === 0 ? "No bookings yet — create one from the sidebar." : "Select a booking from the sidebar."}
        </div>
      )}
    </>
  );
}

function BookingListPanel({ me, bookings, selectedId, onSelect, onNew }) {
  const [filter, setFilter] = useState("all"); // all | test | real
  const enriched = bookings.map((b) => ({ ...b, _date: b.warmup_start_at || b.created_at || "" }));
  const filtered = enriched.filter((b) => filter === "all" || b.type === filter);
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(filtered, { key: "_date", dir: "desc" });

  return (
    <>
      <button className="btn ghost" disabled={!me.is_operating} onClick={onNew} style={{ width: "100%", marginBottom: 10 }}>
        + New booking
      </button>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search…"
        style={{ width: "100%", marginBottom: 8 }} />
      <div className="field" style={{ marginBottom: 10 }}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "100%" }}>
          <option value="all">all types</option><option value="test">test</option><option value="real">real</option>
        </select>
      </div>
      <table className="table-compact">
        <thead><tr>
          <Sortable label="Name" k="name" sort={sort} setSort={setSort} w="46%" />
          <Sortable label="Date" k="_date" sort={sort} setSort={setSort} w="28%" />
          <Sortable label="Status" k="status" sort={sort} setSort={setSort} w="26%" />
        </tr></thead>
        <tbody>{shown.map((b) => (
          <tr key={b.id} className={`row-clickable${b.id === selectedId ? " row-selected" : ""}`} onClick={() => onSelect(b.id)}>
            <td>{b.name}</td>
            <td className="muted">{b._date ? b._date.slice(0, 10) : "—"}</td>
            <td className="muted">{b.status}</td>
          </tr>
        ))}</tbody>
      </table>
      {shown.length === 0 && <div className="muted" style={{ marginTop: 10, fontSize: 13 }}>No bookings match.</div>}
    </>
  );
}

function BookingCreateForm({ me, onCreated }) {
  const [f, setF] = useState({ name: "", type: "test", posto_id: "2032", nationality: "CPV", residence: "CPV",
                               warmup_start_at: "", slot_open_at: "" });
  const [msg, setMsg] = useState("");
  const canOperate = me.is_operating;

  async function create() {
    setMsg("");
    const body = { ...f };
    body.warmup_start_at = f.warmup_start_at ? new Date(f.warmup_start_at).toISOString() : null;
    body.slot_open_at = f.slot_open_at ? new Date(f.slot_open_at).toISOString() : null;
    try { const r = await api("/bookings", { method: "POST", body }); onCreated(r.id); }
    catch (e) { setMsg(e.message); }
  }

  return (
    <>
      {msg && <div className="banner err">{msg}</div>}
      <div className="field"><label>Name</label>
        <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Type</label>
        <select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })} style={{ width: "100%" }}>
          <option value="test">test</option><option value="real">real</option>
        </select></div>
      <div className="field"><label>Posto</label>
        <input value={f.posto_id} onChange={(e) => setF({ ...f, posto_id: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Nationality</label>
        <input value={f.nationality} onChange={(e) => setF({ ...f, nationality: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Residence</label>
        <input value={f.residence} onChange={(e) => setF({ ...f, residence: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Warmup start</label>
        <input type="datetime-local" value={f.warmup_start_at} onChange={(e) => setF({ ...f, warmup_start_at: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Slot-open</label>
        <input type="datetime-local" value={f.slot_open_at} onChange={(e) => setF({ ...f, slot_open_at: e.target.value })} style={{ width: "100%" }} /></div>
      {!canOperate && <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>Operate seat required. Leave times blank for manual/instant start.</div>}
      <button className="btn" disabled={!canOperate || !f.name} onClick={create} style={{ width: "100%" }}>Create</button>
    </>
  );
}

function isoToDatetimeLocal(iso) {
  return iso ? iso.slice(0, 16) : "";
}

// THE single place the dashboard decides what a worker is doing. Everything else (workers table,
// counts row, run summary, action buttons) reads this -- previously each computed its own blend of
// DB row + WS cache and they disagreed constantly.
//
// Two rules, both learned from confirmed bugs:
//  1. `events.byWorker` accumulates for the whole page session and is NEVER cleared, so it is only
//     trustworthy while the worker is CURRENTLY running. A worker that failed, was restarted, and
//     is now healthily holding otherwise renders forever as "holding (failed)" -- the substate half
//     of this was fixed 2026-08-13, the `outcome` half was still leaking through until 2026-08-14.
//  2. `substate` describes what a RUNNING worker is doing. For any other status it is meaningless,
//     and the backend now actively NULLs it on stop/restart/requeue/reap (apps/hub/main.py's
//     _set_worker_status). Old rows written before that fix can still carry a stale value, so this
//     drops it for non-running workers rather than trusting the column.
//
// Result: a non-running worker reports substate/outcome as null and renders "—". Its last-known
// context lives in `substate_detail`, which is shown as the row's tooltip.
function workerView(w, events) {
  const running = w.status === "running";
  const live = running ? (events.byWorker[w.id] || {}) : {};
  return {
    running,
    status: w.status,
    substate: running ? (live.substate || w.substate || null) : null,
    outcome: running ? (live.outcome || null) : null,
    detail: w.substate_detail || null,
  };
}

// Mirrors libs/domain/state_machines.py's WORKER_ACTION_STATUSES exactly -- keep the two in sync.
// The Hub enforces these (via _require_worker_action, 409 otherwise); this table exists so a
// disabled button always means "not applicable to this worker", never "will fail if you click it".
//
// Before this, each action invented its own rule and they contradicted each other: row Launch was
// `!running` (so it was live on COMPLETED workers that had already booked a real appointment, and
// launching one leaked a proxy lease), row Restart was unguarded entirely, bulk Launch/Detach both
// used a set called `selectedStopped` that actually meant "not running" (i.e. including READY and
// COMPLETED, despite Detach's tooltip claiming "stopped workers only"), and bulk Restart used every
// selected row. One table, used by both levels, is the fix.
const WORKER_ACTIONS = {
  launch:  ["ready", "stopped", "held", "failed"],
  stop:    ["running"],
  restart: ["ready", "running", "stopped", "held", "failed"],
  detach:  ["ready", "stopped", "held", "failed", "completed"],
};
const canWorkerAction = (action, status) => (WORKER_ACTIONS[action] || []).includes(status);

// Why a given action is unavailable, for the button tooltip -- "disabled and unexplained" was a
// large part of what made these controls feel arbitrary.
function actionTitle(action, status) {
  if (canWorkerAction(action, status)) return `${action} this worker`;
  if (status === "completed") return `worker already completed — cannot ${action}`;
  if (action === "stop") return "worker is not running";
  if (action === "detach") return "stop the worker first";
  return `cannot ${action} from status "${status}"`;
}

// The operator's requested run-summary layout (2026-08-14): 8 named phases in a fixed 2-column
// grid, always shown (including zeros) so the panel's shape stays stable at a glance rather than
// reflowing as workers move between phases. Left column = session lifecycle, right = what the
// worker is actually doing.
const RUN_SUMMARY_ROWS = [
  ["failed", "discovering"],
  ["logging_in", "polling"],
  ["warming_up", "applying"],
  ["holding", "booked"],
];

// `failed` and `booked` are STATUSES, not substates -- and counting them off substates (as the
// first version of this panel did for `failed`) shows a permanent 0: the backend NULLs substate on
// any non-running worker, a failed worker's status is FAILED with no substate, and a worker that
// books becomes COMPLETED. Both have to be read from status/outcome instead.
const STATUS_COUNTED = { failed: true, booked: true };

function countRunSummary(rows, events) {
  const counts = {};
  for (const w of rows) {
    const v = workerView(w, events);
    if (v.substate && !STATUS_COUNTED[v.substate]) {
      counts[v.substate] = (counts[v.substate] || 0) + 1;
    }
    // A booking is worth showing whether the worker is still finishing up (live outcome) or has
    // already exited COMPLETED -- either way this fleet has that many appointments.
    if (v.outcome === "booked" || w.status === "completed") {
      counts.booked = (counts.booked || 0) + 1;
    } else if (w.status === "failed") {
      counts.failed = (counts.failed || 0) + 1;
    }
  }
  return counts;
}

function BookingRunSummary({ workers, events }) {
  const rows = workers.rows;
  const counts = countRunSummary(rows, events);
  const active = rows.filter((w) => w.status === "running").length;
  const shown = new Set(RUN_SUMMARY_ROWS.flat());
  const otherPhases = Object.entries(counts).filter(([s, n]) => !shown.has(s) && n > 0);
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(150px, 220px))",
                    gap: "2px 24px", fontSize: 13 }}>
        {RUN_SUMMARY_ROWS.flat().map((s) => (
          <div key={s} className="row" style={{ gap: 6, justifyContent: "space-between" }}>
            <span className="muted">{s}:</span>
            <b style={{ opacity: counts[s] ? 1 : 0.35 }}>{counts[s] || 0}</b>
          </div>
        ))}
      </div>
      {/* The 8 rows above are the operator's fixed layout, but WorkerSubstate has 12 values --
          idle/logged_in/warmed/swapped have no row. countRunSummary still counts them, so those
          workers were being silently dropped from the tally and the phases visibly failed to add up
          to "N running" with no way to tell where the rest went. Shown only when non-zero, so the
          panel keeps its stable 8-row shape in the normal case. */}
      {otherPhases.length > 0 && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          also: {otherPhases.map(([s, n]) => `${s} ${n}`).join(", ")}
        </div>
      )}
      {/* Substates only exist for running workers, so an all-zero grid is ambiguous between "idle
          fleet" and "nothing running" -- say which. */}
      <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        {active} of {rows.length} worker{rows.length === 1 ? "" : "s"} running
      </div>
    </div>
  );
}

function BookingOverview({ me, booking, events, workers, onBookingChanged, onDeleted, onSelectBooking }) {
  const op = me.is_operating;
  const [msg, setMsg] = useState("");
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState(() => ({
    name: booking.name, type: booking.type, posto_id: booking.posto_id || "",
    nationality: booking.nationality || "", residence: booking.residence || "",
    warmup_start_at: isoToDatetimeLocal(booking.warmup_start_at), slot_open_at: isoToDatetimeLocal(booking.slot_open_at),
  }));

  useEffect(() => {
    setEditing(false);
    setEdit({
      name: booking.name, type: booking.type, posto_id: booking.posto_id || "",
      nationality: booking.nationality || "", residence: booking.residence || "",
      warmup_start_at: isoToDatetimeLocal(booking.warmup_start_at), slot_open_at: isoToDatetimeLocal(booking.slot_open_at),
    });
  }, [booking.id]);

  // `busy` disables every control while a mutation is in flight. Without it the Start button gave
  // no feedback at all: a live fleet launch used to run inline in the request (minutes of real
  // per-proxy browser probes), so the click just hung, and the operator reasonably clicked again --
  // eight times, each one stacking another concurrent fleet launch. The backend is idempotent now
  // (_begin_fleet_launch), but the UI should never look dead in the first place.
  const [busy, setBusy] = useState("");
  async function act(fn, label = "working") {
    setMsg(""); setBusy(label);
    try { await fn(); onBookingChanged(); }
    catch (e) { setMsg(e.message); }
    finally { setBusy(""); }
  }
  const startAll = () => act(async () => {
    const r = await api(`/bookings/${booking.id}/start`, { method: "POST" });
    if (r && r.already_launching) setMsg("A fleet launch is already running for this booking — leave it be.");
    else if (r && r.started) setMsg(`Launching ${r.started} worker(s) in the background — this takes a few minutes.`);
  }, "starting");
  const stopAll = () => act(() => api(`/bookings/${booking.id}/stop`, { method: "POST" }));
  const schedule = () => act(() => api(`/bookings/${booking.id}/schedule`, { method: "POST" }));
  const fireSlotOpen = () => act(() => api(`/bookings/${booking.id}/fire-slot-open`, { method: "POST" }));
  const fireSlotFound = () => act(() => api(`/bookings/${booking.id}/fire-slot-found`, { method: "POST" }));
  async function duplicateBooking() {
    setMsg("");
    try {
      const b = await api(`/bookings/${booking.id}/duplicate`, { method: "POST" });
      // Land ON the copy. It is a DRAFT with no identities, so the next step is always attaching
      // them -- and leaving the operator on the source booking invites editing the wrong one.
      // onBookingChanged is `reload` and takes no argument, so selecting needs its own callback.
      if (onSelectBooking) onSelectBooking(b.id); else onBookingChanged();
    } catch (e) { setMsg(e.message); }
  }

  async function deleteBooking() {
    if (!window.confirm(
      `Delete "${booking.name}" permanently? This stops and removes every worker, slot, and ` +
      `identity attachment on it. Cannot be undone. (A booking with a real booked appointment ` +
      `can't be deleted.)`
    )) return;
    setMsg("");
    try { await api(`/bookings/${booking.id}`, { method: "DELETE" }); onDeleted(); }
    catch (e) { setMsg(e.message); }
  }
  const saveEdit = () => act(async () => {
    await api(`/bookings/${booking.id}`, {
      method: "PUT",
      body: {
        name: edit.name, type: edit.type, posto_id: edit.posto_id, nationality: edit.nationality,
        residence: edit.residence,
        warmup_start_at: edit.warmup_start_at ? new Date(edit.warmup_start_at).toISOString() : null,
        slot_open_at: edit.slot_open_at ? new Date(edit.slot_open_at).toISOString() : null,
      },
    });
    setEditing(false);
  });

  // Scheduling/firing are only legal from specific booking statuses (the backend 409s otherwise --
  // these mirror those checks so a disabled button always means "not applicable right now", not a
  // guaranteed-to-fail click).
  const canFireSlotOpen = !booking.slot_open_fired_at && ["warming", "armed"].includes(booking.status);
  const canFireSlotFound = !!booking.slot_open_fired_at && !booking.slot_found_fired_at;
  const canStart = !["completed", "aborted"].includes(booking.status);
  const canStop = !["draft", "stopped", "completed", "aborted"].includes(booking.status);
  // STOPPED is schedulable: "stop the run, retime it, schedule tomorrow" is the ordinary way a
  // booking gets reused, and refusing it made auto-start unreachable after a booking's first run.
  // A past warmup time is also rejected -- it would fire on the very next tick, which is Start now
  // wearing a schedule's clothing.
  const warmupInFuture = !!booking.warmup_start_at
    && new Date(booking.warmup_start_at).getTime() > Date.now();
  const canSchedule = warmupInFuture
    && ["draft", "scheduled", "stopped"].includes(booking.status);

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8, flexWrap: "wrap", gap: 10 }}>
        {editing ? (
          <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "flex-end" }}>
            <div className="field" style={{ margin: 0 }}><label>Name</label>
              <input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></div>
            <div className="field" style={{ margin: 0 }}><label>Type</label>
              <select value={edit.type} onChange={(e) => setEdit({ ...edit, type: e.target.value })}
                      style={{ width: 108 }}>
                <option value="real">real</option>
                <option value="test">test</option>
              </select></div>
            {edit.type !== booking.type && (
              <div className="muted" style={{ fontSize: 12, flexBasis: "100%" }}>
                {edit.type === "test"
                  ? "→ test: only TEST identities may stay attached (real ones must be detached first), and you assign each role yourself."
                  : "→ real: every attached worker's role is re-derived — test identity becomes a scout, real identity becomes an applier."}
              </div>
            )}
            <div className="field" style={{ margin: 0 }}><label>Posto</label>
              <input value={edit.posto_id} onChange={(e) => setEdit({ ...edit, posto_id: e.target.value })}
                     style={{ width: 90 }} title="Changing this retires slots found at the old posto, and is refused while workers are RUNNING" /></div>
            <div className="field" style={{ margin: 0 }}><label>Nationality</label>
              <input value={edit.nationality} onChange={(e) => setEdit({ ...edit, nationality: e.target.value })} style={{ width: 90 }} /></div>
            <div className="field" style={{ margin: 0 }}><label>Residence</label>
              <input value={edit.residence} onChange={(e) => setEdit({ ...edit, residence: e.target.value })} style={{ width: 90 }} /></div>
            <div className="field" style={{ margin: 0 }}><label>Warmup start</label>
              <input type="datetime-local" value={edit.warmup_start_at} onChange={(e) => setEdit({ ...edit, warmup_start_at: e.target.value })} /></div>
            <div className="field" style={{ margin: 0 }}><label>Slot-open</label>
              <input type="datetime-local" value={edit.slot_open_at} onChange={(e) => setEdit({ ...edit, slot_open_at: e.target.value })} /></div>
            <button className="btn" disabled={!op || !edit.name} onClick={saveEdit}>Save</button>
            <button className="btn ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        ) : (
          <h2 style={{ margin: 0 }}>
            {booking.name} <span className="muted">({booking.type} · posto {booking.posto_id}
            {booking.nationality ? ` · ${booking.nationality}` : ""}{booking.residence ? `→${booking.residence}` : ""})</span>
            {op && <button className="btn ghost btn-sm" onClick={() => setEditing(true)} style={{ marginLeft: 8 }} title="edit name/nationality/residence/scheduling">✎ Edit</button>}
          </h2>
        )}
        <span className="grow" />
        <button className="btn" disabled={!op || !canStart || !!busy} onClick={startAll}
          title={!canStart ? `booking is ${booking.status}` : "launch ready workers now"}>
          {busy === "starting" ? "Starting…" : "▶ Start now"}</button>
        <button className="btn ghost" disabled={!op || !canSchedule || !!busy} onClick={schedule}
          title={!booking.warmup_start_at ? "set a warmup start time first"
                 : !warmupInFuture ? "warmup time is in the past — set a future time, or use Start now"
                 : ["draft", "scheduled", "stopped"].includes(booking.status)
                   ? `auto-start at ${new Date(booking.warmup_start_at).toLocaleString()}, `
                     + `then fire slot-open on its own schedule`
                   : `booking is ${booking.status}`}>
          {booking.status === "scheduled" ? "⏱ Rescheduled" : "⏱ Schedule run"}</button>
        <button className="btn ghost" disabled={!op || !canFireSlotOpen || !!busy} onClick={fireSlotOpen}
          title={booking.slot_open_fired_at ? "already fired" : "manually fire slot-open now"}>⚡ Fire slot-open</button>
        <button className="btn ghost" disabled={!op || !canFireSlotFound || !!busy} onClick={fireSlotFound}
          title={booking.slot_found_fired_at ? "already fired" : !booking.slot_open_fired_at ? "fire slot-open first" : "manually fire slot-found now"}>🔔 Fire slot-found</button>
        <button className="btn ghost" disabled={!op || !canStop || !!busy} onClick={stopAll} title={!canStop ? `booking is ${booking.status}` : "stop all running workers"}>■ Stop</button>
        <button className="btn ghost" disabled={!op || !!busy} onClick={duplicateBooking}
          title="create a DRAFT copy of this booking's configuration — identities are not carried over">⧉ Duplicate</button>
        <button className="btn ghost" disabled={!op} onClick={deleteBooking} title="permanently delete this booking and everything on it">🗑 Delete</button>
      </div>
      <div className="row muted" style={{ fontSize: 13, marginBottom: 6, gap: 14, flexWrap: "wrap" }}>
        <span>status: <b>{booking.status}</b></span>
        {booking.warmup_start_at && <span>warmup (scheduled)@{(booking.warmup_start_at || "").slice(0, 16).replace("T", " ")}Z</span>}
        {booking.slot_open_at && <span>slot-open (scheduled)@{(booking.slot_open_at || "").slice(0, 16).replace("T", " ")}Z</span>}
      </div>
      <div className="row" style={{ fontSize: 13, marginBottom: 12, gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <span className="muted">signal:</span>
        {!booking.slot_open_fired_at && !booking.slot_found_fired_at && <span className="pill">no-signal</span>}
        {booking.slot_open_fired_at && (
          <span className="pill ok">slot-open fired @{booking.slot_open_fired_at.slice(0, 16).replace("T", " ")}Z</span>
        )}
        {booking.slot_found_fired_at && (
          <span className="pill ok">slot-found fired @{booking.slot_found_fired_at.slice(0, 16).replace("T", " ")}Z</span>
        )}
      </div>
      <BookingRunSummary workers={workers} events={events} />
      {msg && <div className="banner err">{msg}</div>}
      <BookingShortcutSettings me={me} booking={booking} onSaved={onBookingChanged} />
    </div>
  );
}

// {provider, kind: ""} means "any kind of that provider" (PROXY_KIND/HOLD_PROXY_KIND blank = no
// filter, see apps/hub/main.py's `eff.get("...KIND") or None`). Falls back to the two known
// providers (soax/webshare) when no proxies are loaded yet, so the dropdown is never empty.
function derivePoolOptions(proxyRows) {
  const providers = [...new Set(proxyRows.map((p) => p.provider))].sort();
  if (providers.length === 0) return [{ provider: "soax", kind: "", label: "soax (any kind)" },
                                       { provider: "webshare", kind: "", label: "webshare (any kind)" }];
  const out = [];
  for (const provider of providers) {
    out.push({ provider, kind: "", label: `${provider} (any kind)` });
    const kinds = [...new Set(proxyRows.filter((p) => p.provider === provider).map((p) => p.kind))].sort();
    for (const kind of kinds) out.push({ provider, kind, label: `${provider} / ${kind}` });
  }
  return out;
}

// The overview's compact shortcut controls -- warmup/post-hold proxy pool + swap toggle. Saves
// immediately on change (no separate Save button), matching the "quick access, not a form" framing
// of a shortcut. The full grouped-by-category editor (every booking-settable key) lives in the
// Settings tab (BookingFullSettingsPanel) instead of duplicating a raw-JSON escape hatch here.
// Keep a settings editor in sync with the server WITHOUT destroying unsaved edits.
//
// Both settings panels used `useEffect(..., [booking.id, booking.settings_json])` to re-seed local
// state. `booking.settings_json` is a fresh object on every fetch, so the dep compares unequal by
// reference even when the content is byte-identical -- which was harmless while /bookings only
// refetched on an explicit action, and became destructive the moment it started polling every 5s
// (2026-08-14). An operator typing a date had it wiped from under them within five seconds, so the
// value was never in the form when they hit Save, and the setting "wouldn't stick".
//
// Rules: switching booking always re-seeds. Otherwise the server only re-seeds while the form is
// CLEAN -- an in-progress edit outranks a background refresh, and `dirty` clears on save.
function useServerSyncedSettings(booking) {
  const [settings, setSettings] = useState(() => ({ ...(booking.settings_json || {}) }));
  const [dirty, setDirty] = useState(false);
  const serverJson = JSON.stringify(booking.settings_json || {});
  const bookingId = booking.id;
  useEffect(() => { setSettings({ ...(booking.settings_json || {}) }); setDirty(false); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bookingId]);
  useEffect(() => {
    if (dirty) return;                       // never clobber what the operator is typing
    setSettings({ ...JSON.parse(serverJson) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverJson]);
  const edit = (updater) => { setDirty(true); setSettings(updater); };
  return { settings, setSettings, edit, dirty, markClean: () => setDirty(false) };
}


function BookingShortcutSettings({ me, booking, onSaved }) {
  const proxies = useList("/proxies");
  const { settings, setSettings } = useServerSyncedSettings(booking);
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  const pools = derivePoolOptions(proxies.rows);
  const poolValue = (provider, kind) => `${provider}|${kind}`;
  const warmupValue = poolValue(settings.PROXY_PROVIDER ?? "soax", settings.PROXY_KIND ?? "");
  const holdValue = poolValue(settings.HOLD_PROXY_PROVIDER ?? "soax", settings.HOLD_PROXY_KIND ?? "");
  const swapOn = !!settings.HOLD_SWAP_AFTER_FOUND;

  // PATCH, not PUT. This panel owns a handful of proxy/swap fields; it used to write back its own
  // snapshot of the WHOLE settings object, taken when the page loaded -- so toggling a proxy pool
  // silently wiped anything the full Settings tab had added since (confirmed 2026-08-14: a booking's
  // CAL_START_DATE/CAL_END_DATE vanished exactly this way). Send only the keys we actually own.
  async function saveFields(patch) {
    setMsg("");
    setSettings((prev) => ({ ...prev, ...patch }));
    try {
      const bk = await api(`/bookings/${booking.id}/settings`, { method: "PATCH", body: patch });
      setSettings({ ...(bk.settings_json || {}) });   // re-sync with what the server actually kept
      onSaved();
    } catch (e) { setMsg(e.message); }
  }
  function onPoolChange(providerKey, kindKey, value) {
    const [provider, kind] = value.split("|");
    saveFields({ [providerKey]: provider, [kindKey]: kind });
  }

  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 16, alignItems: "flex-end", borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      {msg && <div className="banner err" style={{ width: "100%" }}>{msg}</div>}
      <div className="field" style={{ margin: 0, minWidth: 220 }}>
        <label>Warmup phase proxy pool</label>
        <select value={warmupValue} disabled={!op} style={{ width: "100%" }}
          onChange={(e) => onPoolChange("PROXY_PROVIDER", "PROXY_KIND", e.target.value)}>
          {pools.map((p) => <option key={`w-${p.provider}-${p.kind}`} value={poolValue(p.provider, p.kind)}>{p.label}</option>)}
        </select>
      </div>
      <div className="field" style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" id="hold-swap-toggle" checked={swapOn} disabled={!op}
          onChange={(e) => saveFields({ HOLD_SWAP_AFTER_FOUND: e.target.checked })} />
        <label htmlFor="hold-swap-toggle" style={{ margin: 0 }}>Swap after warmup</label>
      </div>
      <div className="field" style={{ margin: 0, minWidth: 220 }}>
        <label>Post-hold phase proxy pool{!swapOn ? " (enabled when swap is on)" : ""}</label>
        <select value={holdValue} disabled={!op || !swapOn} style={{ width: "100%" }}
          onChange={(e) => onPoolChange("HOLD_PROXY_PROVIDER", "HOLD_PROXY_KIND", e.target.value)}>
          {pools.map((p) => <option key={`h-${p.provider}-${p.kind}`} value={poolValue(p.provider, p.kind)}>{p.label}</option>)}
        </select>
      </div>
    </div>
  );
}

function BookingOpsTabs({ me, booking, events, workers, setRightPanel, onBookingChanged }) {
  const [tab, setTab] = useState("workers");
  return (
    <div className="card">
      <div className="tabs">
        <button className={tab === "workers" ? "active" : ""} onClick={() => setTab("workers")}>Workers</button>
        <button className={tab === "slots" ? "active" : ""} onClick={() => setTab("slots")}>Slots</button>
        <button className={tab === "appointments" ? "active" : ""} onClick={() => setTab("appointments")}>Appointments</button>
        <button className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>Settings</button>
      </div>
      {tab === "workers" && <BookingWorkers me={me} booking={booking} events={events} workers={workers} setRightPanel={setRightPanel} />}
      {tab === "slots" && <BookingSlots booking={booking} me={me} />}
      {tab === "appointments" && <BookingAppointments booking={booking} />}
      {tab === "settings" && <BookingFullSettingsPanel me={me} booking={booking} onSaved={onBookingChanged} />}
    </div>
  );
}

function BookingSlots({ booking, me }) {
  const { rows, err, reload } = useList(`/bookings/${booking.id}/slots`, { pollMs: 5000 });
  // Default view hides CLOSED. "Clear" RETIRES slots rather than deleting them -- the row is live
  // state, not junk: rediscovery flips CLOSED -> AVAILABLE, and it carries the slot's history. But
  // showing retired rows at equal prominence made a successful Clear look like it had done nothing,
  // which is exactly how it was reported. TAKEN always shows: that is a real booking.
  const [statusFilter, setStatusFilter] = useState("live");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  // Slots are per-run OBSERVATIONS, and a stale pool makes the scheduler fire slot_found on the
  // next tick and skip discovery entirely. This drops one without restarting the run. Retires to
  // CLOSED rather than deleting (reopens on rediscovery), and never touches TAKEN -- that is a real
  // booking, not an observation.
  const clearable = rows.filter((s) => s.status !== "taken" && s.status !== "closed").length;
  const clearSlots = async () => {
    if (!window.confirm(
      `Clear ${clearable} slot(s) for "${booking.name}"?\n\n` +
      "They are retired to CLOSED, not deleted, and reopen if a scout rediscovers them. " +
      "Booked (taken) slots are left untouched.")) return;
    setBusy(true); setMsg("");
    try {
      const r = await api(`/bookings/${booking.id}/slots`, { method: "DELETE" });
      const taken = rows.filter((s) => s.status === "taken").length;
      setMsg(`Retired ${r.cleared} slot(s) to closed \u2014 they leave the pool but keep their `
        + `history, and reopen if a scout finds them again.`
        + (taken ? ` ${taken} booked slot(s) left untouched.` : ""));
      reload();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  };
  const closedCount = rows.filter((s) => s.status === "closed").length;
  const filtered = rows.filter((s) => (
    statusFilter === "all" ? true
      : statusFilter === "live" ? s.status !== "closed"
      : s.status === statusFilter));
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(filtered, { key: "date", dir: "asc" });
  const statuses = [...new Set(rows.map((s) => s.status))].sort();

  return (
    <>
      <div className="row" style={{ marginBottom: 10, flexWrap: "wrap", gap: 10 }}>
        <h3 style={{ margin: 0 }}>
          Slots found ({shown.length}/{rows.length})
          {statusFilter === "live" && closedCount > 0 && (
            <span className="muted" style={{ fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
              {closedCount} retired hidden
            </span>
          )}
        </h3>
        <span className="grow" />
        <SearchBox q={q} setQ={setQ} placeholder="search slots…" />
        <div className="field" style={{ margin: 0 }}><label>Status</label>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="live">live (hide retired)</option>
            <option value="all">all</option>
            {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
          </select></div>
        <button className="btn ghost" onClick={clearSlots}
                disabled={!me?.is_operating || busy || clearable === 0}
                title={clearable === 0 ? "nothing to clear (booked slots are never cleared)"
                                       : `retire ${clearable} slot(s) to CLOSED`}>
          {busy ? "Clearing…" : "Clear"}
        </button>
      </div>
      {msg && <div className="banner">{msg}</div>}
      {err && <div className="banner err">{err}</div>}
      <table className="table-compact"><thead><tr>
        <Sortable label="Date" k="date" sort={sort} setSort={setSort} w="20%" />
        <Sortable label="Period" k="period" sort={sort} setSort={setSort} w="12%" />
        <Sortable label="Status" k="status" sort={sort} setSort={setSort} w="16%" />
        <Sortable label="Capacity" k="capacity_estimate" sort={sort} setSort={setSort} w="14%" />
        <Sortable label="Misses" k="miss_count" sort={sort} setSort={setSort} w="12%" />
        <Sortable label="Last seen" k="last_seen" sort={sort} setSort={setSort} w="26%" />
      </tr></thead>
        <tbody>{shown.map((s) => (
          <tr key={s.id}>
            <td>{s.date}</td><td>{s.period}</td>
            <td><span className={`pill ${s.status === "available" ? "ok" : s.status === "taken" ? "err" : ""}`}>{s.status}</span></td>
            <td className="muted">{s.capacity_estimate ?? "—"}</td>
            <td className="muted">{s.miss_count}</td>
            <td className="muted">{s.last_seen ? s.last_seen.slice(0, 19).replace("T", " ") : "—"}</td>
          </tr>
        ))}</tbody></table>
      {rows.length === 0 && <div className="muted">No slots found yet.</div>}
    </>
  );
}

function BookingAppointments({ booking }) {
  // THIS RUN by default: appointments from previous runs are history, not this run's result, and
  // mixing them makes a run that booked nothing look like it booked three. The server does the
  // cut on booked_at >= started_at, and also resolves the identity name -- the dashboard used to
  // look that up in /identities, which no longer returns identities that have booked, and every
  // appointment is by definition one of those.
  const [allRuns, setAllRuns] = useState(false);
  const { rows, err } = useList(
    `/bookings/${booking.id}/appointments${allRuns ? "" : "?since_started=true"}`, { pollMs: 10000 });
  const { rows: allRows } = useList(`/bookings/${booking.id}/appointments`);
  const enriched = rows.map((a) => ({
    ...a, identity_name: a.identity_name || a.identity_id.slice(0, 8) }));
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(enriched, { key: "booked_at", dir: "desc" });
  const earlier = Math.max(0, allRows.length - rows.length);

  return (
    <>
      <div className="row" style={{ marginBottom: 10, flexWrap: "wrap", gap: 10 }}>
        <h3 style={{ margin: 0 }}>
          Appointments booked ({shown.length})
          <span className="muted" style={{ fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
            {allRuns ? "all runs" : "this run"}
          </span>
        </h3>
        <span className="grow" />
        {(earlier > 0 || allRuns) && (
          <button className="btn ghost btn-sm" onClick={() => setAllRuns(!allRuns)}>
            {allRuns ? "Show this run only" : `Show all runs (+${earlier} earlier)`}
          </button>
        )}
        <SearchBox q={q} setQ={setQ} placeholder="search appointments…" />
      </div>
      {err && <div className="banner err">{err}</div>}
      <table className="table-compact"><thead><tr>
        <Sortable label="Identity" k="identity_name" sort={sort} setSort={setSort} w="26%" />
        <Sortable label="Reference" k="appointment_ref" sort={sort} setSort={setSort} w="20%" />
        <Sortable label="Booked at" k="booked_at" sort={sort} setSort={setSort} w="26%" />
        <th style={{ width: "28%" }}>PDF</th>
      </tr></thead>
        <tbody>{shown.map((a) => (
          <tr key={a.id}>
            <td>{a.identity_name}</td>
            <td className="muted">{a.appointment_ref || "—"}</td>
            <td className="muted">{a.booked_at ? a.booked_at.slice(0, 19).replace("T", " ") : "—"}</td>
            <td>{a.pdf_artifact_id
              ? <button className="btn-sm btn ghost" onClick={() => openFile(`/artifacts/${a.pdf_artifact_id}/file`)}>📄 Open PDF</button>
              : <span className="muted">—</span>}</td>
          </tr>
        ))}</tbody></table>
      {rows.length === 0 && <div className="muted">No appointments booked yet.</div>}
    </>
  );
}

// Settings whose valid values are a small, CONFIRMED fixed set (traced to actual code that
// branches on them, or the catalog's own note -- not guessed) get a dropdown instead of free text.
// "" is a legitimate value for the two *_KIND keys (blank = no kind filter, see
// apps/hub/main.py's `eff.get("...KIND") or None`), not a placeholder/unset marker.
const _SETTING_SELECT_OPTIONS = {
  // Both providers are fully implemented (libs/engine/vendor/email_pool.py) and the registrar
  // switches on this in _mk_mail -- so it is genuinely a toggle, and typing the value by hand into
  // a free-text box was the only thing making it look like a setting you could get wrong.
  EMAIL_PROVIDER: ["mailtm", "cloudflare"],
  ENGINE_MODE: ["sim", "live"],
  PROXY_PROVIDER: ["soax", "webshare"],
  HOLD_PROXY_PROVIDER: ["soax", "webshare"],
  PROXY_KIND: ["", "rotating", "mobile"],
  HOLD_PROXY_KIND: ["", "rotating", "mobile"],
};
// Confirmed plain ISO dates (libs/engine/pool_manager.py's date.fromisoformat(CAL_START_DATE/...)).
const _SETTING_DATE_KEYS = new Set(["CAL_START_DATE", "CAL_END_DATE"]);
// Unconfirmed exact format (no current consumer) but named like every other _at timestamp in this
// app (warmup_start_at/slot_open_at), which all store a bare datetime-local string -- no ISO/UTC
// conversion here since that contract isn't established anywhere for these two.
const _SETTING_DATETIME_KEYS = new Set(["SIM_SLOTS_OPEN_AT", "SIMULATE_RELEASE_AT"]);
// settings_for_layer() (libs/domain/settings/catalog.py) sorts a group's fields alphabetically by
// key, which puts CAL_END_DATE before CAL_START_DATE ("E" < "S") -- confusing reading order for a
// date range. Explicit per-group order override for the handful of groups where alphabetical isn't
// the right reading order; every other group keeps the API's alphabetical order untouched.
const _SETTING_GROUP_ORDER = {
  date_window: { CAL_START_DATE: 0, CAL_END_DATE: 1, CAL_NUM_DAYS: 2 },
};

function BookingFullSettingsPanel({ me, booking, onSaved }) {
  const [catalog, setCatalog] = useState(null);
  const { settings, edit, dirty, markClean } = useServerSyncedSettings(booking);
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  useEffect(() => { api("/settings/catalog").then((c) => setCatalog(c.booking || [])).catch(() => setCatalog([])); }, []);

  if (catalog === null) return <div className="muted">Loading settings…</div>;

  const groups = {};
  for (const s of catalog) (groups[s.group || "other"] = groups[s.group || "other"] || []).push(s);
  for (const [group, list] of Object.entries(groups)) {
    const order = _SETTING_GROUP_ORDER[group];
    if (order) list.sort((a, b) => (order[a.key] ?? 99) - (order[b.key] ?? 99));
  }
  const groupNames = Object.keys(groups).sort();

  // undefined (not null/""/0/false) means "no override, unset" -- JSON.stringify drops undefined
  // keys, so saving with a cleared field removes that override and falls back to the true default,
  // rather than persisting a copy of whatever the default happened to be when the tab was opened.
  function setField(key, value) { edit((prev) => ({ ...prev, [key]: value })); }

  async function save() {
    setMsg("");
    try {
      await api(`/bookings/${booking.id}`, { method: "PUT", body: { settings } });
      markClean();          // re-enable server sync now that our edits are persisted
      onSaved();
      setMsg("saved");
    } catch (e) { setMsg(e.message); }
  }

  return (
    <>
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        Overrides GLOBAL for this booking only. {catalog.length} settings across {groupNames.length} groups.
        Empty fields show their default as a placeholder and use it until you set an explicit override.
      </div>
      {msg && <div className={`banner ${msg === "saved" ? "info" : "err"}`}>{msg}</div>}
      {groupNames.map((group) => (
        <details key={group} open style={{ marginBottom: 10, border: "1px solid var(--border)", borderRadius: 8, padding: "10px 14px" }}>
          <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13, textTransform: "uppercase", letterSpacing: "0.4px", color: "var(--muted)" }}>
            {group.replace(/_/g, " ")} <span className="muted" style={{ textTransform: "none", fontWeight: 400 }}>({groups[group].length})</span>
          </summary>
          <div className="row" style={{ flexWrap: "wrap", gap: 12, marginTop: 10 }}>
            {groups[group].map((s) => {
              const value = settings[s.key];
              const selectOptions = _SETTING_SELECT_OPTIONS[s.key];
              const placeholder = s.default != null ? String(s.default) : "";
              return (
                <div className="field" key={s.key} style={{ margin: 0, minWidth: 200 }} title={s.note}>
                  <label>{s.key}{s.home !== "booking" ? ` (${s.home} override)` : ""}</label>
                  {s.type === "bool" ? (
                    <input type="checkbox" checked={!!(value ?? s.default)} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.checked)} />
                  ) : selectOptions ? (
                    <select value={value ?? s.default ?? ""} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.value)} style={{ width: "100%" }}>
                      {selectOptions.map((opt) => <option key={opt} value={opt}>{opt || "(none / no filter)"}</option>)}
                    </select>
                  ) : _SETTING_DATE_KEYS.has(s.key) ? (
                    <input type="date" value={value ?? ""} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.value || undefined)} style={{ width: "100%" }} />
                  ) : _SETTING_DATETIME_KEYS.has(s.key) ? (
                    <input type="datetime-local" value={value ?? ""} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.value || undefined)} style={{ width: "100%" }} />
                  ) : s.type === "int" || s.type === "float" ? (
                    <input type="number" step={s.type === "float" ? "any" : "1"} value={value ?? ""} placeholder={placeholder} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.value === "" ? undefined : Number(e.target.value))} style={{ width: "100%" }} />
                  ) : (
                    <input type="text" value={value ?? ""} placeholder={placeholder} disabled={!op}
                      onChange={(e) => setField(s.key, e.target.value === "" ? undefined : e.target.value)} style={{ width: "100%" }} />
                  )}
                </div>
              );
            })}
          </div>
        </details>
      ))}
      {!op && (
        <div className="banner info" style={{ marginBottom: 10 }}>
          Read-only — you don't hold the operator seat, so these fields and Save are disabled.
          {" "}Take it from the <b>Sessions</b> tab (an idle seat is released automatically after 30
          minutes). Nothing you type here can be saved until then.
        </div>
      )}
      <button className="btn" disabled={!op} onClick={save} style={{ width: "100%", marginTop: 8 }}>
        {dirty ? "Save settings (unsaved changes)" : "Save settings"}
      </button>
      {dirty && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        Unsaved edits — this form has stopped refreshing from the server until you save.
      </div>}
    </>
  );
}

function BookingWorkers({ me, booking, events, workers, setRightPanel }) {
  const identities = useList("/identities");
  const templates = useList("/templates");
  const appointments = useList(`/bookings/${booking.id}/appointments`);
  const [msg, setMsg] = useState("");
  const [logsFor, setLogsFor] = useState(null);
  const [sel, setSel] = useState({});
  const [roleFilter, setRoleFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const op = me.is_operating;

  async function run(fn) { setMsg(""); try { await fn(); workers.reload(); } catch (e) { setMsg(e.message); } }
  // Messages this panel raises ITSELF (rather than catching) are not failures, and its only banner
  // is styled `err`. Same idiom the other panels here use: name the successes.
  const _INFO_MSGS = new Set(["checkpoint cleared", "no checkpoint to clear"]);

  const identityById = Object.fromEntries(identities.rows.map((i) => [i.id, i]));
  const pdfByIdentity = Object.fromEntries(appointments.rows.map((a) => [a.identity_id, a.pdf_artifact_id]));

  // A pure STATUS histogram -- one axis, nothing else spliced in. It used to read
  // `live.outcome === "booked" ? "booked" : w.status`, which mixed an *outcome* into a *status*
  // count (ungated on running, so it never expired either): the same booked worker was
  // simultaneously status=completed, substate=booked, and displayed here as "booked" -- three names
  // for one worker. Booked workers now show up as `completed`, and the Appointments tab is the
  // place that actually lists bookings.
  const counts = workers.rows.reduce((a, w) => {
    a[w.status] = (a[w.status] || 0) + 1; return a;
  }, {});

  // Flatten identity name + live substate/outcome onto each row so search/sort/filter work on
  // plain fields (matching the pattern used on Identities/Proxies), not a lookup done at render time.
  const enriched = workers.rows.map((w) => {
    const view = workerView(w, events);
    const ident = identityById[w.identity_id];
    return {
      ...w,
      identity_name: ident ? ident.full_name : w.identity_id.slice(0, 8),
      live_substate: view.substate || "—",
      live_outcome: view.outcome || "",
      substate_detail: view.detail || "",
      artifact_id: pdfByIdentity[w.identity_id] || null,
    };
  });
  const filtered = enriched.filter((w) =>
    (roleFilter === "all" || w.role === roleFilter) &&
    (statusFilter === "all" || w.status === statusFilter));
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(filtered, { key: "identity_name", dir: "asc" });

  const selectedIds = Object.keys(sel).filter((k) => sel[k]);
  // Bulk actions apply to exactly the selected rows the action is LEGAL for, using the same table
  // the row buttons and the Hub use. Selecting a mixed bag no longer silently 409s on some rows:
  // the button's own label says how many of the selection it will actually act on.
  const selectedFor = (action) =>
    shown.filter((w) => sel[w.id] && canWorkerAction(action, w.status)).map((w) => w.id);

  async function bulk(ids, fn) {
    setMsg("");
    try { await Promise.all(ids.map(fn)); workers.reload(); setSel({}); }
    catch (e) { setMsg(e.message); }
  }
  const bulkLaunch = () => bulk(selectedFor("launch"), (id) => api(`/workers/${id}/launch`, { method: "POST" }));
  const bulkStop = () => bulk(selectedFor("stop"), (id) => api(`/workers/${id}/stop`, { method: "POST" }));
  const bulkRestart = () => bulk(selectedFor("restart"), (id) => api(`/workers/${id}/restart`, { method: "POST" }));
  const bulkDetach = () => bulk(
    selectedFor("detach"),
    (id) => api(`/bookings/${booking.id}/identities/${shown.find((w) => w.id === id).identity_id}`, { method: "DELETE" })
  );

  function openAttachPanel() {
    setRightPanel({
      title: "Attach identities",
      content: <AttachPanel me={me} booking={booking} identities={identities.rows} templates={templates.rows}
        attached={new Set(workers.rows.map((w) => w.identity_id))}
        onAttached={() => { workers.reload(); }} />,
    });
  }

  const roles = [...new Set(workers.rows.map((w) => w.role))].sort();
  const statuses = [...new Set(workers.rows.map((w) => w.status))].sort();

  return (
    <>
      <div className="row muted" style={{ fontSize: 13, marginBottom: 8, gap: 14 }}>
        {Object.entries(counts).map(([k, v]) => <span key={k}>{k}: {v}</span>)}
      </div>
      {msg && <div className={`banner ${_INFO_MSGS.has(msg) ? "info" : "err"}`}>{msg}</div>}
      <div className="row" style={{ marginBottom: 10, flexWrap: "wrap", gap: 10 }}>
        <button className="btn ghost" disabled={!op} onClick={openAttachPanel}>+ Attach identities</button>
        <span className="grow" />
        <SearchBox q={q} setQ={setQ} placeholder="search workers…" />
        <div className="field" style={{ margin: 0 }}><label>Role</label>
          <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
            <option value="all">all</option>
            {roles.map((r) => <option key={r} value={r}>{r}</option>)}
          </select></div>
        <div className="field" style={{ margin: 0 }}><label>Status</label>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="all">all</option>
            {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
          </select></div>
      </div>
      {selectedIds.length > 0 && (
        <div className="banner info row" style={{ flexWrap: "wrap", gap: 8 }}>
          <span>{selectedIds.length} selected</span>
          <span className="grow" />
          {[["launch", bulkLaunch, "btn"], ["stop", bulkStop, "btn ghost"],
            ["restart", bulkRestart, "btn ghost"], ["detach", bulkDetach, "btn ghost"]].map(
            ([action, fn, cls]) => {
              const n = selectedFor(action).length;
              return (
                <button key={action} className={`btn-sm ${cls}`} disabled={!op || !n} onClick={fn}
                  title={n ? `${action} ${n} of ${selectedIds.length} selected`
                           : `none of the selected workers can be ${action}ed`}>
                  {action[0].toUpperCase() + action.slice(1)}{n ? ` (${n})` : ""}
                </button>
              );
            })}
        </div>
      )}
      <table className="table-compact">
        <thead><tr>
          <th style={{ width: 28 }}><input type="checkbox" checked={shown.length > 0 && shown.every((w) => sel[w.id])}
            onChange={(e) => { const s = {}; if (e.target.checked) shown.forEach((w) => (s[w.id] = true)); setSel(s); }} /></th>
          <Sortable label="Identity" k="identity_name" sort={sort} setSort={setSort} w="20%" />
          <Sortable label="Role" k="role" sort={sort} setSort={setSort} w="8%" />
          <Sortable label="Status" k="status" sort={sort} setSort={setSort} w="9%" />
          <Sortable label="Live substate" k="live_substate" sort={sort} setSort={setSort} w="16%" />
          <th style={{ width: "39%" }}>Actions</th>
        </tr></thead>
        <tbody>{shown.map((w) => {
          const live = events.byWorker[w.id] || {};   // log lines only -- status/substate come from workerView
          return (
            <React.Fragment key={w.id}>
              <tr>
                <td onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={!!sel[w.id]} onChange={(e) => setSel({ ...sel, [w.id]: e.target.checked })} />
                </td>
                <td>{w.identity_name}</td>
                <td><span className="pill">{w.role}</span></td>
                <td className={w.status === "completed" ? "ok" : w.status === "failed" ? "err" : ""}
                    title={w.substate_detail || ""}>{w.status}</td>
                <td className={w.live_outcome === "booked" ? "ok" : w.live_outcome === "failed" ? "err" : ""}
                    title={w.substate_detail || ""}>
                  {w.live_substate}{w.live_outcome ? ` (${w.live_outcome})` : ""}
                </td>
                <td className="row" style={{ flexWrap: "nowrap", gap: 4, overflowX: "auto" }}>
                  <button className="btn-sm btn" disabled={!op || !canWorkerAction("launch", w.status)}
                    title={actionTitle("launch", w.status)}
                    onClick={() => run(() => api(`/workers/${w.id}/launch`, { method: "POST" }))}>Launch</button>
                  <button className="btn-sm btn ghost" disabled={!op || !canWorkerAction("stop", w.status)}
                    title={actionTitle("stop", w.status)}
                    onClick={() => run(() => api(`/workers/${w.id}/stop`, { method: "POST" }))}>Stop</button>
                  <button className="btn-sm btn ghost" disabled={!op || !canWorkerAction("restart", w.status)}
                    title={actionTitle("restart", w.status)}
                    onClick={() => run(() => api(`/workers/${w.id}/restart`, { method: "POST" }))}>Restart</button>
                  <button className="btn-sm btn ghost" onClick={() => setLogsFor(logsFor === w.id ? null : w.id)}>
                    {logsFor === w.id ? "Hide log" : "Show log"}
                  </button>
                  {w.artifact_id && (
                    <button className="btn-sm btn ghost" onClick={() => openFile(`/artifacts/${w.artifact_id}/file`)}>📄 PDF</button>
                  )}
                  {/* Checkpoints are keyed by IDENTITY, and a bad one is self-sustaining: the worker
                      restores it, fails, and can restore the same dead cookies next launch ("checkpoint
                      restore: unusable ... doing a full re-login", repeatedly, 2026-08-21). Clearing it
                      used to mean an ad-hoc rm against var/checkpoints/ on the server. Enabled at ANY
                      status on purpose -- the file is only read at launch, so this never touches a live
                      session; the tooltip says when it is pointless rather than disabling it on a rule
                      that does not exist. */}
                  <button className="btn-sm btn ghost" disabled={!op}
                    title={w.status === "running"
                      ? "clear the saved session checkpoint -- takes effect on the NEXT launch (a running worker will save a new one)"
                      : "clear the saved session checkpoint so the next launch does a full fresh login"}
                    onClick={() => run(async () => {
                      const r = await api(`/workers/${w.id}/checkpoint`, { method: "DELETE" });
                      setMsg(r.cleared ? "checkpoint cleared" : "no checkpoint to clear");
                    })}>
                    Clear checkpoint
                  </button>
                  <button className="btn-sm btn ghost" disabled={!op || !canWorkerAction("detach", w.status)}
                    onClick={() => run(() => api(`/bookings/${booking.id}/identities/${w.identity_id}`, { method: "DELETE" }))}
                    title={actionTitle("detach", w.status)}>
                    Detach
                  </button>
                </td>
              </tr>
              {logsFor === w.id && (
                <tr><td colSpan={6}><WorkerLog workerId={w.id} liveLogs={live.logs} /></td></tr>
              )}
            </React.Fragment>
          );
        })}</tbody></table>
      {workers.rows.length === 0 && <div className="muted">No workers yet — attach identities above.</div>}
      {workers.rows.length > 0 && shown.length === 0 && <div className="muted">No workers match the current filter/search.</div>}
    </>
  );
}

// Per-worker log: seeded from the Hub's replay of the shared event stream, then extended with
// whatever arrives live over the WS. The view used to be WS-ONLY, so a worker that did its work
// before the tab connected showed "no log lines received over the WS yet" while its whole history
// sat in Redis -- i.e. the log was blank exactly when you needed it to explain something that had
// already happened.
function WorkerLog({ workerId, liveLogs }) {
  const [history, setHistory] = useState(null);   // null = still loading
  const [err, setErr] = useState("");
  // Scoped to the CURRENT booking run by default. A worker ROW is reused by every /start, and the
  // event stream is keyed only by worker_id, so this used to interleave every past run's lines with
  // no way to tell them apart.
  const [allRuns, setAllRuns] = useState(false);
  const [hidden, setHidden] = useState(0);
  useEffect(() => {
    let alive = true;
    api(`/workers/${workerId}/logs${allRuns ? "?all_runs=true" : ""}`)
      .then((r) => {
        if (!alive) return;
        setHistory((r.lines || []).map((l) => l.text));
        setHidden(r.hidden_from_earlier_runs || 0);
      })
      .catch((e) => { if (alive) { setHistory([]); setErr(e.message); } });
    return () => { alive = false; };
  }, [workerId, allRuns]);

  // The tail of `history` and the head of `liveLogs` overlap for a worker that was already emitting
  // when the tab connected -- drop live lines already present at the end of history.
  const hist = history || [];
  const live = liveLogs || [];
  let overlap = 0;
  for (let n = Math.min(hist.length, live.length); n > 0; n--) {
    if (hist.slice(-n).join("\u0000") === live.slice(0, n).join("\u0000")) { overlap = n; break; }
  }
  const lines = [...hist, ...live.slice(overlap)];

  return (
    <div style={{ background: "var(--panel2)", border: "1px solid var(--border)", borderRadius: 8,
                  padding: 8, maxHeight: 220, overflowY: "auto", fontFamily: "monospace", fontSize: 12 }}>
      {err && <div className="err">could not load history: {err}</div>}
      {history === null ? <span className="muted">loading log…</span>
        : lines.length ? lines.map((l, i) => <div key={i}>{l}</div>)
        : <span className="muted">
            {allRuns ? "no log lines for this worker yet"
                     : "no log lines yet in this run"}
          </span>}
      {(hidden > 0 || allRuns) && (
        <div className="muted" style={{ marginTop: 6, paddingTop: 6,
                                        borderTop: "1px solid var(--border)", fontFamily: "inherit" }}>
          {allRuns
            ? <>showing <b>every run</b> of this worker · </>
            : <>{hidden} line{hidden === 1 ? "" : "s"} from earlier runs hidden · </>}
          <a href="#" onClick={(e) => { e.preventDefault(); setAllRuns(!allRuns); }}>
            {allRuns ? "show only this run" : "show all runs"}
          </a>
        </div>
      )}
    </div>
  );
}


function AttachPanel({ me, booking, identities, templates, attached, onAttached }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState({});
  const [role, setRole] = useState("scout");
  const [showRole, setShowRole] = useState("all");
  const [tplId, setTplId] = useState("");
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  // Eligible = right type for this booking, not already attached HERE, not allocated to another
  // ACTIVE booking (C2 exclusivity -- attach_identity 409s on those, and listing them meant the
  // operator only found out by picking one), AND has a usable sub-account (status "active" means
  // >=1 sub-account exists per IdentityStatus's own definition) -- conversation_extract #269:
  // not-ready identities (zero verified sub-accounts) were showing up here before.
  // Which role each identity will actually get. On a REAL booking the Role select is not shown and
  // `assigned_role` is IGNORED by the server -- resolve_worker_role derives it from identity type
  // (test -> scout, real -> apply). That rule was invisible here: the operator picked identities
  // with no way to know whether they were about to create scouts or appliers, and on a real booking
  // the only lever is the identity's own type. Mirrors libs/domain/rules.py:resolve_worker_role.
  const roleFor = (i) => (booking.type === "test" ? role : (i.type === "test" ? "scout" : "apply"));
  // Search matches passport OR name. The table shows the passport (operator, 2026-08-21) because
  // that is the identifier that appears on the portal side and in a booked appointment, and names
  // repeat across identities ("RICARDO FERREIRA" / "RICARDO MATOS" / "RICARDO RONALDO CORREIA...");
  // the passport is the one field guaranteed to identify a row. Name stays searchable so an operator
  // who only remembers the name is not stuck.
  const matchesSearch = (i) => {
    if (!q) return true;
    const needle = q.toLowerCase();
    return (i.passport_no || "").toLowerCase().includes(needle)
        || (i.full_name || "").toLowerCase().includes(needle);
  };
  const forThisBooking = identities.filter(
    (i) => (booking.type === "real" || i.type === "test") && matchesSearch(i));
  // Role filter. On a REAL booking role is DERIVED (test -> scout, real -> apply), so this narrows
  // the list to the identities that will actually become the role you are staffing -- picking scouts
  // for a real booking previously meant eyeballing the Type column across the whole list. On a TEST
  // booking `role` is an ASSIGNMENT applied to everything selected, not a filter, so it is not
  // applied here.
  const eligible = forThisBooking.filter(
    (i) => !attached.has(i.id) && !i.allocated_booking_id && i.status === "active"
           && (booking.type === "test" || showRole === "all" || roleFor(i) === showRole));

  // An identity attached ANYWHERE is not mentioned at all (operator, 2026-08-21). It was already
  // excluded from the selectable list -- `attached` covers this booking, `allocated_booking_id`
  // covers every other one, and the backend keys that on an unreleased allocation regardless of the
  // holding booking's status. What remained was a "Not shown" footer that still counted and NAMED
  // them, which is the part being removed: for an operator picking who to attach, an identity that
  // is already spoken for is noise wherever it is spoken for.
  //
  // The one exclusion still worth reporting is "no usable sub-account", because that is the only
  // one the operator can DO something about (register more sub-accounts) rather than simply live
  // with. Counting it also keeps the panel from silently showing an empty list with no explanation.
  const hiddenNotReady = forThisBooking.filter(
    (i) => !attached.has(i.id) && !i.allocated_booking_id && i.status !== "active").length;

  const selectedIds = Object.keys(sel).filter((k) => sel[k]);

  async function attachSelected() {
    setMsg("");
    try {
      for (const identity_id of selectedIds) {
        await api(`/bookings/${booking.id}/identities`, {
          method: "POST",
          body: { identity_id, assigned_role: booking.type === "test" ? role : undefined,
                  template_id: tplId || undefined },
        });
      }
      setSel({});
      onAttached();
    } catch (e) { setMsg(e.message); }
  }

  return (
    <>
      <div className="field"><SearchBox q={q} setQ={setQ} placeholder="filter by passport or name…" /></div>
      {booking.type === "real" && (
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
          Role is derived from identity type on a real booking — test identities become scouts,
          real identities become appliers. The Role column below shows what each one will get.
        </div>
      )}
      {booking.type === "real" && (
        <div className="field"><label>Show</label>
          <select value={showRole} onChange={(e) => { setShowRole(e.target.value); setSel({}); }}
                  style={{ width: "100%" }}>
            <option value="all">all eligible</option>
            <option value="scout">scouts only (test identities)</option>
            <option value="apply">appliers only (real identities)</option>
          </select></div>
      )}
      {booking.type === "test" && (
        <div className="field"><label>Role</label>
          <select value={role} onChange={(e) => setRole(e.target.value)} style={{ width: "100%" }}>
            <option value="scout">scout</option><option value="apply">apply</option>
          </select></div>
      )}
      <div className="field"><label>Template</label>
        <select value={tplId} onChange={(e) => setTplId(e.target.value)} style={{ width: "100%" }}>
          <option value="">auto (by role)</option>
          {templates.map((t) => (<option key={t.id} value={t.id}>{t.name} ({t.role})</option>))}
        </select></div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>{selectedIds.length} selected · {eligible.length} eligible</div>
      <button className="btn" disabled={!op || selectedIds.length === 0} onClick={attachSelected} style={{ width: "100%", marginBottom: 12 }}>
        Attach {selectedIds.length > 0 ? `(${selectedIds.length})` : ""}
      </button>
      {msg && <div className="banner err">{msg}</div>}
      <table>
        <thead><tr>
          <th style={{ width: 28 }}><input type="checkbox" checked={eligible.length > 0 && eligible.every((i) => sel[i.id])}
            onChange={(e) => { const s = {}; if (e.target.checked) eligible.forEach((i) => (s[i.id] = true)); setSel(s); }} /></th>
          {/* px, not %: this table lives in a narrow side panel where 16% is ~40px -- less than
              the word "apply" -- so the pills had nothing to sit in. Passport takes what is left. */}
          <th>Passport</th><th style={{ width: 74 }}>Type</th><th style={{ width: 74 }}>Role</th>
        </tr></thead>
        <tbody>{eligible.map((i) => (
          <tr key={i.id}>
            <td><input type="checkbox" checked={!!sel[i.id]} onChange={(e) => setSel({ ...sel, [i.id]: e.target.checked })} /></td>
            <td title={i.full_name}>{i.passport_no || <span className="muted">— none —</span>}</td>
            <td><span className="pill">{i.type}</span></td>
            <td><span className="pill">{roleFor(i)}</span></td>
          </tr>
        ))}</tbody>
      </table>
      {eligible.length === 0 && (
        <div className="muted" style={{ padding: 8 }}>
          {booking.type === "real" && showRole === "scout"
            ? "No test identities available. A real booking's scouts come from TEST identities, so "
              + "generate more or free some before staffing scouts."
            : "No eligible not-yet-attached identities with a usable sub-account."}
        </div>
      )}
      {hiddenNotReady > 0 && (
        <div className="muted" style={{ fontSize: 12, padding: "8px 4px", borderTop: "1px solid var(--border)", marginTop: 8 }}>
          Not shown: {hiddenNotReady} without a usable sub-account.
        </div>
      )}
    </>
  );
}

// Compact "3 new + 2 verified" style summary instead of the coarse Identity.status alone -- built
// client-side from /sub-accounts (already fetched for the Registration page; no backend change
// needed) rather than a new aggregate endpoint.
function summarizeSubAccounts(subs) {
  if (!subs || subs.length === 0) return "no accounts";
  const counts = {};
  for (const s of subs) counts[s.status] = (counts[s.status] || 0) + 1;
  return Object.entries(counts).map(([status, n]) => `${n} ${status}`).join(" + ");
}



// Collapsible card. The Identities page carries two full tables now (all identities + booked), and
// on a normal screen the second one is permanently below the fold; being able to fold either away
// is what makes both usable at once. Open/closed persists per key, so a chosen layout survives
// navigation and reloads rather than resetting every visit.
function Collapsible({ storageKey, title, count, right, children, defaultOpen = true }) {
  const [open, setOpen] = useState(() => {
    const v = localStorage.getItem(`collapse:${storageKey}`);
    return v === null ? defaultOpen : v === "1";
  });
  const toggle = () => {
    setOpen((o) => {
      localStorage.setItem(`collapse:${storageKey}`, o ? "0" : "1");
      return !o;
    });
  };
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: open ? 10 : 0, flexWrap: "wrap", gap: 10 }}>
        <h2 style={{ margin: 0, cursor: "pointer", userSelect: "none" }} onClick={toggle}
            title={open ? "collapse" : "expand"}>
          <span style={{ display: "inline-block", width: 16, opacity: 0.6 }}>{open ? "▾" : "▸"}</span>
          {title}{count != null && ` (${count})`}
        </h2>
        <span className="grow" />
        {open && right}
      </div>
      {open && children}
    </div>
  );
}

// ── Booked identities ─────────────────────────────────────────────────────────────────────────
// One row per IDENTITY, never per sub-account: an identity is a PERSON, its sub-accounts are just
// login attempts on their behalf, so "if any one of the sub-accounts booked, that identity is
// mission completed" (operator, 2026-08-19). A second appointment for the same person is a defect.
function BookedIdentities({ me }) {
  const [type, setType] = useState("all");
  const [posto, setPosto] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [sel, setSel] = useState({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const qs = new URLSearchParams();
  if (type !== "all") qs.set("type", type);
  if (posto) qs.set("posto_id", posto);
  if (from) qs.set("date_from", `${from}T00:00:00`);
  if (to) qs.set("date_to", `${to}T23:59:59`);
  const { rows, err } = useList(`/identities/booked${qs.toString() ? "?" + qs : ""}`,
                                { pollMs: 15000 });
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(rows, { key: "booked_at", dir: "desc" });
  const selectedIds = Object.keys(sel).filter((k) => sel[k]);
  const postos = [...new Set(rows.map((r) => r.posto_id))].sort();

  // Admin only, and it is the one place stored passwords leave the server -- the appointment is
  // useless without the credentials to log in and manage it, but that is not an operator's to take.
  async function download() {
    setMsg(""); setBusy(true);
    try {
      const r = await api("/identities/booked/credentials",
                          { method: "POST", body: { identity_ids: selectedIds } });
      const head = "full_name,username,password,posto_id,booking,booked_at";
      const csv = [head, ...r.rows.map((x) => [x.full_name, x.username, x.password,
        x.posto_id, x.booking, x.booked_at || ""].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))].join("\n");
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `booked-credentials-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setMsg(`Downloaded ${r.count} credential row(s).`);
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  }

  const controls = (
    <>
        <SearchBox q={q} setQ={setQ} placeholder="search booked…" />
        <div className="field" style={{ margin: 0 }}><label>Type</label>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="all">all</option><option value="real">real</option>
            <option value="test">test</option>
          </select></div>
        <div className="field" style={{ margin: 0 }}><label>Posto</label>
          <select value={posto} onChange={(e) => setPosto(e.target.value)}>
            <option value="">all</option>
            {postos.map((p) => <option key={p} value={p}>{p}</option>)}
          </select></div>
        <div className="field" style={{ margin: 0 }}><label>Booked from</label>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
        <div className="field" style={{ margin: 0 }}><label>to</label>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></div>
        {me.role === "admin" && (
          <button className="btn ghost" disabled={!selectedIds.length || busy} onClick={download}
                  title={!selectedIds.length ? "select rows to export"
                         : `download username + password for ${selectedIds.length} identity/identities`}>
            {busy ? "Preparing…" : `⬇ Credentials (${selectedIds.length})`}
          </button>
        )}
    </>
  );

  return (
    <Collapsible storageKey="identities-booked" title="Booked"
                 count={`${shown.length}/${rows.length}`} right={controls}>
      {err && <div className="banner err">{err}</div>}
      {msg && <div className="banner">{msg}</div>}
      <table className="table-compact"><thead><tr>
        {me.role === "admin" && <th style={{ width: "3%" }}>
          <input type="checkbox"
                 checked={!!shown.length && shown.every((r) => sel[r.identity_id])}
                 onChange={(e) => setSel(e.target.checked
                   ? Object.fromEntries(shown.map((r) => [r.identity_id, true])) : {})} /></th>}
        <Sortable label="Identity" k="full_name" sort={sort} setSort={setSort} w="22%" />
        <Sortable label="Type" k="type" sort={sort} setSort={setSort} w="7%" />
        <Sortable label="Booked sub-account" k="sub_account_username" sort={sort} setSort={setSort} w="16%" />
        <Sortable label="Posto" k="posto_id" sort={sort} setSort={setSort} w="8%" />
        <Sortable label="Appointment" k="slot_date" sort={sort} setSort={setSort} w="14%" />
        <Sortable label="Booked at" k="booked_at" sort={sort} setSort={setSort} w="16%" />
        <th style={{ width: "8%" }}>PDF</th></tr></thead>
        <tbody>{shown.map((r) => (
          <tr key={r.identity_id}>
            {me.role === "admin" && <td><input type="checkbox" checked={!!sel[r.identity_id]}
              onChange={(e) => setSel({ ...sel, [r.identity_id]: e.target.checked })} /></td>}
            <td>{r.full_name}</td>
            <td><span className="pill">{r.type}</span></td>
            <td className="muted">{r.sub_account_username}</td>
            <td>{r.posto_id}</td>
            <td>{r.slot_date ? `${r.slot_date} p${r.slot_period}` : "—"}</td>
            <td className="muted">{r.booked_at ? r.booked_at.slice(0, 19).replace("T", " ") : "—"}</td>
            <td>{r.pdf_artifact_id
              ? <button className="btn ghost btn-sm"
                        onClick={() => openFile(`/artifacts/${r.pdf_artifact_id}/file`)}>open</button>
              : <span className="muted">—</span>}</td>
          </tr>
        ))}</tbody></table>
      {rows.length === 0 && <div className="muted">No booked identities yet.</div>}
    </Collapsible>
  );
}

function Identities({ me, setLeftPanel, setRightPanel }) {
  const { rows, err, reload } = useList("/identities");
  const { rows: subRows, reload: reloadSubs } = useList("/sub-accounts");
  const [selectedId, setSelectedId] = useState(null);  // visual highlight only; panel content lives in App
  const [sel, setSel] = useState({});           // id -> bool (bulk multi-select, unrelated to selectedId)
  const [filter, setFilter] = useState("all");  // all | test | real
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [subCount, setSubCount] = useState(3);
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  const typeAndStatus = rows.filter((i) => filter === "all" ? true : i.type === filter);
  const dateFiltered = typeAndStatus.filter((i) => {
    if (!i.created_at) return true;
    const d = i.created_at.slice(0, 10);
    if (dateFrom && d < dateFrom) return false;
    if (dateTo && d > dateTo) return false;
    return true;
  });
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(dateFiltered, { key: "created_at", dir: "desc" });

  const subsByIdentity = {};
  for (const s of subRows) (subsByIdentity[s.identity_id] = subsByIdentity[s.identity_id] || []).push(s);

  const selectedIds = Object.keys(sel).filter((k) => sel[k]);
  async function act(fn) { setMsg(""); try { await fn(); setSel({}); reload(); reloadSubs(); } catch (e) { setMsg(e.message); } }
  const genSubs = () => act(() => api("/identities/gen-subaccounts",
    { method: "POST", body: { identity_ids: selectedIds, count: Number(subCount) } }));

  function openCreatePanel() {
    // reload() alone left the Sub-accounts column stale after Generate/Import (which create
    // sub-accounts server-side) until a manual page refresh -- looked like 0 sub-accounts were
    // created when they actually were.
    setLeftPanel({
      title: "Add / import",
      content: <IdentityCreatePanel me={me} onDone={() => { reload(); reloadSubs(); }} />,
    });
  }
  function openDetailPanel(i) {
    setSelectedId(i.id);
    setRightPanel({
      title: i.full_name,
      content: <IdentityDetailPanel i={i} op={op} subs={subsByIdentity[i.id] || []}
        onChanged={() => { reload(); reloadSubs(); }} onClose={() => { setRightPanel(null); setSelectedId(null); }} />,
    });
  }

  const identityControls = (
    <>
        <button className="btn ghost" onClick={openCreatePanel}>+ Add / import</button>
        <SearchBox q={q} setQ={setQ} placeholder="search identities…" />
        <div className="field" style={{ margin: 0 }}><label>Filter</label>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">all</option><option value="test">test</option>
            <option value="real">real</option>
          </select></div>
        <div className="field" style={{ margin: 0 }}><label>Created from</label>
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} /></div>
        <div className="field" style={{ margin: 0 }}><label>to</label>
          <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} /></div>
    </>
  );

  return (
    <>
    <Collapsible storageKey="identities-all" title="Identities"
                 count={`${shown.length}/${rows.length}`} right={identityControls}>
      {msg && <div className="banner err">{msg}</div>}
      {err && <div className="banner err">{err}</div>}
      {selectedIds.length > 0 && (
        <div className="banner info row">
          <span>{selectedIds.length} selected</span>
          <span className="grow" />
          <label style={{ margin: 0 }}>Sub-accounts/id</label>
          <input type="number" value={subCount} onChange={(e) => setSubCount(e.target.value)} style={{ width: 60 }} />
          <button className="btn" disabled={!op} onClick={genSubs}>Gen sub-accounts</button>
        </div>
      )}
      <table>
        <thead><tr>
          <th style={{ width: 36 }}><input type="checkbox" checked={shown.length > 0 && shown.every((i) => sel[i.id])}
            onChange={(e) => { const s = {}; if (e.target.checked) shown.forEach((i) => (s[i.id] = true)); setSel(s); }} /></th>
          <Sortable label="Name" k="full_name" sort={sort} setSort={setSort} w="19%" />
          <Sortable label="Type" k="type" sort={sort} setSort={setSort} w="7%" />
          <th style={{ width: "17%" }}>Sub-accounts</th>
          <Sortable label="Passport" k="passport_no" sort={sort} setSort={setSort} w="11%" />
          <Sortable label="Nat." k="nationality" sort={sort} setSort={setSort} w="6%" />
          <th style={{ width: "7%" }}>Posto</th><th style={{ width: "12%" }}>Comment</th>
          <Sortable label="Created" k="created_at" sort={sort} setSort={setSort} w="10%" />
        </tr></thead>
        <tbody>{shown.map((i) => (
          <tr key={i.id} className={`row-clickable${i.id === selectedId ? " row-selected" : ""}`}
            onClick={() => openDetailPanel(i)}>
            <td onClick={(e) => e.stopPropagation()}>
              <input type="checkbox" checked={!!sel[i.id]} onChange={(e) => setSel({ ...sel, [i.id]: e.target.checked })} />
            </td>
            <td>{i.full_name}</td><td><span className="pill">{i.type}</span></td>
            <td className="muted" style={{ fontSize: 13 }} title={i.status}>{summarizeSubAccounts(subsByIdentity[i.id])}</td>
            <td className="muted">{i.passport_no || "—"}</td><td className="muted">{i.nationality || "—"}</td>
            <td className="muted">{i.target_posto || "—"}</td>
            <td className="muted">{i.comment || "—"}</td>
            <td className="muted">{i.created_at ? i.created_at.slice(0, 10) : "—"}</td>
          </tr>
        ))}</tbody></table>
      {shown.length === 0 && <div className="muted">No identities match this filter.</div>}
    </Collapsible>
      {/* Booked identities live in their own table: they are deliberately absent from the list
          above (and so from the attach panel it feeds), because a person who already has an
          appointment must not be offered for another one. */}
      <BookedIdentities me={me} />
    </>
  );
}

function IdentityCreatePanel({ me, onDone }) {
  const op = me.is_operating;
  const [f, setF] = useState({ full_name: "", type: "test", birthdate: "", gender: "M",
                               passport_no: "", nationality: "CPV", comment: "", target_posto: "" });
  const [msg, setMsg] = useState("");
  const [gt, setGt] = useState({ count: 10, accounts_per_identity: 3, nationality: "CPV" });

  async function create() {
    setMsg("");
    const body = { ...f, target_posto: f.type === "real" ? (f.target_posto || null) : null };
    try { await api("/identities", { method: "POST", body }); onDone(); setF({ ...f, full_name: "", passport_no: "", comment: "", target_posto: "" }); }
    catch (e) { setMsg(e.message); }
  }
  async function genTest() {
    setMsg("");
    try { await api("/identities/gen-test", { method: "POST", body: gt }); onDone(); }
    catch (e) { setMsg(e.message); }
  }

  return (
    <>
      {msg && <div className="banner err">{msg}</div>}
      <h4 style={{ margin: "0 0 8px" }}>New identity</h4>
      <div className="field"><label>Full name</label><input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Type</label>
        <select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })} style={{ width: "100%" }}>
          <option value="test">test</option><option value="real">real</option>
        </select></div>
      <div className="field"><label>Birthdate</label>
        <input type="date" value={f.birthdate} onChange={(e) => setF({ ...f, birthdate: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Gender</label>
        <select value={f.gender} onChange={(e) => setF({ ...f, gender: e.target.value })} style={{ width: "100%" }}>
          <option value="M">M</option><option value="F">F</option>
        </select></div>
      <div className="field"><label>Passport</label><input value={f.passport_no} onChange={(e) => setF({ ...f, passport_no: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Nationality</label><input value={f.nationality} onChange={(e) => setF({ ...f, nationality: e.target.value })} style={{ width: "100%" }} /></div>
      {f.type === "real" && (
        <div className="field"><label>Target posto</label><input value={f.target_posto} onChange={(e) => setF({ ...f, target_posto: e.target.value })} style={{ width: "100%" }} /></div>
      )}
      <div className="field"><label>Comment</label><input value={f.comment} onChange={(e) => setF({ ...f, comment: e.target.value })} style={{ width: "100%" }} /></div>
      <button className="btn" disabled={!op || !f.full_name} onClick={create} style={{ width: "100%", marginBottom: 18 }}>Create identity</button>

      <h4 style={{ margin: "0 0 8px" }}>Generate test identities</h4>
      <div className="field"><label>Count</label><input type="number" value={gt.count} onChange={(e) => setGt({ ...gt, count: +e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Sub-accounts / id</label><input type="number" value={gt.accounts_per_identity} onChange={(e) => setGt({ ...gt, accounts_per_identity: +e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Nationality</label><input value={gt.nationality} onChange={(e) => setGt({ ...gt, nationality: e.target.value })} style={{ width: "100%" }} /></div>
      <button className="btn" disabled={!op} onClick={genTest} style={{ width: "100%", marginBottom: 18 }}>Generate</button>

      <h4 style={{ margin: "0 0 8px" }}>Import file</h4>
      <IdentityImportPipeline me={me} onDone={onDone} />
    </>
  );
}

// Upload -> import -> (optional) generate sub-accounts -> (optional) start registration, as one
// pipeline. Reading the file client-side and posting its text keeps the existing pure parser
// (parse_identities, auto-detects the passport-OCR labeled block format vs CSV) unchanged -- only
// how the text reaches it changes, from paste to upload. identity_ids/sub_account_ids returned by
// each step let this chain onto exactly the rows just created, not a stale/racy re-query.
function IdentityImportPipeline({ me, onDone }) {
  const [fileName, setFileName] = useState("");
  const [text, setText] = useState("");
  const [defaultType, setDefaultType] = useState("real");
  const [autoGen, setAutoGen] = useState(true);
  const [subCount, setSubCount] = useState(3);
  const [autoRegister, setAutoRegister] = useState(true);
  const [provider, setProvider] = useState("soax");
  const [kind, setKind] = useState("mobile");
  const [concurrency, setConcurrency] = useState(10);
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState([]);
  const [err, setErr] = useState("");
  const op = me.is_operating;

  function onFile(e) {
    const file = e.target.files[0];
    if (!file) return;
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => setText(String(reader.result || ""));
    reader.readAsText(file);
  }

  async function run() {
    setErr(""); setSteps([]); setBusy(true);
    const log = [];
    try {
      const imp = await api("/identities/import", { method: "POST", body: { csv: text, default_type: defaultType } });
      log.push({ label: "Import", detail: `${imp.created} identities created, ${imp.sub_accounts} sub-accounts from file, ${imp.errors.length} skipped/errors` });
      setSteps([...log]);

      let subAccountIds = imp.sub_account_ids || [];
      if (autoGen && imp.identity_ids.length > 0) {
        const gen = await api("/identities/gen-subaccounts",
          { method: "POST", body: { identity_ids: imp.identity_ids, count: Number(subCount) } });
        log.push({ label: "Generate sub-accounts", detail: `${gen.sub_accounts} sub-accounts generated for ${imp.identity_ids.length} identities` });
        setSteps([...log]);
        subAccountIds = gen.sub_account_ids || [];
      }

      if (autoRegister && autoGen && subAccountIds.length > 0) {
        const reg = await api("/registration/run",
          { method: "POST", body: { sub_account_ids: subAccountIds, concurrency: Number(concurrency), provider, kind } });
        log.push({ label: "Registration", detail: `run ${reg.run_id} started, ${reg.queued} queued -- watch progress on the Registration page` });
        setSteps([...log]);
      }
      onDone && onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        Upload a passport-OCR labeled .txt (<code>=== img === / First Name: ... / Passport Number: ...</code>)
        or a CSV (identity_id, first_name, last_name, traveldoc/passport_no, nationality, account_type/type,
        username, password, email, email_pass).
      </div>
      {err && <div className="banner err">{err}</div>}
      <div className="field">
        <label>File</label>
        <input type="file" accept=".txt,.csv,text/plain,text/csv" onChange={onFile} disabled={!op} style={{ width: "100%" }} />
      </div>
      {fileName && (
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
          {fileName} ({text ? text.split("\n").length : 0} lines) -- edit below if needed
        </div>
      )}
      <textarea value={text} onChange={(e) => { setText(e.target.value); setFileName(""); }} rows={6} disabled={!op}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8, marginBottom: 8 }}
        placeholder="…or paste content here" />
      <div className="field">
        <label>Default type (CSV rows without an explicit type; labeled-block files always import as real)</label>
        <select value={defaultType} onChange={(e) => setDefaultType(e.target.value)} disabled={!op} style={{ width: "100%" }}>
          <option value="real">real</option><option value="test">test</option>
        </select>
      </div>

      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" id="pipe-autogen" checked={autoGen} disabled={!op} onChange={(e) => setAutoGen(e.target.checked)} />
        <label htmlFor="pipe-autogen" style={{ margin: 0 }}>Auto-generate sub-accounts for imported identities</label>
      </div>
      {autoGen && (
        <div className="field">
          <label>Sub-accounts / identity</label>
          <input type="number" value={subCount} disabled={!op} onChange={(e) => setSubCount(e.target.value)} style={{ width: "100%" }} />
        </div>
      )}

      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" id="pipe-autoreg" checked={autoRegister} disabled={!op || !autoGen}
          onChange={(e) => setAutoRegister(e.target.checked)} />
        <label htmlFor="pipe-autoreg" style={{ margin: 0 }}>Auto-start registration for generated sub-accounts</label>
      </div>
      {autoRegister && autoGen && (
        <>
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            Hits the live portal and consumes CAPTCHA-solver credits.
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
            <div className="field" style={{ margin: 0, flex: 1 }}>
              <label>Proxy provider</label>
              <select value={provider} onChange={(e) => setProvider(e.target.value)} disabled={!op} style={{ width: "100%" }}>
                <option value="soax">soax</option><option value="webshare">webshare</option>
              </select>
            </div>
            <div className="field" style={{ margin: 0, flex: 1 }}>
              <label>Kind</label>
              <select value={kind} onChange={(e) => setKind(e.target.value)} disabled={!op} style={{ width: "100%" }}>
                <option value="rotating">rotating</option><option value="mobile">mobile</option>
              </select>
            </div>
            <div className="field" style={{ margin: 0, flex: 1 }}>
              <label>Concurrency</label>
              <input type="number" value={concurrency} disabled={!op} onChange={(e) => setConcurrency(e.target.value)} style={{ width: "100%" }} />
            </div>
          </div>
        </>
      )}

      <button className="btn" disabled={!op || !text.trim() || busy} onClick={run} style={{ width: "100%" }}>
        {busy ? "Running…" : "Import" + (autoGen ? " + generate" : "") + (autoGen && autoRegister ? " + register" : "")}
      </button>

      {steps.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {steps.map((s, i) => (
            <div key={i} className="banner info" style={{ marginBottom: 6 }}><b>{s.label}</b>: {s.detail}</div>
          ))}
        </div>
      )}
    </>
  );
}

function IdentityDetailPanel({ i, op, subs, onChanged, onClose }) {
  const [comment, setComment] = useState(i.comment || "");
  const [posto, setPosto] = useState(i.target_posto || "");
  const [msg, setMsg] = useState("");
  // Re-sync when a DIFFERENT identity is selected -- without this the panel kept showing (and would
  // have saved) the previously-selected identity's comment/posto.
  useEffect(() => { setComment(i.comment || ""); setPosto(i.target_posto || ""); setMsg(""); }, [i.id]);

  async function save() {
    setMsg("");
    try {
      await api(`/identities/${i.id}`, { method: "PATCH",
        body: { comment, target_posto: i.type === "real" ? posto : undefined } });
      onChanged();
    } catch (e) { setMsg(e.message); }
  }
  async function archive() {
    setMsg("");
    try { await api(`/identities/${i.id}/archive`, { method: "POST" }); onChanged(); }
    catch (e) { setMsg(e.message); }
  }
  async function del() {
    setMsg("");
    try { await api(`/identities/${i.id}`, { method: "DELETE" }); onChanged(); onClose(); }
    catch (e) { setMsg(e.message); }
  }

  return (
    <>
      {msg && <div className="banner err">{msg}</div>}
      <div className="field"><label>Type</label><span className="pill">{i.type}</span></div>
      <div className="field"><label>Status</label>{i.status}</div>
      <div className="field"><label>Birthdate</label>{i.birthdate || "—"}</div>
      <div className="field"><label>Gender</label>{i.gender || "—"}</div>
      <div className="field"><label>Passport</label>{i.passport_no || "—"}</div>
      <div className="field"><label>Nationality</label>{i.nationality || "—"}</div>
      <div className="field"><label>Created</label>{i.created_at ? i.created_at.slice(0, 10) : "—"}</div>
      {i.type === "real" && (
        <div className="field"><label>Target posto</label>
          <input value={posto} onChange={(e) => setPosto(e.target.value)} style={{ width: "100%" }} /></div>
      )}
      <div className="field"><label>Comment</label>
        <input value={comment} onChange={(e) => setComment(e.target.value)} style={{ width: "100%" }} /></div>
      <button className="btn" disabled={!op} onClick={save} style={{ width: "100%", marginBottom: 18 }}>Save</button>

      <div className="row" style={{ gap: 8, marginBottom: 18 }}>
        <button className="btn ghost" disabled={!op || i.status === "archived"} onClick={archive} style={{ flex: 1 }}>Archive</button>
        <button className="btn ghost" disabled={!op} onClick={del} style={{ flex: 1 }}>Delete</button>
      </div>

      <h4 style={{ margin: "0 0 8px" }}>Sub-accounts ({subs.length})</h4>
      {subs.length === 0 && <div className="muted" style={{ fontSize: 13 }}>None yet — generate some from the Identities table.</div>}
      {subs.map((s) => (
        <div key={s.id} className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          <div className="row" style={{ gap: 6 }}>
            <span style={{ fontFamily: "monospace", fontSize: 13 }}>{s.username}</span>
            <span className="grow" />
            <span className={s.status === "verified" || s.status === "login_ok" || s.status === "booked" ? "ok"
                            : s.status === "burned" ? "err" : "muted"} style={{ fontSize: 12 }}>{s.status}</span>
          </div>
          {s.email && <div className="muted" style={{ fontSize: 12 }}>{s.email}</div>}
        </div>
      ))}
    </>
  );
}

// apps/registrar emits verbose, developer-oriented progress strings (proxy URLs, raw HTML
// snippets, stack-trace-ish timeout messages) meant for the Redis event log / audit trail --
// exactly what you don't want staring back at you from a table cell. This maps them down to a
// short, fixed vocabulary for the Live column; the raw string is still available via the cell's
// title tooltip for anyone who needs to actually debug a specific row.
function shortRegPhase(raw) {
  if (!raw) return "—";
  const msg = raw.includes(": ") ? raw.slice(raw.indexOf(": ") + 2) : raw; // strip "username: " prefix
  const rules = [
    [/^starting/i, "Starting"],
    [/^creating email inbox/i, "Creating email…"],
    [/^email=/i, "Email ready"],
    [/^loading registration page/i, "Page loading…"],
    [/^register form loaded/i, "Form loaded"],
    [/^solving captcha/i, "Solving captcha…"],
    [/CAPTCHA failed/i, "Captcha failed"],
    [/^form filled/i, "Form filled"],
    [/^submitting registration/i, "Submitting…"],
    [/register SUCCESS/i, "Registered"],
    [/already exists/i, "Already exists"],
    [/register attempt failed/i, "Rejected — retrying"],
    [/FAIL get_session/i, "Proxy connect failed"],
    [/FAIL exhausted proxy retries/i, "Out of proxies"],
    [/^waiting for verification email/i, "Email polling…"],
    [/FAIL no email tokens/i, "Email timed out"],
    [/^email tokens received/i, "Token received"],
    [/^verifying \(attempt/i, "Verifying…"],
    [/^verify attempt/i, "Verifying…"],
    [/^VERIFIED/, "Completed"],
    [/FAIL verify exhausted/i, "Verify failed"],
    [/^FAIL /i, "Failed"],
  ];
  for (const [re, label] of rules) if (re.test(msg)) return label;
  return "Working…";
}

const REG_OUTCOME_LABEL = { registered: "Registered", exists: "Already exists", verified: "Verified", failed: "Failed" };

// Registration page: list not-yet-verified sub-accounts, multi-select + register/reset, live
// per-step status during registration. Conversation_extract #223/#230/#236 -- the one dashboard
// subsystem that was missing entirely (both backend and frontend) rather than just under-built.
function Registration({ me, events }) {
  const { rows, err, reload } = useList("/sub-accounts");
  const [sel, setSel] = useState({});
  const [statusFilter, setStatusFilter] = useState("not_verified"); // not_verified | all | new | registered | verified | failed
  const [typeFilter, setTypeFilter] = useState("all");              // all | test | real
  const [concurrency, setConcurrency] = useState(10);
  const [provider, setProvider] = useState("soax");
  const [kind, setKind] = useState("");
  const [geos, setGeos] = useState("");
  const [msg, setMsg] = useState("");
  const [runInfo, setRunInfo] = useState(null);
  const op = me.is_operating;

  // Provider/kind/geo come from the POOL, not a hardcoded list. They used to be two fixed options
  // ("soax", "webshare"), so a newly imported pool -- soax_mobile, say -- was simply unreachable
  // from this page however many proxies it had. Deriving them also lets the count below tell you a
  // selection is empty BEFORE you press Register, instead of a 409 afterwards.
  const { rows: poolRows } = useList("/proxies");
  const providers = [...new Set(poolRows.map((p) => p.provider))].sort();
  const kinds = [...new Set(poolRows.filter((p) => p.provider === provider).map((p) => p.kind))].sort();
  const geoOpts = [...new Set(poolRows.filter((p) => p.provider === provider && p.geo)
                                      .map((p) => p.geo))].sort();
  const wantGeos = geos.split(",").map((g) => g.trim().toUpperCase()).filter(Boolean);
  const matching = poolRows.filter((p) =>
    p.provider === provider && p.active && p.is_healthy !== false
    && (!kind || p.kind === kind)
    && (!wantGeos.length || !p.geo || wantGeos.includes(p.geo))).length;

  const typeFiltered = rows.filter((s) => typeFilter === "all" || s.identity_type === typeFilter);
  const statusFiltered = typeFiltered.filter((s) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "not_verified") return s.status !== "verified" && s.status !== "login_ok"
      && s.status !== "booked";
    if (statusFilter === "failed") return s.registration_status === "failed";
    return s.status === statusFilter;
  });
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(statusFiltered, { key: "created_at", dir: "desc" });

  const selectedIds = Object.keys(sel).filter((k) => sel[k]);

  async function register() {
    setMsg(""); setRunInfo(null);
    try {
      const r = await api("/registration/run", {
        method: "POST",
        body: { sub_account_ids: selectedIds, concurrency: Number(concurrency), provider,
                kind: kind || null, geos: geos || null },
      });
      setRunInfo(r);
      setSel({});
      reload();
    } catch (e) { setMsg(e.message); }
  }

  async function reset() {
    setMsg("");
    try {
      await api("/sub-accounts/reset", { method: "POST", body: { sub_account_ids: selectedIds } });
      setSel({});
      reload();
    } catch (e) { setMsg(e.message); }
  }

  return (
    <>
      <div className="card">
        <div className="row" style={{ marginBottom: 10, flexWrap: "wrap", gap: 10 }}>
          <h2 style={{ margin: 0 }}>Registration ({shown.length}/{rows.length})</h2>
          <span className="grow" />
          <SearchBox q={q} setQ={setQ} placeholder="search accounts…" />
          <div className="field" style={{ margin: 0 }}><label>Status</label>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="not_verified">not verified</option>
              <option value="all">all</option>
              <option value="new">new</option>
              <option value="registered">registered</option>
              <option value="verified">verified</option>
              <option value="failed">failed last attempt</option>
            </select></div>
          <div className="field" style={{ margin: 0 }}><label>Type</label>
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="all">all</option><option value="test">test</option><option value="real">real</option>
            </select></div>
          <button className="btn ghost" onClick={reload}>Refresh</button>
        </div>
        {err && <div className="banner err">{err}</div>}
        {msg && <div className="banner err">{msg}</div>}
        {runInfo && <div className="banner info">Run {runInfo.run_id} started — {runInfo.queued} account(s) queued.</div>}
        {selectedIds.length > 0 && (
          <div className="banner info row" style={{ flexWrap: "wrap", gap: 10 }}>
            <span>{selectedIds.length} selected</span>
            <span className="grow" />
            <div className="field" style={{ margin: 0 }}><label>Provider</label>
              <select value={provider} onChange={(e) => { setProvider(e.target.value); setKind(""); }}
                      disabled={!op}>
                {providers.map((p) => <option key={p} value={p}>{p}</option>)}
              </select></div>
            <div className="field" style={{ margin: 0 }}><label>Kind</label>
              <select value={kind} onChange={(e) => setKind(e.target.value)} disabled={!op}>
                <option value="">any</option>
                {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
              </select></div>
            <div className="field" style={{ margin: 0 }}><label>Geo</label>
              <select value={geos} onChange={(e) => setGeos(e.target.value)} disabled={!op}
                      title="exit country; registration hits the same portal behind the same reCAPTCHA">
                <option value="">any</option>
                {geoOpts.map((g) => <option key={g} value={g}>{g}</option>)}
                {geoOpts.length > 1 && <option value={geoOpts.join(",")}>{geoOpts.join(",")}</option>}
              </select></div>
            <span className={matching ? "muted" : "err"} style={{ fontSize: 12 }}>
              {matching} proxy{matching === 1 ? "" : "s"} match
            </span>
            <div className="field" style={{ margin: 0 }}><label>Concurrency</label>
              <input type="number" value={concurrency} onChange={(e) => setConcurrency(e.target.value)} disabled={!op} style={{ width: 60 }} /></div>
            <button className="btn ghost" disabled={!op} onClick={reset}>Reset (regen credentials)</button>
            <button className="btn" disabled={!op} onClick={register}>Register selected</button>
          </div>
        )}
        <table>
          <thead><tr>
            <th style={{ width: 36 }}><input type="checkbox" checked={shown.length > 0 && shown.every((s) => sel[s.id])}
              onChange={(e) => { const s = {}; if (e.target.checked) shown.forEach((r) => (s[r.id] = true)); setSel(s); }} /></th>
            <Sortable label="Identity" k="identity_name" sort={sort} setSort={setSort} w="18%" />
            <Sortable label="Type" k="identity_type" sort={sort} setSort={setSort} w="7%" />
            <Sortable label="Username" k="username" sort={sort} setSort={setSort} w="13%" />
            <Sortable label="Status" k="status" sort={sort} setSort={setSort} w="8%" />
            <th style={{ width: "17%" }}>Live</th>
            <Sortable label="Last run" k="registration_status" sort={sort} setSort={setSort} w="10%" />
            <th style={{ width: "9%" }}>Attempts</th><th>Email</th>
          </tr></thead>
          <tbody>{shown.map((s) => {
            const live = events.bySubAccount[s.id] || {};
            return (
              <tr key={s.id}>
                <td><input type="checkbox" checked={!!sel[s.id]} onChange={(e) => setSel({ ...sel, [s.id]: e.target.checked })} /></td>
                <td>{s.identity_name}</td>
                <td><span className="pill">{s.identity_type}</span></td>
                <td className="muted">{s.username}</td>
                <td>{s.status}</td>
                <td className={live.outcome === "verified" || live.outcome === "registered" ? "ok"
                              : live.outcome === "failed" ? "err" : "muted"} style={{ fontSize: 12 }}
                  title={live.phase || ""}>
                  {live.outcome ? (REG_OUTCOME_LABEL[live.outcome] || live.outcome) : shortRegPhase(live.phase)}
                </td>
                <td className={s.registration_status === "failed" ? "err" : "muted"}>{s.registration_status || "—"}</td>
                <td className="muted">{s.attempts}</td>
                <td className="muted">{s.email || "—"}</td>
              </tr>
            );
          })}</tbody></table>
        {shown.length === 0 && <div className="muted">No sub-accounts match this filter.</div>}
      </div>
      <RegistrationJobHistory />
    </>
  );
}

function RegistrationJobHistory() {
  const { rows, err, reload } = useList("/registration/jobs?limit=50");
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Recent registration attempts</h2>
        <span className="grow" />
        <button className="btn ghost" onClick={reload}>Refresh</button>
      </div>
      {err && <div className="banner err">{err}</div>}
      <table><thead><tr><th>Sub-account</th><th>Mode</th><th>Status</th><th>Attempts</th><th>When</th></tr></thead>
        <tbody>{rows.map((j) => (
          <tr key={j.id}>
            <td className="muted">{j.sub_account_id.slice(0, 8)}</td>
            <td>{j.mode}</td>
            <td className={j.status === "failed" ? "err" : j.status === "verified" ? "ok" : ""}>{j.status}</td>
            <td>{j.attempts}</td>
            <td className="muted">{j.created_at ? j.created_at.replace("T", " ").slice(0, 19) : "—"}</td>
          </tr>
        ))}</tbody></table>
      {rows.length === 0 && <div className="muted">No registration runs yet.</div>}
    </div>
  );
}

function ImportCsv({ me, path, label, hint, makeBody, onDone, compact }) {
  const [text, setText] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  async function go() {
    setErr(""); setRes(null);
    try { setRes(await api(path, { method: "POST", body: makeBody(text) })); onDone && onDone(); }
    catch (e) { setErr(e.message); }
  }
  const body = (
    <>
      {err && <div className="banner err">{err}</div>}
      {res && <div className="banner info">Created {res.created}{res.sub_accounts != null ? ` (+${res.sub_accounts} sub-accounts)` : ""}. {res.errors?.length ? `${res.errors.length} skipped/errors.` : ""}</div>}
      <div className="muted" style={{ marginBottom: 6, fontSize: 12 }}>{hint}</div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8 }}
        placeholder="paste CSV / lines here" />
      <div style={{ marginTop: 8 }}><button className="btn" disabled={!me.is_operating || !text.trim()} onClick={go} style={compact ? { width: "100%" } : undefined}>Import</button></div>
    </>
  );
  if (compact) return body;
  return (
    <div className="card">
      <h2>{label}</h2>
      {body}
    </div>
  );
}

function Proxies({ me, setLeftPanel }) {
  const { rows, err, reload } = useList("/proxies");
  const [selectedPool, setSelectedPool] = useState(null); // {provider, kind?} | null (null = all)
  const [modal, setModal] = useState(null);               // "add" | "import" | null
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  const filtered = rows.filter((p) => {
    if (!selectedPool) return true;
    if (selectedPool.kind) return p.provider === selectedPool.provider && p.kind === selectedPool.kind;
    return p.provider === selectedPool.provider;
  });
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(filtered, { key: "created_at", dir: "desc" });

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function toggle(id) {
    setMsg("");
    try { await api(`/proxies/${id}/toggle-active`, { method: "POST" }); reload(); }
    catch (e) { setMsg(e.message); }
  }

  // Dry run first, ALWAYS. lease_proxy filters on is_healthy and health.apply_result sets it False
  // on any probe failure, so a bad prober condemns good proxies permanently (639 of 2000 on
  // 2026-08-15). This is the only way to undo that -- and it clears every verdict in the pool, so
  // the operator sees the exact scope before anything is written.
  async function resetHealth() {
    setMsg(""); setNote(""); setBusy(true);
    try {
      const dry = await api("/proxies/reset-health", { method: "POST" });
      const ok = window.confirm(
        `Reset health for ${dry.probed} probed proxy/proxies?\n\n` +
        `Leasable now: ${dry.leasable_before} of ${dry.total}  ->  after: ${dry.total}\n\n` +
        "They are set back to UNPROBED, not to known-good: the next lease re-probes each one. " +
        "Nothing has been written yet.");
      if (!ok) return;
      const res = await api("/proxies/reset-health?confirm=true", { method: "POST" });
      setNote(`Reset ${res.probed} proxy health verdict(s); ${res.total} now leasable.`);
      reload();
    } catch (e) {
      // 409 = workers are RUNNING. Say what to do about it rather than just echoing the status.
      setMsg(e.message.startsWith("409")
        ? `${e.message}\n\nStop the booking run first -- a running worker re-probes and would ` +
          "immediately re-condemn what this clears."
        : e.message);
    } finally { setBusy(false); }
  }

  // The pool tree is permanent page navigation for Proxies (unlike Identities'/Bookings' on-demand
  // action panels), so it stays mounted in the left slot the whole time this page is active,
  // re-set whenever the underlying data or selection changes.
  useEffect(() => {
    const tree = {};
    for (const p of rows) {
      tree[p.provider] = tree[p.provider] || {};
      tree[p.provider][p.kind] = (tree[p.provider][p.kind] || 0) + 1;
    }
    setLeftPanel({
      title: "Proxy pools",
      closable: false,  // permanent page navigation, not an on-demand action panel
      content: (
        <ProxyPoolTree tree={tree} selected={selectedPool} onSelect={setSelectedPool} op={op}
          onAdd={() => setModal("add")} onImport={() => setModal("import")} />
      ),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, selectedPool, op]);
  useEffect(() => () => setLeftPanel(null), []); // eslint-disable-line react-hooks/exhaustive-deps

  const poolLabel = selectedPool ? `${selectedPool.provider}${selectedPool.kind ? " / " + selectedPool.kind : ""}` : "All proxies";

  return (
    <>
      {modal === "add" && (
        <Modal title="Add proxy" onClose={() => setModal(null)}>
          <ProxyAddForm me={me} defaultProvider={selectedPool?.provider} defaultKind={selectedPool?.kind}
            onDone={() => { reload(); setModal(null); }} />
        </Modal>
      )}
      {modal === "import" && (
        <Modal title="Import proxies" onClose={() => setModal(null)}>
          <ProxyImportForm me={me} defaultProvider={selectedPool?.provider} defaultKind={selectedPool?.kind}
            onDone={() => { reload(); setModal(null); }} />
        </Modal>
      )}
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>{poolLabel} ({shown.length}/{rows.length})</h2>
          <span className="grow" />
          <SearchBox q={q} setQ={setQ} placeholder="search proxies…" />
          <button className="btn ghost" disabled={!op || busy} onClick={resetHealth}
                  title="Clear every health verdict back to unprobed — undoes proxies wrongly
condemned by a failed probe. Shows the scope before writing.">
            {busy ? "Checking…" : "Reset pool health"}
          </button>
        </div>
        {err && <div className="banner err">{err}</div>}
        {msg && <div className="banner err" style={{ whiteSpace: "pre-line" }}>{msg}</div>}
        {note && <div className="banner">{note}</div>}
        {rows.length > 0 && (() => {
          const dead = rows.filter((p) => p.is_healthy === false).length;
          return dead > 0 ? (
            <div className="banner err">
              {dead} of {rows.length} proxies are marked unhealthy and are excluded from leasing.
            </div>
          ) : null;
        })()}
        <table><thead><tr>
          <Sortable label="Provider" k="provider" sort={sort} setSort={setSort} w="15%" />
          <Sortable label="Kind" k="kind" sort={sort} setSort={setSort} w="15%" />
          <Sortable label="Endpoint" k="endpoint" sort={sort} setSort={setSort} />
          <Sortable label="Active" k="active" sort={sort} setSort={setSort} w="10%" />
          <Sortable label="Health" k="is_healthy" sort={sort} setSort={setSort} w="16%" />
          <th style={{ width: "10%" }}>Max/IP</th><th style={{ width: "12%" }}></th></tr></thead>
          <tbody>{shown.map((p) => (
            <tr key={p.id}><td><span className="pill">{p.provider}</span></td><td>{p.kind}</td>
              <td className="muted">{p.endpoint}</td><td className={p.active ? "ok" : "muted"}>{String(p.active)}</td>
              <td className={p.is_healthy === false ? "err" : "muted"}
                  title={p.health || "unprobed"}>
                {p.is_healthy === false ? (p.health || "unhealthy") : (p.health ? "ok" : "unprobed")}
              </td>
              <td>{p.max_sessions}</td>
              <td><button className="btn ghost" disabled={!op} onClick={() => toggle(p.id)}>{p.active ? "Deactivate" : "Activate"}</button></td></tr>
          ))}</tbody></table>
        {shown.length === 0 && <div className="muted">No proxies in this pool yet.</div>}
      </div>
    </>
  );
}

function ProxyPoolTree({ tree, selected, onSelect, onAdd, onImport, op }) {
  const providers = Object.keys(tree).sort();
  const [collapsed, setCollapsed] = useState({});
  const poolTotal = (kinds) => Object.values(kinds).reduce((a, b) => a + b, 0);
  const grandTotal = providers.reduce((a, p) => a + poolTotal(tree[p]), 0);

  return (
    <>
      <div className="row" style={{ marginBottom: 14, gap: 6 }}>
        <button className="btn ghost" disabled={!op} onClick={onAdd} style={{ flex: 1 }}>+ Add</button>
        <button className="btn ghost" disabled={!op} onClick={onImport} style={{ flex: 1 }}>⇩ Import</button>
      </div>
      <div className={`tree-node${!selected ? " tree-selected" : ""}`} onClick={() => onSelect(null)}>
        All proxies <span className="muted">({grandTotal})</span>
      </div>
      {providers.length === 0 && <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>No proxies yet — Add or Import to get started.</div>}
      {providers.map((prov) => (
        <div key={prov}>
          <div className={`tree-node tree-parent${selected?.provider === prov && !selected.kind ? " tree-selected" : ""}`}
            onClick={() => onSelect({ provider: prov })}>
            <span className="tree-caret" onClick={(e) => {
              e.stopPropagation();
              setCollapsed((s) => ({ ...s, [prov]: !s[prov] }));
            }}>{collapsed[prov] ? "▸" : "▾"}</span>
            {prov} <span className="muted">({poolTotal(tree[prov])})</span>
          </div>
          {!collapsed[prov] && Object.keys(tree[prov]).sort().map((kind) => (
            <div key={kind}
              className={`tree-node tree-child${selected?.provider === prov && selected?.kind === kind ? " tree-selected" : ""}`}
              onClick={() => onSelect({ provider: prov, kind })}>
              {kind} <span className="muted">({tree[prov][kind]})</span>
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

function ProxyAddForm({ me, onDone, defaultProvider, defaultKind }) {
  const [f, setF] = useState({
    provider: defaultProvider || "soax", kind: defaultKind || "mobile",
    endpoint: "", credentials_ref: "", max_sessions: 2,
  });
  const [msg, setMsg] = useState("");
  async function create() {
    setMsg("");
    try { await api("/proxies", { method: "POST", body: f }); onDone(); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <>
      {msg && <div className="banner err">{msg}</div>}
      <div className="field"><label>Provider</label>
        <select value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value })} style={{ width: "100%" }}>
          <option value="soax">soax</option><option value="webshare">webshare</option>
        </select></div>
      <div className="field"><label>Kind</label>
        <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} style={{ width: "100%" }}>
          <option value="rotating">rotating</option><option value="mobile">mobile</option>
        </select></div>
      <div className="field"><label>Endpoint</label>
        <input value={f.endpoint} onChange={(e) => setF({ ...f, endpoint: e.target.value })}
          placeholder="host:port" style={{ width: "100%" }} /></div>
      <div className="field"><label>Credentials (user:pass)</label>
        <input value={f.credentials_ref} onChange={(e) => setF({ ...f, credentials_ref: e.target.value })} style={{ width: "100%" }} /></div>
      <div className="field"><label>Max sessions</label>
        <input type="number" value={f.max_sessions} onChange={(e) => setF({ ...f, max_sessions: +e.target.value })} style={{ width: "100%" }} /></div>
      <button className="btn" disabled={!me.is_operating || !f.endpoint} onClick={create} style={{ width: "100%" }}>Add proxy</button>
    </>
  );
}

function ProxyImportForm({ me, onDone, defaultProvider, defaultKind }) {
  const [prov, setProv] = useState(defaultProvider || "soax");
  const [kind, setKind] = useState(defaultKind || "mobile");
  return (
    <>
      <div className="field"><label>Provider</label>
        <select value={prov} onChange={(e) => setProv(e.target.value)} style={{ width: "100%" }}>
          <option value="soax">soax</option><option value="webshare">webshare</option>
        </select></div>
      <div className="field"><label>Kind</label>
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: "100%" }}>
          <option value="rotating">rotating</option><option value="mobile">mobile</option>
        </select></div>
      <ImportInline me={me} makeBody={(text) => ({ provider: prov, kind, text })} onDone={onDone} />
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
  const [catalog, setCatalog] = useState([]);
  const [key, setKey] = useState("CAPSOLVER_KEYS");
  const [val, setVal] = useState("");
  const [msg, setMsg] = useState("");
  const reload = () => api("/settings/global").then(setCurrent).catch(() => {});
  useEffect(() => { reload(); }, []);
  // The key was a free-text box defaulting to CAPSOLVER_KEYS, so every other global setting was
  // reachable only by knowing its exact name. CF_MAIL_DOMAIN and EMAIL_PROVIDER are the ones that
  // bit: registration is quota-blocked on a single mail domain, changing it is the obvious next
  // test, and the operator's reasonable read of this panel was "there is no option to change the
  // email service". The catalog already knows every settable GLOBAL key and its note.
  useEffect(() => {
    api("/settings/catalog").then((c) => setCatalog(c.global || [])).catch(() => setCatalog([]));
  }, []);
  const groups = {};
  for (const s of catalog) (groups[s.group || "other"] = groups[s.group || "other"] || []).push(s);
  const chosen = catalog.find((s) => s.key === key);
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
        Stored in the DB (not env). Secret values are masked on read.
        {chosen && chosen.note ? <> · <span style={{ fontStyle: "italic" }}>{chosen.note}</span></> : null}
        {chosen && chosen.default != null ? <> · default <code>{String(chosen.default)}</code></> : null}
      </div>
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="field"><label>Key</label>
          {catalog.length > 0 ? (
            <select value={key} onChange={(e) => setKey(e.target.value)} style={{ width: 260 }}>
              {Object.keys(groups).sort().map((g) => (
                <optgroup key={g} label={g.replace(/_/g, " ")}>
                  {groups[g].map((s) => (<option key={s.key} value={s.key}>{s.key}</option>))}
                </optgroup>
              ))}
            </select>
          ) : (
            <input value={key} onChange={(e) => setKey(e.target.value)} style={{ width: 260 }} />
          )}
        </div>
        <div className="field grow"><label>Value</label>
          {_SETTING_SELECT_OPTIONS[key] ? (
            <select value={val} onChange={(e) => setVal(e.target.value)} style={{ width: "100%" }}>
              <option value="">— choose —</option>
              {_SETTING_SELECT_OPTIONS[key].map((o) => (<option key={o} value={o}>{o}</option>))}
            </select>
          ) : (
            <input value={val} onChange={(e) => setVal(e.target.value)} style={{ width: "100%" }} placeholder="value" />
          )}
        </div>
        <div className="field" style={{ alignSelf: "end" }}><button className="btn" disabled={!me.is_operating || !key || !val} onClick={save}>Save</button></div>
      </div>
      <table><thead><tr><th>Key</th><th>Value</th></tr></thead>
        <tbody>{Object.entries(current).map(([k, v]) => (
          <tr key={k}><td><code>{k}</code></td><td className="muted">{String(v)}</td></tr>
        ))}{Object.keys(current).length === 0 && <tr><td colSpan={2} className="muted">none set</td></tr>}</tbody></table>
    </div>
  );
}

function TravelDatesEditor({ me }) {
  const [fd, setFd] = useState(null);
  const [msg, setMsg] = useState("");
  const load = () => api("/configs/form_defaults").then((r) => setFd(r.value)).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!fd) return null;
  const dyn = fd.dynamic || {};
  const setDyn = (k, v) => setFd({ ...fd, dynamic: { ...dyn, [k]: v } });
  async function save() {
    setMsg("");
    try { await api("/configs/form_defaults", { method: "PUT", body: { value: fd } }); setMsg("saved"); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <div className="card">
      <h2>Booking travel dates <span className="muted">(frequently updated)</span></h2>
      {msg && <div className={`banner ${msg === "saved" ? "info" : "err"}`}>{msg}</div>}
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        f30/f31 = arrival/departure (literal <code>YYYY/MM/DD</code>); f16/f17 = passport dates (<code>today±Nyr</code>).
      </div>
      <div className="row" style={{ flexWrap: "wrap" }}>
        {["f30", "f31", "f16", "f17"].map((k) => (
          <div className="field" key={k}><label>{k}</label>
            <input value={dyn[k] || ""} onChange={(e) => setDyn(k, e.target.value)} style={{ width: 130 }} /></div>
        ))}
        <div className="field" style={{ alignSelf: "end" }}><button className="btn" disabled={!me.is_operating} onClick={save}>Save dates</button></div>
      </div>
    </div>
  );
}

function ConfigJsonEditor({ me, ck }) {
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  useEffect(() => { api(`/configs/${ck}`).then((r) => setText(JSON.stringify(r.value, null, 2))).catch(() => {}); }, [ck]);
  async function save() {
    setMsg("");
    let value;
    try { value = JSON.parse(text); } catch { setMsg("invalid JSON"); return; }
    try { await api(`/configs/${ck}`, { method: "PUT", body: { value } }); setMsg("saved"); }
    catch (e) { setMsg(e.message); }
  }
  return (
    <div className="card">
      <h2>{ck} <span className="muted">(raw JSON)</span></h2>
      {msg && <div className={`banner ${msg === "saved" ? "info" : "err"}`}>{msg}</div>}
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={12}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8 }} />
      <div style={{ marginTop: 8 }}><button className="btn" disabled={!me.is_operating} onClick={save}>Save {ck}</button></div>
    </div>
  );
}

function Templates({ me }) {
  const { rows, err, reload } = useList("/templates");
  const [f, setF] = useState({ name: "", role: "scout", settings: "{}" });
  const [editId, setEditId] = useState(null);
  const [msg, setMsg] = useState("");
  const op = me.is_operating;

  function startEdit(t) {
    setEditId(t.id);
    setF({ name: t.name, role: t.role, settings: JSON.stringify(t.settings_json || {}, null, 2) });
  }
  function cancelEdit() { setEditId(null); setF({ name: "", role: "scout", settings: "{}" }); }

  async function save() {
    setMsg("");
    let settings;
    try { settings = JSON.parse(f.settings || "{}"); } catch { setMsg("settings must be valid JSON"); return; }
    try {
      if (editId) await api(`/templates/${editId}`, { method: "PUT", body: { name: f.name, role: f.role, settings } });
      else await api("/templates", { method: "POST", body: { name: f.name, role: f.role, settings } });
      reload(); cancelEdit();
    } catch (e) { setMsg(e.message); }
  }
  async function del(id) {
    setMsg("");
    try { await api(`/templates/${id}`, { method: "DELETE" }); reload(); }
    catch (e) { setMsg(e.message); }
  }

  return (
    <>
      <div className="card">
        <h2>{editId ? "Edit worker template" : "New worker template"}</h2>
        {msg && <div className="banner err">{msg}</div>}
        <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-start" }}>
          <div className="field"><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
          <div className="field"><label>Role</label>
            <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
              <option value="scout">scout</option><option value="apply">apply</option>
            </select></div>
          <div className="field grow"><label>Settings (JSON, template-layer knobs)</label>
            <textarea value={f.settings} onChange={(e) => setF({ ...f, settings: e.target.value })} rows={3}
              style={{ width: "100%", fontFamily: "monospace", fontSize: 12, background: "var(--panel2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 8 }}
              placeholder='{"NAV_TIMEOUT_SCALE": 4, "KEEPALIVE_NO_RESTORE": true}' /></div>
          <div className="field" style={{ alignSelf: "end" }}>
            <button className="btn" disabled={!op || !f.name} onClick={save}>{editId ? "Save" : "Create"}</button>
            {editId && <button className="btn ghost" style={{ marginLeft: 6 }} onClick={cancelEdit}>Cancel</button>}
          </div>
        </div>
      </div>
      <div className="card">
        <h2>Templates ({rows.length})</h2>
        {err && <div className="banner err">{err}</div>}
        <table><thead><tr><th>Name</th><th>Role</th><th>Settings</th><th></th></tr></thead>
          <tbody>{rows.map((t) => (
            <tr key={t.id}><td>{t.name}</td><td><span className="pill">{t.role}</span></td>
              <td className="muted" style={{ fontFamily: "monospace", fontSize: 12 }}>{JSON.stringify(t.settings_json)}</td>
              <td className="row">
                <button className="btn ghost" disabled={!op} onClick={() => startEdit(t)}>Edit</button>
                <button className="btn ghost" disabled={!op} onClick={() => del(t.id)}>Delete</button>
              </td></tr>
          ))}</tbody></table>
      </div>
    </>
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
      <TravelDatesEditor me={me} />
      <ConfigJsonEditor me={me} ck="form_defaults" />
      <ConfigJsonEditor me={me} ck="quest_steps" />
      <div className="card"><h2>Settings taxonomy</h2>
        <div className="muted">{cat._meta.total} settings · precedence: {cat._meta.precedence}</div>
        <div className="muted" style={{ marginTop: 6 }}>Per-booking overrides (e.g. discovery window, poll gaps) are set from that booking's ⚙ Settings panel on the Bookings page, not here.</div></div>
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

// Redis memory / stream lengths / disk / worker counts -- exists specifically because the whole
// reconstruction effort started from a Redis memory-inflation incident that nobody noticed until it
// was too late; this page is the always-on early-warning the incident postmortem called for.
function Ops() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api("/ops/status").then(setStatus).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);
  if (err) return <div className="banner err">{err}</div>;
  if (!status) return <div className="muted">Loading…</div>;
  return (
    <div className="card">
      <h2>Ops status <span className="muted">(auto-refreshes every 5s)</span></h2>
      <div className="row" style={{ flexWrap: "wrap", gap: 24 }}>
        <div>
          <h3>Redis</h3>
          <div className="muted">memory: <b className="ok">{status.redis?.used_memory_human || "—"}</b></div>
          <div className="muted">evt stream length: <b>{status.evt_stream_len ?? "—"}</b></div>
          <div className="muted">evicted keys: {status.redis?.evicted_keys ?? "—"}</div>
          {status.redis?.error && <div className="banner err">{status.redis.error}</div>}
        </div>
        <div>
          <h3>Disk</h3>
          <div className="muted">free: <b className={status.disk?.free_pct < 15 ? "err" : "ok"}>{status.disk?.free_pct}%</b></div>
          {status.disk?.error && <div className="banner err">{status.disk.error}</div>}
        </div>
        <div>
          <h3>Workers</h3>
          <div className="muted">running: <b>{status.workers?.running ?? "—"}</b></div>
          <div className="muted">supervisor processes: {status.workers?.supervisor_processes ?? "—"}</div>
          {status.workers?.by_role && Object.entries(status.workers.by_role).map(([k, v]) => (
            <div className="muted" key={k}>{k}: {v}</div>
          ))}
          {status.workers?.error && <div className="banner err">{status.workers.error}</div>}
        </div>
      </div>
    </div>
  );
}

function Audit() {
  const { rows, err, reload } = useList("/audit?limit=200");
  const { q, setQ, sort, setSort, rows: shown } = useSortSearch(rows, { key: "ts", dir: "desc" });
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Audit log <span className="muted">(most recent {rows.length})</span></h2>
        <span className="grow" />
        <SearchBox q={q} setQ={setQ} placeholder="search audit…" />
        <button className="btn ghost" onClick={reload}>Refresh</button>
      </div>
      {err && <div className="banner err">{err}</div>}
      <table><thead><tr>
        <Sortable label="Time" k="ts" sort={sort} setSort={setSort} />
        <Sortable label="Actor" k="actor" sort={sort} setSort={setSort} />
        <Sortable label="Action" k="action" sort={sort} setSort={setSort} />
        <Sortable label="Target" k="target" sort={sort} setSort={setSort} />
        <th>IP</th><th>Status</th>
      </tr></thead>
        <tbody>{shown.map((r) => (
          <tr key={r.id}>
            <td className="muted">{r.ts ? r.ts.replace("T", " ").slice(0, 19) : "—"}</td>
            <td>{r.actor || "—"}</td>
            <td><span className="pill">{r.action}</span></td>
            <td className="muted">{r.target || "—"}</td>
            <td className="muted">{r.payload?.ip || "—"}</td>
            <td className={r.payload?.status >= 400 ? "err" : "ok"}>{r.payload?.status ?? "—"}</td>
          </tr>
        ))}</tbody></table>
    </div>
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
