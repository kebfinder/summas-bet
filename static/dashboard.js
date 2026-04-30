const initialToday = window.__DASHBOARD_BOOTSTRAP__?.today || "";
const initialDates = window.__DASHBOARD_BOOTSTRAP__?.available_dates || [];
const refreshMs = 30000;

const dateStrip = document.getElementById("date-strip");
const picksNode = document.getElementById("picks");
const dateInput = document.getElementById("date-input");
const runDateBtn = document.getElementById("run-date-btn");
const toggleDatesBtn = document.getElementById("toggle-dates-btn");
const statusLine = document.getElementById("status-line");
const currentDateLabel = document.getElementById("current-date-label");
const topJsonLink = document.getElementById("top-json-link");
const allJsonLink = document.getElementById("all-json-link");
const fixturesJsonLink = document.getElementById("fixtures-json-link");
const summaryJsonLink = document.getElementById("summary-json-link");
const topPicksCaption = document.getElementById("top-picks-caption");
const compareToggle = document.getElementById("compare-toggle");
const filterLeague = document.getElementById("filter-league");
const filterRisk = document.getElementById("filter-risk");
const filterStars = document.getElementById("filter-stars");
const filterValue = document.getElementById("filter-value");
const comparePanel = document.getElementById("compare-panel");
const compareGrid = document.getElementById("compare-grid");
const calcStake = document.getElementById("calc-stake");
const calcOdds = document.getElementById("calc-odds");
const calcOutput = document.getElementById("calc-output");

let currentDate = initialToday;
let availableDates = initialDates;
let datesExpanded = false;
let refreshHandle = null;
const autoCollectAttempts = new Set();
const runInFlightDates = new Set();
let refreshInFlight = false;
let compareMode = false;
const selectedForCompare = new Set();
let cachedPicks = [];

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const formatPercent = (value) => `${(Number(value ?? 0) * 100).toFixed(1)}%`;
const stars = (n) => "★".repeat(Math.max(0, Math.min(5, Number(n || 0)))) + "☆".repeat(5 - Math.max(0, Math.min(5, Number(n || 0))));

function updateLinks(dateValue) {
  const encoded = encodeURIComponent(dateValue);
  topJsonLink.href = `/board?date=${encoded}`;
  allJsonLink.href = `/matchday?date=${encoded}`;
  fixturesJsonLink.href = `/fixtures-view?date=${encoded}`;
  summaryJsonLink.href = `/summary?date=${encoded}`;
}

function chooseInitialDate(rows) {
  if (!Array.isArray(rows) || !rows.length) return initialToday;
  const queryDate = new URLSearchParams(window.location.search).get("date");
  if (queryDate) return queryDate;
  return rows.find((r) => r.date === initialToday)?.date || rows[0].date;
}

function renderDateStrip(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    toggleDatesBtn.classList.add("hidden");
    dateStrip.innerHTML = `<article class="empty-card"><h3>No matchdays available yet</h3><p>Run a date to populate your board.</p></article>`;
    return;
  }
  const sortedRows = [...rows].sort((a,b)=>String(b.date).localeCompare(String(a.date)));
  const visibleRows = datesExpanded ? sortedRows : sortedRows.slice(0,3);
  toggleDatesBtn.classList.toggle("hidden", sortedRows.length <= 3);
  toggleDatesBtn.textContent = datesExpanded ? "Show fewer dates" : `View more (${sortedRows.length - 3} more)`;
  dateStrip.innerHTML = visibleRows.map((item) => `<button class="date-pill ${item.date===currentDate?"active":""}" type="button" data-date="${escapeHtml(item.date)}"><span class="date-label">${escapeHtml(item.date)}</span><span class="date-meta">${item.fixtures} fixtures | ${item.predictions} predicted</span></button>`).join("");
  dateStrip.querySelectorAll("[data-date]").forEach((button) => button.addEventListener("click", () => loadDate(button.dataset.date, false)));
}

function applyFilters(picks) {
  const league = filterLeague?.value || "all";
  const risk = filterRisk?.value || "all";
  const minStars = Number(filterStars?.value || 0);
  const value = filterValue?.value || "all";
  return picks.filter((p) => (league === "all" || String(p.league||"") === league) && (risk === "all" || String(p.risk_tier||"") === risk) && (!minStars || Number(p.confidence_stars||0) >= minStars) && (value === "all" || String(p.value_status||"") === value));
}

