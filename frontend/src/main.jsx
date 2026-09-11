import { StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_BASE_URL || "";

const iconPaths = {
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M16 3v4M8 3v4M3 11h18" /></>,
  chart: <><path d="M4 19V5M4 19h17" /><path d="m7 15 4-4 3 2 5-7" /></>,
  chevron: <path d="m9 18 6-6-6-6" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  play: <path d="m8 5 11 7-11 7z" fill="currentColor" stroke="none" />,
  shield: <path d="M12 3 20 6v5c0 5-3.4 8.2-8 10-4.6-1.8-8-5-8-10V6z" />,
  spark: <path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />,
  target: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /></>,
};

function Icon({ name, size = 18 }) {
  return <svg aria-hidden="true" className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{iconPaths[name]}</svg>;
}

function api(path, options) {
  return fetch(`${API}${path}`, { cache: "no-store", ...options }).then(async (response) => {
    if (!response.ok) throw new Error(`Request failed: ${response.status}`);
    return response.json();
  });
}

function percentage(value, fallback = "0.0%") {
  if (value === undefined || value === null || value === "") return fallback;
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function riskClass(risk) {
  const normalized = String(risk || "").toLowerCase();
  if (normalized.includes("safe")) return "safe";
  if (normalized.includes("bold") || normalized.includes("high")) return "bold";
  return "balanced";
}

function App() {
  const today = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(today);
  const [dates, setDates] = useState([]);
  const [picks, setPicks] = useState([]);
  const [performance, setPerformance] = useState(null);
  const [status, setStatus] = useState("Loading analyst intelligence…");
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState("");
  const [riskFilter, setRiskFilter] = useState("all");

  const load = useCallback(async (requestedDate) => {
    setError("");
    const [dateResponse, predictionResponse] = await Promise.all([
      api("/dates/available?predicted_only=false"),
      api(`/predictions/top?date=${encodeURIComponent(requestedDate)}`),
    ]);
    setDates(dateResponse.dates || []);
    setPicks(predictionResponse.picks || []);
    setPerformance(predictionResponse.performance || null);
    setStatus(predictionResponse.count ? `${predictionResponse.count} ranked opportunities ready` : `No stored opportunities for ${requestedDate}`);
  }, []);

  useEffect(() => {
    load(date).catch((reason) => {
      setError(reason.message);
      setStatus("The dashboard could not be refreshed.");
    });
  }, [date, load]);

  const visiblePicks = useMemo(() => picks.filter((pick) => riskFilter === "all" || riskClass(pick.risk_tier) === riskFilter), [picks, riskFilter]);
  const totalEdge = useMemo(() => picks.reduce((total, pick) => total + Number(pick.edge || 0), 0), [picks]);

  async function runPipeline() {
    setIsRunning(true);
    setError("");
    setStatus(`Refreshing fixtures and predictions for ${date}…`);
    try {
      await api(`/run?source=hybrid&date=${encodeURIComponent(date)}`, { method: "POST" });
      await load(date);
    } catch (reason) {
      setError(reason.message);
      setStatus("The pipeline did not complete.");
    } finally {
      setIsRunning(false);
    }
  }

  return <main className="app-shell">
    <nav className="topbar" aria-label="Primary navigation">
      <a className="brand" href="/"><span className="brand-mark"><Icon name="spark" size={22} /></span><span>SUMMAS<small>ANALYST DESK</small></span></a>
      <span className="live-status"><i /> Engine online</span>
    </nav>

    <header className="hero">
      <div><p className="eyebrow"><Icon name="target" size={15} /> Match intelligence</p><h1>Make every fixture<br /><em>count.</em></h1><p className="hero-copy">A focused view of the model’s highest-conviction football opportunities, refreshed from the live SUMMAS prediction engine.</p></div>
      <aside className="hero-orb" aria-hidden="true"><Icon name="chart" size={54} /><span>MODEL<br />LIVE</span></aside>
    </header>

    <section className="control-card" aria-label="Matchday controls">
      <div className="date-input"><Icon name="calendar" /><label htmlFor="match-date">Matchday<input id="match-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label></div>
      <button className="run-button" type="button" onClick={runPipeline} disabled={isRunning}><Icon name="play" />{isRunning ? "Refreshing engine…" : "Run selected date"}</button>
      <p className="status" role="status"><Icon name="clock" size={16} />{status}</p>
    </section>
    {error && <p className="error" role="alert">{error}</p>}

    <section className="summary-grid" aria-label="Matchday summary">
      <article><span className="summary-icon mint"><Icon name="chart" /></span><p>Ranked picks</p><strong>{picks.length}</strong><small>for {date}</small></article>
      <article><span className="summary-icon gold"><Icon name="target" /></span><p>Average edge</p><strong>{picks.length ? percentage(totalEdge / picks.length) : "—"}</strong><small>model advantage</small></article>
      <article><span className="summary-icon blue"><Icon name="shield" /></span><p>Settled win rate</p><strong>{performance ? `${Number(performance.win_rate_settled ?? performance.win_rate ?? 0).toFixed(1)}%` : "—"}</strong><small>{performance ? `${performance.wins ?? 0} wins · ${performance.losses ?? 0} losses` : "awaiting results"}</small></article>
    </section>

    <section className="matchdays" aria-label="Available matchdays"><div className="section-label"><span>Available matchdays</span><Icon name="chevron" size={16} /></div><div className="dates">{dates.slice(0, 6).map((item) => <button key={item.date} className={item.date === date ? "active" : ""} onClick={() => setDate(item.date)}><span>{item.date}</span><small>{item.fixtures} fixtures</small></button>)}</div></section>

    <section className="board" aria-live="polite">
      <div className="board-heading"><div><p className="eyebrow"><Icon name="spark" size={15} /> Model selections</p><h2>Today’s sharpest angles</h2></div><div className="filter-group" aria-label="Risk filter">{["all", "safe", "balanced", "bold"].map((risk) => <button className={riskFilter === risk ? "selected" : ""} key={risk} onClick={() => setRiskFilter(risk)}>{risk}</button>)}</div></div>
      <div className="cards">{visiblePicks.map((pick) => <article className="pick" key={`${pick.fixture_id}-${pick.market}-${pick.selection}`}><div className="pick-heading"><div><p className="league">{pick.league || "Unknown league"}</p><h3>{pick.home_team && pick.away_team ? `${pick.home_team} vs ${pick.away_team}` : pick.match}</h3></div><span className={`risk ${riskClass(pick.risk_tier)}`}>{pick.risk_tier || "Balanced"}</span></div><div className="selection"><span><Icon name="target" size={17} /> {pick.market}</span><strong>{pick.selection}</strong></div><dl><div><dt>Probability</dt><dd>{pick.probability_pct ? `${pick.probability_pct}%` : percentage(pick.probability)}</dd></div><div><dt>Model edge</dt><dd className="positive">+{percentage(pick.edge)}</dd></div><div><dt>Stake</dt><dd>{Number(pick.stake || 0).toFixed(2)}</dd></div></dl><div className="card-footer"><span><Icon name="clock" size={14} /> {pick.kickoff || pick.date || "Kickoff TBD"}</span><button aria-label={`View ${pick.home_team || "match"} details`}><Icon name="chevron" size={17} /></button></div>{pick.explanation && <p className="explanation">{pick.explanation}</p>}</article>)}</div>
      {!visiblePicks.length && !error && <article className="empty"><span className="summary-icon mint"><Icon name="spark" /></span><h3>No predictions yet</h3><p>Run this matchday to collect fixtures and generate ranked opportunities.</p></article>}
    </section>
  </main>;
}

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
