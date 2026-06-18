(() => {
  const bootstrapNode = document.getElementById("reports-bootstrap");
  if (!bootstrapNode) {
    return;
  }

  const bootstrap = JSON.parse(bootstrapNode.textContent || "{}");
  const state = {
    reports: Array.isArray(bootstrap.reports) ? bootstrap.reports : [],
    filtered: [],
    selectedId: bootstrap.latest_report_id || null,
    search: "",
    sort: "generated_at",
    order: "desc",
    successFilter: "all",
    positiveOnly: false,
    aiOnly: false,
    realOnly: false,
  };

  const nodes = {
    summaryGrid: document.getElementById("reports-summary-grid"),
    list: document.getElementById("reports-list"),
    detail: document.getElementById("report-detail"),
    resultsCount: document.getElementById("reports-results-count"),
    search: document.getElementById("reports-search"),
    sort: document.getElementById("reports-sort"),
    order: document.getElementById("reports-order"),
    successFilter: document.getElementById("reports-success-filter"),
    positiveOnly: document.getElementById("reports-positive-only"),
    aiOnly: document.getElementById("reports-ai-only"),
    realOnly: document.getElementById("reports-real-only"),
  };

  seedSortOptions();
  bindEvents();
  renderSummary();
  applyFiltersAndRender();

  function seedSortOptions() {
    const options = Array.isArray(bootstrap.sort_options) ? bootstrap.sort_options : [];
    nodes.sort.innerHTML = options
      .map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`)
      .join("");
    nodes.sort.value = state.sort;
  }

  function bindEvents() {
    nodes.search?.addEventListener("input", () => {
      state.search = nodes.search.value.trim().toLowerCase();
      applyFiltersAndRender();
    });
    nodes.sort?.addEventListener("change", () => {
      state.sort = nodes.sort.value;
      applyFiltersAndRender();
    });
    nodes.order?.addEventListener("change", () => {
      state.order = nodes.order.value;
      applyFiltersAndRender();
    });
    nodes.successFilter?.addEventListener("change", () => {
      state.successFilter = nodes.successFilter.value;
      applyFiltersAndRender();
    });
    nodes.positiveOnly?.addEventListener("change", () => {
      state.positiveOnly = nodes.positiveOnly.checked;
      applyFiltersAndRender();
    });
    nodes.aiOnly?.addEventListener("change", () => {
      state.aiOnly = nodes.aiOnly.checked;
      applyFiltersAndRender();
    });
    nodes.realOnly?.addEventListener("change", () => {
      state.realOnly = nodes.realOnly.checked;
      applyFiltersAndRender();
    });
    nodes.list?.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-report-id]") : null;
      if (!target) {
        return;
      }
      state.selectedId = target.getAttribute("data-report-id");
      renderList();
      renderDetail();
    });
  }

  function renderSummary() {
    const summary = bootstrap.summary || {};
    const cards = [
      ["Reports", summary.total_reports ?? 0],
      ["Successful", summary.successful_reports ?? 0],
      ["Channels", summary.channels ?? 0],
      ["Average Score", formatNumber(summary.avg_score)],
      ["Best Score", formatNumber(summary.best_score)],
      ["Average Win Rate", `${formatNumber(summary.avg_win_rate_pct)}%`],
      ["Positive PnL", summary.positive_pnl_reports ?? 0],
    ];
    nodes.summaryGrid.innerHTML = cards
      .map(
        ([label, value]) => `
          <div class="report-summary-card">
            <span>${escapeHtml(String(label))}</span>
            <strong>${escapeHtml(String(value))}</strong>
          </div>
        `
      )
      .join("");
  }

  function applyFiltersAndRender() {
    const filtered = state.reports
      .filter((report) => matchesFilters(report))
      .sort(compareReports);
    state.filtered = filtered;
    if (!filtered.some((report) => report.report_id === state.selectedId)) {
      state.selectedId = filtered[0]?.report_id || null;
    }
    renderList();
    renderDetail();
  }

  function matchesFilters(report) {
    if (state.search) {
      const haystack = `${report.channel} ${report.channel_label}`.toLowerCase();
      if (!haystack.includes(state.search)) {
        return false;
      }
    }
    if (state.successFilter === "successful" && !report.success) {
      return false;
    }
    if (state.successFilter === "failed" && report.success) {
      return false;
    }
    if (state.positiveOnly && !(report.total_pnl_value > 0)) {
      return false;
    }
    if (state.aiOnly && !report.ai_used) {
      return false;
    }
    if (state.realOnly && !report.real_telegram_used) {
      return false;
    }
    return true;
  }

  function compareReports(left, right) {
    const field = state.sort;
    const direction = state.order === "asc" ? 1 : -1;
    const leftValue = normalizeSortValue(left[field]);
    const rightValue = normalizeSortValue(right[field]);
    if (leftValue < rightValue) {
      return -1 * direction;
    }
    if (leftValue > rightValue) {
      return 1 * direction;
    }
    return 0;
  }

  function normalizeSortValue(value) {
    if (typeof value === "number") {
      return value;
    }
    if (typeof value === "string") {
      const numeric = Number(value);
      return Number.isNaN(numeric) ? value : numeric;
    }
    return 0;
  }

  function renderList() {
    nodes.resultsCount.textContent = `${state.filtered.length} reports`;
    if (!state.filtered.length) {
      nodes.list.innerHTML = '<div class="empty-state-box">No reports match the current filters.</div>';
      return;
    }
    nodes.list.innerHTML = state.filtered.map(renderReportCard).join("");
  }

  function renderReportCard(report) {
    const active = report.report_id === state.selectedId;
    return `
      <button type="button" class="report-card ${active ? "active" : ""}" data-report-id="${escapeHtml(report.report_id)}">
        <div class="report-card-head">
          <div>
            <p class="eyebrow">Report</p>
            <h3>${escapeHtml(report.channel_label)}</h3>
            <div class="subtle">${escapeHtml(formatDate(report.generated_at))}</div>
          </div>
          <div class="report-badge-row">
            <span class="report-status-pill ${report.success ? "success" : "failed"}">${escapeHtml(report.success_label)}</span>
            <span class="report-score-pill">Score ${escapeHtml(report.score_label)}</span>
          </div>
        </div>
        <div class="report-card-score-row">
          ${renderScoreGauge(report.score_value, report.score_label)}
          <div class="report-mini-metrics">
            ${miniMetric("PnL", report.total_pnl_label)}
            ${miniMetric("Win Rate", report.win_rate_label)}
            ${miniMetric("Profit Factor", report.profit_factor_label)}
            ${miniMetric("Fill Rate", report.fill_rate_label)}
          </div>
        </div>
        <div class="report-card-meta">
          <span>${escapeHtml(report.from_date)} → ${escapeHtml(report.to_date)}</span>
          <span>${escapeHtml(String(report.trades_filled))} / ${escapeHtml(String(report.trades_simulated))} filled</span>
          <span>${escapeHtml(String(report.total_messages))} messages</span>
        </div>
      </button>
    `;
  }

  function renderDetail() {
    const report = state.filtered.find((item) => item.report_id === state.selectedId);
    if (!report) {
      nodes.detail.innerHTML = '<div class="report-detail-empty">Select a report to inspect its details.</div>';
      return;
    }
    nodes.detail.innerHTML = `
      <div class="report-detail-stack">
        <section class="report-detail-hero">
          <div class="report-detail-head">
            <div>
              <p class="eyebrow">Selected Report</p>
              <h2 class="report-detail-title">${escapeHtml(report.channel_label)}</h2>
              <p class="subtle">${escapeHtml(formatDate(report.generated_at))}</p>
            </div>
            ${renderScoreGauge(report.score_value, report.score_label)}
          </div>
          <div class="report-detail-tags">
            <span class="report-status-pill ${report.success ? "success" : "failed"}">${escapeHtml(report.success_label)}</span>
            <span class="report-badge">${report.ai_used ? "AI" : "No AI"}</span>
            <span class="report-badge">${report.real_telegram_used ? "Real Telegram" : "No Real Telegram"}</span>
            <span class="report-badge">${report.real_market_data_used ? "Real Market Data" : "No Real Market Data"}</span>
          </div>
        </section>

        <section>
          <div class="report-metric-grid">
            ${detailMetric("Total PnL", report.total_pnl_label)}
            ${detailMetric("Win Rate", report.win_rate_label)}
            ${detailMetric("Profit Factor", report.profit_factor_label)}
            ${detailMetric("Max Drawdown", report.max_drawdown_label)}
            ${detailMetric("Trades Filled", `${report.trades_filled}/${report.trades_simulated}`)}
            ${detailMetric("Balances", `${formatNumber(report.initial_balance)} → ${formatNumber(report.final_balance)}`)}
          </div>
        </section>

        <section class="report-chart-grid">
          <div class="report-chart-card">
            <span>Equity Curve</span>
            <h3>Balance Progression</h3>
            ${renderEquityCurve(report.equity_curve)}
          </div>
          <div class="report-chart-card">
            <span>Trade Outcomes</span>
            <h3>Status Distribution</h3>
            ${renderStatusBars(report.trade_status_counts)}
          </div>
        </section>

        <section>
          <div class="section-head compact">
            <div>
              <p class="eyebrow">Score Logic</p>
              <h3>Breakdown</h3>
            </div>
          </div>
          <div class="report-breakdown-grid">
            ${renderScoreBreakdown(report.score_breakdown)}
          </div>
        </section>

        <section>
          <div class="section-head compact">
            <div>
              <p class="eyebrow">Symbols</p>
              <h3>Best And Worst Contributors</h3>
            </div>
          </div>
          <div class="report-symbol-list">
            ${renderSymbolSummary(report.symbol_summary)}
          </div>
        </section>

        <section>
          <div class="section-head compact">
            <div>
              <p class="eyebrow">Trades</p>
              <h3>Full Trade Table</h3>
            </div>
          </div>
          <div class="report-trade-table">
            ${renderTrades(report.trades)}
          </div>
        </section>

        ${renderNoteBlock("Warnings", report.warnings)}
        ${renderNoteBlock("Errors", report.errors)}
        ${renderNoteBlock("Skipped Reasons", report.skipped_reasons)}
      </div>
    `;
  }

  function renderScoreGauge(scoreValue, scoreLabel) {
    const angle = `${Math.max(0, Math.min(scoreValue, 100)) * 3.6}deg`;
    return `
      <div class="score-gauge" style="--gauge-angle:${angle}">
        <div>
          <strong>${escapeHtml(scoreLabel)}</strong>
          <span>/ 100</span>
        </div>
      </div>
    `;
  }

  function miniMetric(label, value) {
    return `
      <div class="report-mini-metric">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(String(value))}</strong>
      </div>
    `;
  }

  function detailMetric(label, value) {
    return `
      <div class="report-metric-card">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(String(value))}</strong>
      </div>
    `;
  }

  function renderScoreBreakdown(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return '<div class="empty-state-box">No score breakdown is available for this report.</div>';
    }
    return rows
      .map((row) => `
        <div class="report-breakdown-card">
          <span>${escapeHtml(row.label)}</span>
          <strong>${escapeHtml(row.label_value)}</strong>
          <div class="report-breakdown-bar"><i style="width:${Math.max(0, Math.min(row.value, 100))}%"></i></div>
        </div>
      `)
      .join("");
  }

  function renderEquityCurve(points) {
    if (!Array.isArray(points) || points.length === 0) {
      return '<div class="report-chart-empty">No equity data yet.</div>';
    }
    const values = points.map((point) => point.equity_value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const width = 520;
    const height = 170;
    const pad = 14;
    const xStep = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
    const scaleY = (value) => {
      if (max === min) {
        return height / 2;
      }
      const ratio = (value - min) / (max - min);
      return height - pad - ratio * (height - pad * 2);
    };
    const polyline = points
      .map((point, index) => `${pad + (index * xStep)},${scaleY(point.equity_value)}`)
      .join(" ");
    return `
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Equity curve">
        <defs>
          <linearGradient id="equity-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="rgba(14, 124, 102, 0.32)"></stop>
            <stop offset="100%" stop-color="rgba(14, 124, 102, 0.02)"></stop>
          </linearGradient>
        </defs>
        <polyline
          fill="none"
          stroke="#0e7c66"
          stroke-width="4"
          stroke-linecap="round"
          stroke-linejoin="round"
          points="${polyline}"
        ></polyline>
      </svg>
    `;
  }

  function renderStatusBars(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return '<div class="report-chart-empty">No trade status data yet.</div>';
    }
    const max = Math.max(...rows.map((row) => row.count), 1);
    return `
      <div class="report-horizontal-bars">
        ${rows
          .map((row) => `
            <div class="report-horizontal-row">
              <div class="report-status-card">
                <span>${escapeHtml(row.status)}</span>
                <strong>${escapeHtml(String(row.count))}</strong>
              </div>
              <div class="report-horizontal-track"><i style="width:${(row.count / max) * 100}%"></i></div>
            </div>
          `)
          .join("")}
      </div>
    `;
  }

  function renderSymbolSummary(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return '<div class="empty-state-box">No symbol summary is available.</div>';
    }
    return rows
      .slice(0, 8)
      .map((row) => `
        <div class="report-symbol-row">
          <div>
            <strong>${escapeHtml(row.symbol || "unknown")}</strong>
            <div class="subtle">${escapeHtml(String(row.trades))} trades • ${escapeHtml(String(row.wins))} wins • ${escapeHtml(String(row.losses))} losses</div>
          </div>
          <div class="${row.pnl_value >= 0 ? "pnl-positive" : "pnl-negative"}"><strong>${escapeHtml(row.pnl_label)}</strong></div>
        </div>
      `)
      .join("");
  }

  function renderTrades(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return '<div class="empty-state-box">No trade rows are available.</div>';
    }
    return rows
      .map((trade) => `
        <div class="report-trade-row">
          <div>
            <div class="report-trade-main">
              <strong>${escapeHtml(trade.symbol || "unknown")}</strong>
              <span class="report-badge">${escapeHtml(trade.side || "unknown")}</span>
              <span class="report-badge">${escapeHtml(trade.status || "unknown")}</span>
            </div>
            <div class="report-trade-sub">
              <span>Entry: ${escapeHtml(String(trade.entry_price ?? "n/a"))}</span>
              <span>Exit: ${escapeHtml(String(trade.exit_price ?? "n/a"))}</span>
              <span>Qty: ${escapeHtml(trade.quantity_label)}</span>
            </div>
            ${trade.notes?.length ? `<div class="report-trade-sub">${trade.notes.map((note) => `<span>${escapeHtml(note)}</span>`).join("")}</div>` : ""}
          </div>
          <div class="${trade.pnl_value >= 0 ? "pnl-positive" : "pnl-negative"}"><strong>${escapeHtml(trade.pnl_label)}</strong></div>
        </div>
      `)
      .join("");
  }

  function renderNoteBlock(title, items) {
    if (!Array.isArray(items) || !items.length) {
      return "";
    }
    return `
      <section class="report-note-block">
        <h3>${escapeHtml(title)}</h3>
        <ul class="report-note-list">
          ${items.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}
        </ul>
      </section>
    `;
  }

  function formatDate(value) {
    if (!value) {
      return "n/a";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString();
  }

  function formatNumber(value) {
    const numeric = Number(value || 0);
    return Number.isFinite(numeric) ? numeric.toFixed(2) : "0.00";
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }
})();