function renderCompare(filtered) {
  if (!compareMode || !selectedForCompare.size) {
    comparePanel?.classList.add("hidden");
    if (compareGrid) compareGrid.innerHTML = "";
    return;
  }
  const selected = [...selectedForCompare].map((i) => filtered[Number(i)]).filter(Boolean);
  comparePanel?.classList.remove("hidden");
  if (compareGrid) compareGrid.innerHTML = selected.map((p)=>`<article class="empty-card"><h3>${escapeHtml(p.match)}</h3><p class="meta">${escapeHtml(p.market)} ${escapeHtml(p.selection||"")}</p><p class="meta">Prob ${p.probability_pct || 0}% | ${stars(p.confidence_stars)} | ${escapeHtml(p.risk_tier || "Unavailable")}</p><p class="meta">Value: ${escapeHtml(p.value_status || "Odds unavailable")}</p><p class="meta">${escapeHtml(p.explanation || "Not available from current data source")}</p></article>`).join("");
}

function renderPicks(payload) {
  if (!payload || !Array.isArray(payload.picks) || !payload.picks.length) {
    picksNode.innerHTML = `<article class="empty-card"><h3>No board entries for ${escapeHtml(currentDate)}</h3><p>${escapeHtml(payload?.note || "No ranked opportunities are stored for this date yet.")}</p></article>`;
    return;
  }
  cachedPicks = payload.picks || [];
  const leagues = [...new Set(cachedPicks.map((p) => String(p.league || "")).filter(Boolean))];
  if (filterLeague) {
    const prev = filterLeague.value || "all";
    filterLeague.innerHTML = `<option value="all">All leagues</option>${leagues.map((l)=>`<option value="${escapeHtml(l)}">${escapeHtml(l)}</option>`).join("")}`;
    filterLeague.value = leagues.includes(prev) ? prev : "all";
  }
  const filtered = applyFilters(cachedPicks);
  picksNode.innerHTML = filtered.map((pick, idx) => `<article class="pick-card" data-index="${idx}" data-confidence="${escapeHtml((pick.confidence || 'medium').toLowerCase())}"><div class="pick-head"><span class="badge">${escapeHtml((pick.confidence_band || pick.confidence || "medium"))}</span><span class="edge">${pick.edge >= 0 ? "+" : ""}${formatPercent(pick.edge)} edge</span></div><h3>${escapeHtml(pick.home_team || "")} vs ${escapeHtml(pick.away_team || "")}</h3><p class="meta">${escapeHtml(pick.league || "Unknown league")} | ${escapeHtml(pick.kickoff || pick.date || "Unavailable")}</p><p class="selection">${escapeHtml(pick.market)}: <strong>${escapeHtml(pick.selection || "")}</strong></p><p class="meta">${stars(pick.confidence_stars)} | Risk: ${escapeHtml(pick.risk_tier || "Unavailable")} | Value: ${escapeHtml(pick.value_status || "Odds unavailable")}</p><div class="prob-track"><div class="prob-fill" style="width:${Math.max(0, Math.min(100, Number(pick.probability_pct || 0)))}%;"></div></div><div class="stats"><span>Prob ${pick.probability_pct ?? (Number(pick.probability||0)*100).toFixed(1)}%</span><span>Stake ${Number(pick.stake ?? 0).toFixed(2)}</span><span>Trend ${escapeHtml(pick.last_5_home || "Unavailable")} / ${escapeHtml(pick.last_5_away || "Unavailable")}</span></div><div class="meta hidden details">${escapeHtml(pick.recent_trend_summary || "Unavailable")}. ${escapeHtml(pick.explanation || "Not available from current data source")}</div></article>`).join("");
  picksNode.querySelectorAll(".pick-card").forEach((card) => {
    card.addEventListener("click", () => {
      card.querySelector(".details")?.classList.toggle("hidden");
      if (!compareMode) return;
      const idx = card.dataset.index;
      if (!selectedForCompare.has(idx) && selectedForCompare.size >= 2) {
        statusLine.textContent = "You can compare up to 2 matches at a time.";
        return;
      }
      if (selectedForCompare.has(idx)) { selectedForCompare.delete(idx); card.style.outline = "none"; }
      else { selectedForCompare.add(idx); card.style.outline = "2px solid #d4af37"; }
      renderCompare(filtered);
    });
  });
  renderCompare(filtered);
}

