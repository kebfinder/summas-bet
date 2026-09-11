import { StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_BASE_URL || "";
const today = new Date().toISOString().slice(0, 10);

const api = (path, options) => fetch(`${API}${path}`, { cache: "no-store", ...options }).then(async (response) => {
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
});

const percent = (pick) => Number(pick.probability_pct ?? (Number(pick.probability || 0) * 100));
const stars = (count) => "★".repeat(Math.max(0, Math.min(5, Number(count || 0)))) + "☆".repeat(5 - Math.max(0, Math.min(5, Number(count || 0))));
const marketType = (pick) => {
  const market = String(pick.market || "").toLowerCase();
  if (market.includes("goal") || market.includes("over") || market.includes("under")) return "goals";
  if (market.includes("btts") || market.includes("both teams")) return "btts";
  return "safe";
};
const isHighConfidence = (pick) => Number(pick.confidence_stars || 0) >= 4 || percent(pick) >= 75;
const risk = (pick) => String(pick.risk_tier || "Medium").toUpperCase();

function App() {
  const [date, setDate] = useState(today);
  const [dates, setDates] = useState([]);
  const [picks, setPicks] = useState([]);
  const [performance, setPerformance] = useState(null);
  const [filter, setFilter] = useState("all");
  const [isRunning, setIsRunning] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async (requestedDate) => {
    const [dateResponse, predictionResponse] = await Promise.all([
      api("/dates/available?predicted_only=false"),
      api(`/predictions/top?date=${encodeURIComponent(requestedDate)}`),
    ]);
    setDates(dateResponse.dates || []);
    setPicks(predictionResponse.picks || []);
    setPerformance(predictionResponse.performance || null);
    setMessage(predictionResponse.note || "");
  }, []);

  useEffect(() => { load(date).catch((error) => setMessage(`Unable to refresh the prediction board: ${error.message}`)); }, [date, load]);

  async function runDate() {
    setIsRunning(true);
    setMessage(`Running the SUMMAS engine for ${date}…`);
    try {
      await api(`/run?source=hybrid&date=${encodeURIComponent(date)}`, { method: "POST" });
      await load(date);
    } catch (error) {
      setMessage(`The engine run could not complete: ${error.message}`);
    } finally { setIsRunning(false); }
  }

  const visiblePicks = useMemo(() => picks.filter((pick) => filter === "all" || (filter === "safe" ? isHighConfidence(pick) : marketType(pick) === filter)), [filter, picks]);
  const coverage = useMemo(() => Object.entries(picks.reduce((totals, pick) => {
    const league = pick.league || "Other leagues";
    totals[league] = (totals[league] || 0) + 1;
    return totals;
  }, {})).sort((a, b) => b[1] - a[1]).slice(0, 5), [picks]);
  const bars = useMemo(() => picks.slice(0, 7).reverse().map(percent), [picks]);
  const fixtures = dates.find((item) => item.date === date)?.fixtures ?? picks.length;
  const hitRate = performance ? Number(performance.win_rate_settled ?? performance.win_rate ?? 0) : 0;

  return <div className="app">
    <aside className="side">
      <div className="logo">SUM<span>MAS</span></div>
      <nav className="nav" aria-label="Dashboard navigation">
        <button className="active" type="button">▣ <span>Dashboard</span></button><button type="button">◎ <span>Predictions</span></button><button type="button">◷ <span>Fixtures</span></button><button type="button">▤ <span>Leagues</span></button><button type="button">◒ <span>Analytics</span></button><button type="button">✓ <span>Results</span></button><button type="button">↗ <span>Backtest</span></button>
      </nav>
      <div className="foot">Football intelligence<br />Prediction engine UI</div>
    </aside>
    <main className="main">
      <header className="top"><div><div className="eyebrow">Prediction centre</div><h1>Today&apos;s Football</h1><div className="sub">Ranked opportunities across your selected leagues</div></div><label className="date">Matchday <input aria-label="Matchday" type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label></header>
      <section className="stats" aria-label="Prediction statistics"><div className="stat"><small>Matches analysed</small><b>{fixtures}</b></div><div className="stat"><small>Predictions</small><b>{picks.length}</b></div><div className="stat"><small>High confidence</small><b>{picks.filter(isHighConfidence).length}</b></div><div className="stat"><small>Model hit rate</small><b>{performance ? `${hitRate.toFixed(1)}%` : "—"}</b></div></section>
      <section className="grid"><div className="panel"><div className="ph"><h2>Ranked Predictions</h2><select className="filter" aria-label="Filter prediction markets" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All markets</option><option value="safe">High confidence</option><option value="goals">Goals</option><option value="btts">BTTS</option></select></div>
        <div className="rows">{visiblePicks.length ? visiblePicks.map((pick, index) => <article className="match" key={`${pick.fixture_id}-${pick.market}-${pick.selection}`}><div className="rank">{String(index + 1).padStart(2, "0")}</div><div className="teams"><b>{pick.home_team && pick.away_team ? `${pick.home_team} vs ${pick.away_team}` : pick.match}</b><span>{pick.league || "League unavailable"} · {pick.kickoff || pick.date || "Kickoff TBD"}</span></div><div className="market">{pick.market || "Market"}</div><div className="pick">{pick.selection || "Awaiting selection"}</div><div><span className="prob">{percent(pick).toFixed(1)}%</span><br /><span className="stars">{stars(pick.confidence_stars || 0)}</span></div><div className={`badge ${risk(pick).toLowerCase()}`}>{risk(pick)}</div></article>) : <div className="empty">No ranked opportunities are stored for this date. Run the engine to collect fixtures and generate predictions.</div>}</div>
        <div className="note" aria-live="polite">{message || <>Live data from your SUMMAS prediction engine. Bookmaker value remains <b>Odds unavailable</b> until prices are supplied.</>}</div>
      </div><div><div className="panel chart-panel"><div className="ph"><h2>Model confidence</h2><span className="sub">Current board</span></div><div className="chart">{(bars.length ? bars : [15, 20, 18, 28, 22, 30, 25]).map((value, index) => <div className="bar" title={`${Number(value).toFixed(1)}% confidence`} style={{ height: `${Math.max(15, Math.min(100, value))}%` }} key={index} />)}</div></div><div className="panel"><div className="ph"><h2>Coverage</h2><span className="sub">Selected leagues</span></div><div className="sidecard">{coverage.length ? coverage.map(([league, count]) => <div className="league" key={league}><b>{league}</b><span>{count} prediction{count === 1 ? "" : "s"}</span></div>) : <div className="empty">Coverage appears after predictions are generated.</div>}</div></div><button className="run" type="button" onClick={runDate} disabled={isRunning}>{isRunning ? "Running engine…" : "Run selected date"}</button></div></section>
    </main>
  </div>;
}

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
