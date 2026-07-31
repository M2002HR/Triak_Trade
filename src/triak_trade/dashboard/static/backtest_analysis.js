(() => {
  const form = document.getElementById("backtest-analysis-filters");
  if (!form) {
    return;
  }

  const state = {
    data: null,
    charts: new Map(),
    requestSequence: 0,
  };
  const nodes = {
    form,
    channel: document.getElementById("analysis-channel"),
    strategy: document.getElementById("analysis-strategy"),
    fillPolicy: document.getElementById("analysis-fill-policy"),
    interval: document.getElementById("analysis-interval"),
    status: document.getElementById("analysis-status"),
    dateFrom: document.getElementById("analysis-date-from"),
    dateTo: document.getElementById("analysis-date-to"),
    minSignals: document.getElementById("analysis-min-signals"),
    sortBy: document.getElementById("analysis-sort-by"),
    sortOrder: document.getElementById("analysis-sort-order"),
    reset: document.getElementById("analysis-reset"),
    refreshStatus: document.getElementById("analysis-refresh-status"),
    overview: document.getElementById("analysis-overview"),
    quality: document.getElementById("analysis-data-quality"),
    rankings: document.getElementById("analysis-channel-rankings"),
    runs: document.getElementById("analysis-run-comparison"),
    parameters: document.getElementById("analysis-parameter-impact"),
    strategies: document.getElementById("analysis-strategy-comparison"),
    signals: document.getElementById("analysis-signal-extremes"),
    methodology: document.getElementById("analysis-methodology"),
    empty: document.getElementById("analysis-empty"),
    heroRuns: document.getElementById("analysis-hero-runs"),
    heroSignals: document.getElementById("analysis-hero-signals"),
    heroChannels: document.getElementById("analysis-hero-channels"),
  };

  restoreFiltersFromUrl();
  bindEvents();
  loadAnalysis();

  function bindEvents() {
    nodes.form.addEventListener("submit", (event) => {
      event.preventDefault();
      loadAnalysis({ updateUrl: true });
    });
    nodes.reset.addEventListener("click", () => {
      nodes.form.reset();
      nodes.minSignals.value = "0";
      loadAnalysis({ updateUrl: true });
    });
    document.querySelectorAll("[data-analysis-tab]").forEach((button) => {
      button.addEventListener("click", () => openTab(button.dataset.analysisTab || "runs"));
    });
    nodes.rankings.addEventListener("click", (event) => {
      const target = event.target instanceof Element
        ? event.target.closest("[data-filter-channel]")
        : null;
      if (!target) {
        return;
      }
      nodes.channel.value = target.getAttribute("data-filter-channel") || "";
      loadAnalysis({ updateUrl: true });
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    window.addEventListener("resize", resizeCharts);
  }

  async function loadAnalysis(options = {}) {
    const sequence = ++state.requestSequence;
    nodes.refreshStatus.textContent = "Calculating persisted analytics…";
    nodes.refreshStatus.className = "counter-pill working";
    const params = buildQuery();
    if (options.updateUrl) {
      const url = new URL(window.location.href);
      const token = url.searchParams.get("token");
      url.search = params.toString();
      if (token) {
        url.searchParams.set("token", token);
      }
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
    try {
      const response = await fetch(withAuthPath(`/api/backtests/analysis?${params}`));
      const payload = await response.json();
      if (sequence !== state.requestSequence) {
        return;
      }
      if (!response.ok) {
        throw new Error(payload.detail || "Analysis request failed");
      }
      state.data = payload;
      populateFilterOptions(payload.filter_options || {});
      render(payload);
      nodes.refreshStatus.textContent = `Updated ${formatDate(payload.generated_at)}`;
      nodes.refreshStatus.className = "counter-pill success";
    } catch (error) {
      nodes.refreshStatus.textContent = error instanceof Error ? error.message : "Analysis failed";
      nodes.refreshStatus.className = "counter-pill error";
      nodes.quality.innerHTML = '<article class="analysis-quality-item danger"><strong>Analysis unavailable</strong><span>The persisted data could not be read safely.</span></article>';
    }
  }

  function buildQuery() {
    const params = new URLSearchParams();
    const values = {
      channel: nodes.channel.value,
      strategy: nodes.strategy.value,
      fill_policy: nodes.fillPolicy.value,
      interval: nodes.interval.value,
      status: nodes.status.value,
      date_from: localInputToIso(nodes.dateFrom.value),
      date_to: localInputToIso(nodes.dateTo.value),
      min_signals: nodes.minSignals.value || "0",
      sort_by: nodes.sortBy.value || "score",
      sort_order: nodes.sortOrder.value || "desc",
    };
    Object.entries(values).forEach(([key, value]) => {
      if (value !== "" && value !== null) {
        params.set(key, String(value));
      }
    });
    return params;
  }

  function restoreFiltersFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const mappings = [
      [nodes.channel, "channel"],
      [nodes.strategy, "strategy"],
      [nodes.fillPolicy, "fill_policy"],
      [nodes.interval, "interval"],
      [nodes.status, "status"],
      [nodes.minSignals, "min_signals"],
      [nodes.sortBy, "sort_by"],
      [nodes.sortOrder, "sort_order"],
    ];
    mappings.forEach(([node, key]) => {
      if (params.has(key)) {
        node.dataset.pendingValue = params.get(key) || "";
        if (["min_signals", "sort_by", "sort_order", "status"].includes(key)) {
          node.value = params.get(key) || "";
        }
      }
    });
    nodes.dateFrom.value = isoToLocalInput(params.get("date_from"));
    nodes.dateTo.value = isoToLocalInput(params.get("date_to"));
  }

  function populateFilterOptions(options) {
    setSelectOptions(nodes.channel, options.channels, "All channels");
    setSelectOptions(nodes.strategy, options.strategies, "All strategies");
    setSelectOptions(nodes.fillPolicy, options.fill_policies, "All fill policies");
    setSelectOptions(nodes.interval, options.intervals, "All intervals");
    const statuses = Array.isArray(options.statuses) ? options.statuses : [];
    setSelectOptions(nodes.status, statuses, "All statuses", true);
  }

  function setSelectOptions(node, values, allLabel, preserveExisting = false) {
    const current = node.dataset.pendingValue || node.value;
    const source = Array.isArray(values) ? values : [];
    const normalized = preserveExisting
      ? [...new Set([...Array.from(node.options).map((option) => option.value).filter(Boolean), ...source])]
      : source;
    node.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>${normalized
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(humanize(value))}</option>`)
      .join("")}`;
    if (normalized.includes(current)) {
      node.value = current;
    }
    delete node.dataset.pendingValue;
  }

  function render(data) {
    const overview = data.overview || {};
    nodes.heroRuns.textContent = formatInteger(overview.completed_runs);
    nodes.heroSignals.textContent = formatInteger(overview.signals);
    nodes.heroChannels.textContent = formatInteger(overview.channels);
    nodes.empty.hidden = !data.empty;
    renderQuality(data);
    renderOverview(overview);
    renderRankings(data.channel_rankings || []);
    renderRuns(data.run_comparison || []);
    renderParameters(data.parameter_impact || []);
    renderStrategies(data.strategy_comparison || []);
    renderSignals(data.best_signals || [], data.worst_signals || []);
    renderMethodology(data.methodology || {});
    renderChartsWhenReady(data, 0);
  }

  function renderQuality(data) {
    const overview = data.overview || {};
    const status = data.status_counts || {};
    const sources = data.data_sources || {};
    const activeLink = overview.latest_active_run_id
      ? `<a href="/backtests?run_id=${encodeURIComponent(overview.latest_active_run_id)}">Resume monitor</a>`
      : "No active worker";
    nodes.quality.innerHTML = `
      <article class="analysis-quality-item ${Number(status.failed || 0) ? "warning" : "success"}">
        <strong>${formatInteger(status.completed || 0)} completed / ${formatInteger(status.failed || 0)} failed</strong>
        <span>Failed runs stay visible but are excluded from performance scoring.</span>
      </article>
      <article class="analysis-quality-item">
        <strong>${formatInteger(overview.configurations)} configurations</strong>
        <span>Repeated parameter search increases the selection-risk penalty.</span>
      </article>
      <article class="analysis-quality-item ${Number(overview.active_runs || 0) ? "working" : ""}">
        <strong>${formatInteger(overview.active_runs)} active workers</strong>
        <span>${activeLink}</span>
      </article>
      <article class="analysis-quality-item">
        <strong>${formatInteger(sources.dashboard_runs)} dashboard + ${formatInteger(sources.report_only_runs)} report-only runs</strong>
        <span>${formatInteger(sources.skipped_invalid_reports)} invalid reports skipped safely; matching dashboard reports are deduplicated.</span>
      </article>
    `;
  }

  function renderOverview(overview) {
    const cards = [
      ["Total PnL", money(overview.total_pnl), pnlClass(overview.total_pnl)],
      ["Allocated-capital return", percent(overview.return_pct), pnlClass(overview.return_pct)],
      ["Persisted signals", formatInteger(overview.signals), ""],
      ["Average run score", score(overview.average_run_score), scoreClass(overview.average_run_score)],
      ["Completed runs", formatInteger(overview.completed_runs), ""],
      ["Tested configurations", formatInteger(overview.configurations), ""],
      ["Channels", formatInteger(overview.channels), ""],
      ["Strategies", formatInteger(overview.strategies), ""],
    ];
    nodes.overview.innerHTML = cards.map(([label, value, css]) => `
      <article class="metric-card analysis-metric-card">
        <span>${escapeHtml(label)}</span><strong class="${css}">${escapeHtml(value)}</strong>
      </article>
    `).join("");
  }

  function renderRankings(rows) {
    if (!rows.length) {
      nodes.rankings.innerHTML = "No channels match this analysis scope.";
      return;
    }
    nodes.rankings.classList.remove("empty-state-box");
    nodes.rankings.innerHTML = table(
      ["Rank", "Channel", "Score / confidence", "Runs / configs", "Signals", "PnL", "Return", "Win / fill", "Avg profit / loss", "Worst DD", "Selection penalty", ""],
      rows.map((row) => [
        `#${row.rank}`,
        `<strong>${escapeHtml(row.channel)}</strong><small>${escapeHtml(row.score_grade || "")}</small>`,
        `<span class="score-badge ${scoreClass(row.score)}">${escapeHtml(score(row.score))}</span><small>${escapeHtml(row.confidence || "low")} evidence</small>`,
        `${formatInteger(row.completed_runs)} / ${formatInteger(row.configuration_count)}<small>${formatInteger(row.failed_runs)} failed</small>`,
        formatInteger(row.signals),
        `<span class="${pnlClass(row.total_pnl)}">${escapeHtml(money(row.total_pnl))}</span>`,
        `<span class="${pnlClass(row.return_pct)}">${escapeHtml(percent(row.return_pct))}</span>`,
        `${percent(row.win_rate)} / ${percent(row.fill_rate)}`,
        `<span class="pnl-positive">${escapeHtml(money(row.average_profit))}</span><small class="pnl-negative">${escapeHtml(money(row.average_loss))}</small>`,
        percent(row.worst_drawdown_pct),
        `−${score(row.selection_penalty)}`,
        `<button type="button" class="ghost-button compact-action" data-filter-channel="${escapeHtml(row.channel)}">Analyze</button>`,
      ]),
    );
  }

  function renderRuns(rows) {
    if (!rows.length) {
      nodes.runs.innerHTML = "No completed runs are available for scenario comparison.";
      return;
    }
    nodes.runs.classList.remove("empty-state-box");
    nodes.runs.innerHTML = table(
      ["Run", "Channel / strategy", "Score", "Signals", "PnL / return", "Win / fill", "Avg P/L", "Max P/L", "Drawdown", "Tail risk", "Parameters"],
      rows.map((row) => [
        `${runReference(row)}<small>${escapeHtml(formatDate(row.created_at))}</small>`,
        `${escapeHtml(row.channel)}<small>${escapeHtml(humanize(row.strategy))}</small>`,
        `<span class="score-badge ${scoreClass(row.score)}">${escapeHtml(score(row.score))}</span><small>${escapeHtml(row.score_grade)}</small>`,
        `${formatInteger(row.signals)}<small>${formatInteger(row.filled_signals)} filled</small>`,
        `<span class="${pnlClass(row.total_pnl)}">${escapeHtml(money(row.total_pnl))}</span><small class="${pnlClass(row.return_pct)}">${escapeHtml(percent(row.return_pct))}</small>`,
        `${percent(row.win_rate)} / ${percent(row.fill_rate)}`,
        `<span class="pnl-positive">${escapeHtml(money(row.average_profit))}</span><small class="pnl-negative">${escapeHtml(money(row.average_loss))}</small>`,
        `<span class="pnl-positive">${escapeHtml(money(row.maximum_profit))}</span><small class="pnl-negative">${escapeHtml(money(row.maximum_loss))}</small>`,
        `${money(row.max_drawdown)}<small>${percent(row.max_drawdown_pct)}</small>`,
        `${percent(row.expected_shortfall_pct)}<small>downside dev ${percent(row.downside_deviation_pct)}</small>`,
        parameterDetails(row.parameters || {}),
      ]),
    );
  }

  function renderParameters(dimensions) {
    if (!dimensions.length) {
      nodes.parameters.innerHTML = '<div class="empty-state-box">No completed runs expose comparable parameters.</div>';
      return;
    }
    nodes.parameters.innerHTML = dimensions.map((dimension) => `
      <article class="parameter-impact-card ${dimension.comparable ? "" : "single-variant"}">
        <div class="section-head compact">
          <div><p class="eyebrow">${escapeHtml(dimension.key)}</p><h3>${escapeHtml(dimension.label)}</h3></div>
          <span class="counter-pill">${formatInteger(dimension.variation_count)} variants</span>
        </div>
        ${dimension.comparable ? "" : '<p class="analysis-caution">Only one value exists in this scope; this is descriptive, not a causal comparison.</p>'}
        ${table(
          ["Value", "Runs / signals", "Avg score", "Avg return", "Delta return", "Avg DD", "Positive runs", "Evidence"],
          dimension.values.map((row) => [
            `<strong>${escapeHtml(row.value)}</strong>`,
            `${formatInteger(row.runs)} / ${formatInteger(row.signals)}`,
            score(row.average_score),
            `<span class="${pnlClass(row.average_return_pct)}">${escapeHtml(percent(row.average_return_pct))}</span>`,
            `<span class="${pnlClass(row.delta_return_pct)}">${escapeHtml(signedPercent(row.delta_return_pct))}</span>`,
            percent(row.average_drawdown_pct),
            percent(row.positive_run_rate),
            `${escapeHtml(row.confidence)}<small>${score(row.evidence_score)}</small>`,
          ]),
        )}
      </article>
    `).join("");
  }

  function renderStrategies(rows) {
    if (!rows.length) {
      nodes.strategies.innerHTML = '<div class="empty-state-box">No completed strategy scenarios.</div>';
      return;
    }
    nodes.strategies.innerHTML = table(
      ["Strategy", "Runs", "Signals", "Avg score", "Avg PnL", "Avg / median return", "Avg drawdown", "Positive runs", "Evidence"],
      rows.map((row) => [
        `<strong>${escapeHtml(humanize(row.value))}</strong>`,
        formatInteger(row.runs),
        formatInteger(row.signals),
        score(row.average_score),
        `<span class="${pnlClass(row.average_pnl)}">${escapeHtml(money(row.average_pnl))}</span>`,
        `${percent(row.average_return_pct)} / ${percent(row.median_return_pct)}`,
        percent(row.average_drawdown_pct),
        percent(row.positive_run_rate),
        `${escapeHtml(row.confidence)} (${score(row.evidence_score)})`,
      ]),
    );
  }

  function renderSignals(best, worst) {
    nodes.signals.innerHTML = `
      <section><div class="section-head compact"><div><p class="eyebrow">Upside</p><h3>Best Persisted Signals</h3></div></div>${signalTable(best)}</section>
      <section><div class="section-head compact"><div><p class="eyebrow">Downside</p><h3>Worst Persisted Signals</h3></div></div>${signalTable(worst)}</section>
    `;
  }

  function signalTable(rows) {
    if (!rows.length) {
      return '<div class="empty-state-box">No signal outcomes in this scope.</div>';
    }
    return table(
      ["Signal", "Channel", "Symbol / side", "Status", "PnL", "Return", "Run"],
      rows.map((row) => [
        escapeHtml(row.signal_id),
        escapeHtml(row.channel),
        `${escapeHtml(row.symbol)}<small>${escapeHtml(row.side)}</small>`,
        escapeHtml(humanize(row.status)),
        `<span class="${pnlClass(row.total_pnl)}">${escapeHtml(money(row.total_pnl))}</span>`,
        `<span class="${pnlClass(row.return_pct)}">${escapeHtml(percent(row.return_pct))}</span>`,
        runReference(row, true),
      ]),
    );
  }

  function renderMethodology(methodology) {
    const runWeights = methodology.run_score_weights || {};
    const channelWeights = methodology.channel_score_weights || {};
    const sources = Array.isArray(methodology.sources) ? methodology.sources : [];
    nodes.methodology.innerHTML = `
      <div class="methodology-callout">
        <strong>${escapeHtml(methodology.version || "score methodology")}</strong>
        <p>${escapeHtml(methodology.purpose || "")}</p>
      </div>
      <div class="methodology-grid">
        <section><h3>Run score</h3>${definitionList(runWeights)}</section>
        <section><h3>Channel score</h3>${definitionList(channelWeights)}</section>
        <section><h3>Selection-risk control</h3><p>${escapeHtml(methodology.selection_penalty || "")}</p></section>
        <section><h3>Bootstrap uncertainty</h3><p>${escapeHtml(methodology.bootstrap || "")}</p></section>
      </div>
      <div class="analysis-caution"><strong>Metric boundary:</strong> ${escapeHtml(methodology.why_not_annualized_sharpe || "")}</div>
      <div class="methodology-sources"><h3>Research anchors</h3>${sources.map((source) => `
        <a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.label)}</a>
      `).join("")}</div>
    `;
  }

  function renderChartsWhenReady(data, attempt) {
    if (!window.echarts) {
      if (attempt < 30) {
        window.setTimeout(() => renderChartsWhenReady(data, attempt + 1), 100);
      }
      return;
    }
    renderRiskReturnChart(data.run_comparison || []);
    renderTimelineChart(data.time_series || []);
    renderDistributionChart(data.signal_return_distribution || {});
    renderBootstrapChart(data.bootstrap || []);
  }

  function chart(id) {
    const element = document.getElementById(id);
    if (!element || !window.echarts) {
      return null;
    }
    if (!state.charts.has(id)) {
      state.charts.set(id, window.echarts.init(element));
    }
    return state.charts.get(id);
  }

  function renderRiskReturnChart(rows) {
    const instance = chart("analysis-risk-return-chart");
    if (!instance) return;
    instance.setOption(baseChartOption({
      tooltip: {
        trigger: "item",
        formatter: ({ data }) => `${escapeHtml(data[4])}<br>${escapeHtml(shortId(data[5]))}<br>Return: ${percent(data[1])}<br>Drawdown: ${percent(data[0])}<br>Score: ${score(data[2])}<br>Signals: ${formatInteger(data[3])}`,
      },
      xAxis: { type: "value", name: "Max drawdown %", nameLocation: "middle", nameGap: 30 },
      yAxis: { type: "value", name: "Return %" },
      series: [{
        type: "scatter",
        data: rows.map((row) => [Number(row.max_drawdown_pct), Number(row.return_pct), Number(row.score), Number(row.signals), row.channel, row.run_id, row.strategy]),
        symbolSize: (value) => Math.max(9, Math.min(32, 8 + Math.sqrt(Math.max(value[3], 1)) * 2)),
        itemStyle: { color: ({ data }) => scoreColor(data[2]), opacity: 0.82 },
      }],
    }));
  }

  function renderTimelineChart(rows) {
    const instance = chart("analysis-timeline-chart");
    if (!instance) return;
    instance.setOption(baseChartOption({
      tooltip: { trigger: "axis" },
      legend: { data: ["Decision score", "Return %"], textStyle: { color: chartTextColor() } },
      xAxis: { type: "category", data: rows.map((row) => shortDate(row.created_at)), axisLabel: { rotate: 30 } },
      yAxis: [
        { type: "value", name: "Score", min: 0, max: 100 },
        { type: "value", name: "Return %" },
      ],
      series: [
        { name: "Decision score", type: "line", smooth: true, data: rows.map((row) => Number(row.score)), symbolSize: 7 },
        { name: "Return %", type: "bar", yAxisIndex: 1, data: rows.map((row) => Number(row.return_pct)), itemStyle: { color: "#38bdf8", opacity: 0.55 } },
      ],
    }));
  }

  function renderDistributionChart(distribution) {
    const instance = chart("analysis-distribution-chart");
    if (!instance) return;
    const buckets = Array.isArray(distribution.buckets) ? distribution.buckets : [];
    instance.setOption(baseChartOption({
      tooltip: { trigger: "axis", formatter: (items) => `${escapeHtml(items[0]?.axisValue || "")} %<br>Signals: ${formatInteger(items[0]?.value || 0)}` },
      xAxis: { type: "category", data: buckets.map((item) => item.label), axisLabel: { rotate: 40, interval: Math.max(Math.floor(buckets.length / 6) - 1, 0) } },
      yAxis: { type: "value", name: "Signals", minInterval: 1 },
      series: [{ type: "bar", data: buckets.map((item) => item.count), itemStyle: { color: "#8b5cf6" } }],
    }));
  }

  function renderBootstrapChart(rows) {
    const instance = chart("analysis-bootstrap-chart");
    if (!instance) return;
    const available = rows.filter((row) => row.available);
    instance.setOption(baseChartOption({
      tooltip: {
        trigger: "axis",
        formatter: (items) => {
          const index = items[0]?.dataIndex || 0;
          const row = available[index] || {};
          return `${escapeHtml(row.channel || "")}<br>5th–95th percentile: ${percent(row.p05_mean_return_pct)} to ${percent(row.p95_mean_return_pct)}<br>Observed mean: ${percent(row.observed_mean_return_pct)}<br>Positive resamples: ${percent(row.probability_positive_pct)}`;
        },
      },
      xAxis: { type: "category", data: available.map((row) => row.channel), axisLabel: { rotate: 25 } },
      yAxis: { type: "value", name: "Mean signal return %" },
      series: [
        {
          name: "5–95% interval",
          type: "candlestick",
          data: available.map((row) => [Number(row.p05_mean_return_pct), Number(row.p05_mean_return_pct), Number(row.p05_mean_return_pct), Number(row.p95_mean_return_pct)]),
          itemStyle: { color: "#10b981", color0: "#ef4444", borderColor: "#10b981", borderColor0: "#ef4444" },
        },
        {
          name: "Observed mean",
          type: "scatter",
          data: available.map((row) => Number(row.observed_mean_return_pct)),
          symbolSize: 11,
          itemStyle: { color: "#f59e0b" },
        },
      ],
    }));
  }

  function baseChartOption(option) {
    const textColor = chartTextColor();
    return {
      ...option,
      animationDuration: 350,
      backgroundColor: "transparent",
      grid: { left: 58, right: 28, top: 48, bottom: 68, containLabel: true },
      textStyle: { color: textColor, fontFamily: "Inter, system-ui, sans-serif" },
      xAxis: { axisLine: { lineStyle: { color: textColor } }, axisLabel: { color: textColor }, splitLine: { lineStyle: { color: chartGridColor() } }, ...(option.xAxis || {}) },
      yAxis: Array.isArray(option.yAxis)
        ? option.yAxis.map((axis) => ({ axisLine: { lineStyle: { color: textColor } }, axisLabel: { color: textColor }, splitLine: { lineStyle: { color: chartGridColor() } }, ...axis }))
        : { axisLine: { lineStyle: { color: textColor } }, axisLabel: { color: textColor }, splitLine: { lineStyle: { color: chartGridColor() } }, ...(option.yAxis || {}) },
    };
  }

  function resizeCharts() {
    state.charts.forEach((instance) => instance.resize());
  }

  function openTab(name) {
    document.querySelectorAll("[data-analysis-tab]").forEach((button) => {
      const active = button.dataset.analysisTab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-analysis-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.analysisPanel !== name;
    });
    window.setTimeout(resizeCharts, 0);
  }

  function table(headers, rows) {
    return `<div class="responsive-table"><table class="analysis-table"><thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.map((cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  function parameterDetails(parameters) {
    const entries = Object.entries(parameters);
    if (!entries.length) return "n/a";
    return `<details class="parameter-details"><summary>${entries.length} values</summary><dl>${entries.map(([key, value]) => `<dt>${escapeHtml(humanize(key))}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl></details>`;
  }

  function runReference(row, compact = false) {
    const label = escapeHtml(shortId(row.run_id));
    if (row.source === "report") {
      return `<span>${compact ? label : `<strong>${label}</strong>`}</span>${compact ? "" : "<small>Saved report</small>"}`;
    }
    const link = `/backtests?run_id=${encodeURIComponent(row.run_id)}`;
    return `<a href="${link}">${compact ? label : `<strong>${label}</strong>`}</a>${compact ? "" : "<small>Dashboard run</small>"}`;
  }

  function definitionList(values) {
    return `<dl class="methodology-weights">${Object.entries(values).map(([key, value]) => `<dt>${escapeHtml(humanize(key))}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>`;
  }

  function money(value) {
    return formatNumber(value, 4);
  }

  function percent(value) {
    return `${formatNumber(value, 2)}%`;
  }

  function signedPercent(value) {
    const number = Number(value || 0);
    return `${number > 0 ? "+" : ""}${percent(value)}`;
  }

  function score(value) {
    return formatNumber(value, 2);
  }

  function formatNumber(value, digits) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return String(value || "0");
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(number);
  }

  function formatInteger(value) {
    return new Intl.NumberFormat().format(Number(value || 0));
  }

  function formatDate(value) {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "n/a" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function shortDate(value) {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "n/a" : new Intl.DateTimeFormat(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function shortId(value) {
    const text = String(value || "");
    return text.length > 15 ? `${text.slice(0, 8)}…${text.slice(-5)}` : text;
  }

  function humanize(value) {
    return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function pnlClass(value) {
    const number = Number(value || 0);
    return number > 0 ? "pnl-positive" : number < 0 ? "pnl-negative" : "pnl-flat";
  }

  function scoreClass(value) {
    const number = Number(value || 0);
    return number >= 65 ? "score-strong" : number >= 50 ? "score-watch" : "score-risk";
  }

  function scoreColor(value) {
    const number = Number(value || 0);
    return number >= 65 ? "#10b981" : number >= 50 ? "#f59e0b" : "#ef4444";
  }

  function localInputToIso(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }

  function isoToLocalInput(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function chartTextColor() {
    return getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#94a3b8";
  }

  function chartGridColor() {
    return getComputedStyle(document.documentElement).getPropertyValue("--line-strong").trim() || "rgba(148,163,184,.18)";
  }

  function getAuthToken() {
    return new URLSearchParams(window.location.search).get("token") || "";
  }

  function withAuthPath(path) {
    const token = getAuthToken();
    if (!token) return path;
    const url = new URL(path, window.location.origin);
    url.searchParams.set("token", token);
    return `${url.pathname}${url.search}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