async function fetchJson(url) { const r = await fetch(url, {cache:"no-store"}); if(!r.ok) throw new Error(`Request failed: ${r.status}`); return r.json(); }
function setBusyState(v){ runDateBtn.disabled=v; dateInput.disabled=v; toggleDatesBtn.disabled=v; }

async function refreshDates(preferredDate = null) {
  const payload = await fetchJson("/dates/available?predicted_only=false");
  availableDates = Array.isArray(payload?.dates) ? payload.dates : [];
  if (preferredDate) currentDate = preferredDate;
  else if (!availableDates.some((i) => i.date === currentDate)) currentDate = chooseInitialDate(availableDates);
  dateInput.value = currentDate; renderDateStrip(availableDates); updateLinks(currentDate);
  currentDateLabel.textContent = availableDates.length ? `Showing ${currentDate}` : "No stored match dates yet";
}

async function loadDate(dateValue, refreshDatesFirst = true) {
  currentDate = dateValue || initialToday; dateInput.value = currentDate; updateLinks(currentDate);
  statusLine.textContent = `Refreshing insights for ${currentDate}...`; currentDateLabel.textContent = `Showing ${currentDate}`;
  if (refreshDatesFirst) await refreshDates(currentDate); else renderDateStrip(availableDates);
  const payload = await fetchJson(`/predictions/top?date=${encodeURIComponent(currentDate)}`); renderPicks(payload);
  topPicksCaption.textContent = payload?.count ? `Showing ${payload.count} ranked opportunities for ${currentDate}` : `No ranked opportunities stored for ${currentDate}`;
}

async function runSelectedDate() {
  const targetDate = dateInput.value || currentDate || initialToday; currentDate = targetDate; updateLinks(currentDate); setBusyState(true);
  statusLine.textContent = `Running selected date for ${currentDate}...`;
  try { await fetchJson(`/run?source=hybrid&date=${encodeURIComponent(currentDate)}`); await refreshDates(currentDate); await loadDate(currentDate, false); }
  catch (e) { statusLine.textContent = `Couldn't run ${currentDate}: ${e.message || "Unknown error"}`; }
  finally { setBusyState(false); }
}

compareToggle?.addEventListener("click", () => { compareMode = !compareMode; compareToggle.textContent = `Comparison: ${compareMode ? "ON" : "OFF"}`; if(!compareMode){selectedForCompare.clear(); document.querySelectorAll('.pick-card').forEach(c=>c.style.outline='none'); renderCompare([]);} });
[filterLeague, filterRisk, filterStars, filterValue].forEach((n) => n?.addEventListener("change", () => renderPicks({picks: cachedPicks})));

function updateCalculator(){ const stake=Number(calcStake?.value||0); const odds=Number(calcOdds?.value||0); const payout=stake*odds; const profit=payout-stake; if(calcOutput) calcOutput.textContent=`Payout = stake × odds = ${payout.toFixed(2)} | Profit = payout − stake = ${profit.toFixed(2)}`; }
calcStake?.addEventListener("input", updateCalculator); calcOdds?.addEventListener("input", updateCalculator);
runDateBtn.addEventListener("click", () => runSelectedDate());
toggleDatesBtn.addEventListener("click", () => { datesExpanded = !datesExpanded; renderDateStrip(availableDates); });
dateInput.addEventListener("change", () => loadDate(dateInput.value || currentDate || initialToday, true));
refreshDates().then(() => loadDate(currentDate || initialToday, false));
refreshHandle = window.setInterval(() => { if (!refreshInFlight) { refreshInFlight = true; loadDate(currentDate, true).finally(()=>refreshInFlight=false);} }, refreshMs);
window.addEventListener("beforeunload", () => refreshHandle && window.clearInterval(refreshHandle));
