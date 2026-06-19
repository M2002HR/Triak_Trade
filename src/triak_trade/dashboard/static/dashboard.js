document.documentElement.dataset.dashboardReady = "true";

(() => {
  const bootstrapNode = document.getElementById("backtest-bootstrap");
  if (!bootstrapNode) {
    return;
  }

  const bootstrap = JSON.parse(bootstrapNode.textContent || "{}");
  const initialRecentRuns = Array.isArray(bootstrap.recent_runs) ? bootstrap.recent_runs : [];
  const state = {
    bootstrap,
    savedChannels: Array.isArray(bootstrap.saved_channels) ? bootstrap.saved_channels : [],
    activeRunId: initialRecentRuns.length ? initialRecentRuns[0].run_id : null,
    activeRun: initialRecentRuns.length ? initialRecentRuns[0] : null,
    recentRuns: initialRecentRuns,
    selectedMessageId: null,
    selectedSignalId: null,
    modalOpen: false,
    panelModalOpen: false,
    activePanelModal: null,
    messageFilter: "all",
    pollTimer: null,
    listTimer: null,
    ws: null,
    wsReady: false,
    charts: new Map(),
  };

  const nodes = {
    form: document.getElementById("backtest-live-form"),
    channel: document.getElementById("backtest-channel"),
    savedChannelSelect: document.getElementById("backtest-saved-channel-select"),
    saveChannelInput: document.getElementById("backtest-save-channel-input"),
    saveChannelButton: document.getElementById("backtest-save-channel"),
    applySavedChannelButton: document.getElementById("backtest-apply-saved-channel"),
    removeChannelButton: document.getElementById("backtest-remove-channel"),
    savedChannelList: document.getElementById("backtest-saved-channel-list"),
    savedChannelStatus: document.getElementById("backtest-saved-channel-status"),
    fromDate: document.getElementById("backtest-from-date"),
    toDate: document.getElementById("backtest-to-date"),
    startMessageLink: document.getElementById("backtest-start-message-link"),
    interval: document.getElementById("backtest-interval"),
    maxMessages: document.getElementById("backtest-max-messages"),
    initialBalance: document.getElementById("backtest-initial-balance"),
    riskPerTradePct: document.getElementById("backtest-risk-per-trade-pct"),
    strategyKey: document.getElementById("backtest-strategy-key"),
    strategySummary: document.getElementById("backtest-strategy-summary"),
    strategyParameters: document.getElementById("backtest-strategy-parameters"),
    useAi: document.getElementById("backtest-use-ai"),
    sendLogChannel: document.getElementById("backtest-send-log-channel"),
    logPerMessage: document.getElementById("backtest-log-per-message"),
    startButton: document.getElementById("backtest-start-button"),
    formStatus: document.getElementById("backtest-form-status"),
    readinessHeadline: document.getElementById("readiness-headline"),
    readinessBadges: document.getElementById("readiness-badges"),
    readinessIssues: document.getElementById("readiness-issues"),
    activeRunHeadline: document.getElementById("active-run-headline"),
    runTitle: document.getElementById("run-title"),
    runSubtitle: document.getElementById("run-subtitle"),
    runPhasePill: document.getElementById("run-phase-pill"),
    runActionBar: document.getElementById("run-action-bar"),
    metrics: document.getElementById("backtest-metrics"),
    currentPhaseLabel: document.getElementById("current-phase-label"),
    currentPhaseSummary: document.getElementById("current-phase-summary"),
    currentMessageLabel: document.getElementById("current-message-label"),
    currentMessageSummary: document.getElementById("current-message-summary"),
    messageCountLabel: document.getElementById("message-count-label"),
    messageFilterBar: document.getElementById("message-filter-bar"),
    messageStream: document.getElementById("message-stream"),
    eventFeed: document.getElementById("event-feed"),
    recentRuns: document.getElementById("recent-runs"),
    signalStatePreview: document.getElementById("signal-state-preview"),
    modal: document.getElementById("message-modal"),
    modalTitle: document.getElementById("message-modal-title"),
    modalStatus: document.getElementById("message-modal-status"),
    modalMeta: document.getElementById("message-modal-meta"),
    modalStageGraph: document.getElementById("message-modal-stage-graph"),
    modalPreview: document.getElementById("message-modal-preview"),
    modalSummary: document.getElementById("message-modal-summary"),
    modalDebug: document.getElementById("message-modal-debug"),
    panelModal: document.getElementById("panel-modal"),
    panelModalTitle: document.getElementById("panel-modal-title"),
    panelModalBody: document.getElementById("panel-modal-body"),
  };

  seedDefaults();
  bindEvents();
  renderReadiness(bootstrap.readiness || {});
  renderSavedChannels();
  renderStrategies();
  renderRecentRuns(state.recentRuns);
  if (state.activeRun) {
    renderRun(state.activeRun);
  } else {
    renderEmptyRun();
  }
  connectWebSocket();
  refreshRunsList();

  function seedDefaults() {
    nodes.interval.value = bootstrap.default_interval || "1m";
    nodes.maxMessages.value = String(bootstrap.default_max_messages || 1000);
    nodes.initialBalance.value = String(bootstrap.default_initial_balance || "100");
    nodes.riskPerTradePct.value = String(bootstrap.default_risk_per_trade_pct || "3");
    if (nodes.strategyKey) {
      nodes.strategyKey.value = bootstrap.default_strategy_key || "default_risk_managed";
    }
    nodes.useAi.checked = Boolean(bootstrap.default_use_ai);
    nodes.sendLogChannel.checked = Boolean(bootstrap.default_send_log_channel);
    nodes.logPerMessage.checked = Boolean(bootstrap.default_log_per_message);
    applyDateRange(bootstrap.default_from_date, bootstrap.default_to_date);
  }

  function renderSavedChannels() {
    if (nodes.savedChannelSelect) {
      const currentValue = nodes.savedChannelSelect.value;
      nodes.savedChannelSelect.innerHTML = '<option value="">Choose a saved channel to load...</option>';
      state.savedChannels.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.channel_resolved;
        option.textContent = `${item.label} · ${item.channel_resolved}`;
        nodes.savedChannelSelect.appendChild(option);
      });
      if (state.savedChannels.some((item) => item.channel_resolved === currentValue)) {
        nodes.savedChannelSelect.value = currentValue;
      }
    }
    if (!nodes.savedChannelList) {
      return;
    }
    if (!state.savedChannels.length) {
      nodes.savedChannelList.innerHTML = '<p class="saved-channel-empty">No saved channels yet.</p>';
      return;
    }
    nodes.savedChannelList.innerHTML = state.savedChannels
      .map((item) => `
        <div class="saved-channel-chip">
          <strong>${escapeHtml(item.label || item.channel_resolved)}</strong>
          <small>${escapeHtml(item.channel_resolved)}</small>
        </div>
      `)
      .join("");
  }

  function setSavedChannelStatus(message, tone) {
    if (!nodes.savedChannelStatus) {
      return;
    }
    nodes.savedChannelStatus.textContent = message || "";
    nodes.savedChannelStatus.className = tone ? `inline-status ${tone}` : "inline-status";
  }

  function bindEvents() {
    if (nodes.form) {
      nodes.form.addEventListener("submit", handleSubmit);
    }
    if (nodes.saveChannelButton) {
      nodes.saveChannelButton.addEventListener("click", saveCurrentChannel);
    }
    if (nodes.applySavedChannelButton) {
      nodes.applySavedChannelButton.addEventListener("click", applySelectedSavedChannel);
    }
    if (nodes.removeChannelButton) {
      nodes.removeChannelButton.addEventListener("click", removeSelectedSavedChannel);
    }
    if (nodes.savedChannelSelect) {
      nodes.savedChannelSelect.addEventListener("change", () => setSavedChannelStatus("", ""));
    }
    if (nodes.strategyKey) {
      nodes.strategyKey.addEventListener("change", renderSelectedStrategy);
    }
    document.querySelectorAll("[data-preset-hours]").forEach((button) => {
      button.addEventListener("click", () => {
        const hours = Number(button.getAttribute("data-preset-hours") || "24");
        const end = new Date();
        const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
        applyDateRange(start.toISOString(), end.toISOString());
      });
    });
    if (nodes.messageFilterBar) {
      nodes.messageFilterBar.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-message-filter]") : null;
        if (!target) {
          return;
        }
        state.messageFilter = target.getAttribute("data-message-filter") || "all";
        renderFilterBar();
        renderMessages((state.activeRun && state.activeRun.messages) || []);
      });
    }
    if (nodes.messageStream) {
      nodes.messageStream.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-message-id]") : null;
        if (!target) {
          return;
        }
        const messageId = Number(target.getAttribute("data-message-id") || "0");
        state.selectedMessageId = messageId;
        const traces = state.activeRun && Array.isArray(state.activeRun.messages)
          ? state.activeRun.messages
          : [];
        const trace = traces.find((item) => item.message_id === messageId);
        if (trace) {
          openModal(trace);
        }
      });
    }
    document.addEventListener("click", (event) => {
      const panelTarget = event.target instanceof Element ? event.target.closest("[data-open-panel-modal]") : null;
      if (panelTarget) {
        const kind = panelTarget.getAttribute("data-open-panel-modal") || "feed";
        openPanelModal(kind);
        return;
      }
      const stopTarget = event.target instanceof Element ? event.target.closest("[data-stop-run-id]") : null;
      if (stopTarget) {
        event.preventDefault();
        event.stopPropagation();
        stopRun(stopTarget.getAttribute("data-stop-run-id") || "");
        return;
      }
      const rerunTarget = event.target instanceof Element ? event.target.closest("[data-rerun-run-id]") : null;
      if (rerunTarget) {
        event.preventDefault();
        event.stopPropagation();
        rerunRun(rerunTarget.getAttribute("data-rerun-run-id") || "");
        return;
      }
      const signalTarget = event.target instanceof Element ? event.target.closest("[data-signal-id]") : null;
      if (signalTarget) {
        event.preventDefault();
        event.stopPropagation();
        openSignalModal(signalTarget.getAttribute("data-signal-id") || "");
        return;
      }
      const target = event.target instanceof Element ? event.target.closest("[data-run-id]") : null;
      if (!target) {
        return;
      }
      state.activeRunId = target.getAttribute("data-run-id");
      closePanelModal();
      closeModal();
      fetchRun();
    });
    document.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-close-modal='true']") : null;
      if (target) {
        closeModal();
      }
    });
    document.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-close-panel-modal='true']") : null;
      if (target) {
        closePanelModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeModal();
        closePanelModal();
      }
    });
  }

  function renderStrategies() {
    if (!nodes.strategyKey) {
      return;
    }
    const strategies = Array.isArray(bootstrap.available_strategies) ? bootstrap.available_strategies : [];
    nodes.strategyKey.innerHTML = strategies
      .map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.name)}</option>`)
      .join("");
    nodes.strategyKey.value = bootstrap.default_strategy_key || (strategies[0] && strategies[0].key) || "";
    renderSelectedStrategy();
  }

  function renderSelectedStrategy() {
    const strategies = Array.isArray(bootstrap.available_strategies) ? bootstrap.available_strategies : [];
    const selectedKey = nodes.strategyKey ? nodes.strategyKey.value : "";
    const selected = strategies.find((item) => item.key === selectedKey) || strategies[0];
    if (!selected) {
      if (nodes.strategySummary) {
        nodes.strategySummary.textContent = "No strategy selected.";
      }
      if (nodes.strategyParameters) {
        nodes.strategyParameters.innerHTML = "";
      }
      return;
    }
    if (nodes.strategySummary) {
      nodes.strategySummary.innerHTML = `
        <strong>${escapeHtml(selected.name || selected.key)}</strong>
        <p>${escapeHtml(selected.description || "")}</p>
        <small>Class: ${escapeHtml(selected.class_name || "")}</small>
      `;
    }
    if (nodes.strategyParameters) {
      const parameters = selected.parameters || {};
      nodes.strategyParameters.innerHTML = Object.entries(parameters)
        .map(([key, value]) => `
          <div class="strategy-parameter-card">
            <span>${escapeHtml(formatStrategyKey(key))}</span>
            <strong>${escapeHtml(Array.isArray(value) ? value.join(", ") : String(value))}</strong>
          </div>
        `)
        .join("");
    }
  }

  async function saveCurrentChannel() {
    const saveFieldChannel = nodes.saveChannelInput ? nodes.saveChannelInput.value.trim() : "";
    const formChannel = nodes.channel ? nodes.channel.value.trim() : "";
    const channel = saveFieldChannel || formChannel;
    if (!channel) {
      setSavedChannelStatus("Enter a Telegram channel in the save field first.", "error");
      return;
    }
    setSavedChannelStatus("Saving channel...", "working");
    try {
      const response = await fetch(withAuthPath("/api/backtests/channels"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel }),
      });
      const data = await response.json();
      if (!response.ok) {
        setSavedChannelStatus(data.detail || "Channel save failed.", "error");
        return;
      }
      state.savedChannels = Array.isArray(data.channels) ? data.channels : [];
      renderSavedChannels();
      const savedChannel = state.savedChannels.find((item) => item.channel_input === channel)
        || state.savedChannels.find((item) => item.channel_resolved === channel)
        || state.savedChannels[0];
      if (nodes.savedChannelSelect && savedChannel) {
        nodes.savedChannelSelect.value = savedChannel.channel_resolved;
      }
      if (nodes.saveChannelInput && savedChannel) {
        nodes.saveChannelInput.value = savedChannel.channel_resolved;
      }
      if (nodes.channel && savedChannel) {
        nodes.channel.value = savedChannel.channel_resolved;
      }
      setSavedChannelStatus("Channel saved. You can now load it into the form anytime.", "success");
    } catch (error) {
      setSavedChannelStatus(
        `Channel save failed: ${error instanceof Error ? error.message : "unknown error"}`,
        "error",
      );
    }
  }

  function applySelectedSavedChannel() {
    const selected = nodes.savedChannelSelect ? nodes.savedChannelSelect.value : "";
    if (!selected) {
      setSavedChannelStatus("Choose a saved channel to load first.", "error");
      return;
    }
    nodes.channel.value = selected;
    if (nodes.saveChannelInput) {
      nodes.saveChannelInput.value = selected;
    }
    nodes.channel.focus();
    setSavedChannelStatus("Saved channel loaded into the backtest form.", "success");
  }

  async function removeSelectedSavedChannel() {
    const selected = nodes.savedChannelSelect ? nodes.savedChannelSelect.value : "";
    if (!selected) {
      setSavedChannelStatus("Choose a saved channel to remove first.", "error");
      return;
    }
    setSavedChannelStatus("Removing channel...", "working");
    try {
      const response = await fetch(withAuthPath("/api/backtests/channels"), {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: selected }),
      });
      const data = await response.json();
      if (!response.ok) {
        setSavedChannelStatus(data.detail || "Channel removal failed.", "error");
        return;
      }
      state.savedChannels = Array.isArray(data.channels) ? data.channels : [];
      renderSavedChannels();
      if (nodes.savedChannelSelect) {
        nodes.savedChannelSelect.value = "";
      }
      if (nodes.saveChannelInput && nodes.saveChannelInput.value.trim() === selected) {
        nodes.saveChannelInput.value = "";
      }
      setSavedChannelStatus("Saved channel removed.", "success");
    } catch (error) {
      setSavedChannelStatus(
        `Channel removal failed: ${error instanceof Error ? error.message : "unknown error"}`,
        "error",
      );
    }
  }

  function renderFilterBar() {
    if (!nodes.messageFilterBar) {
      return;
    }
    nodes.messageFilterBar.querySelectorAll("[data-message-filter]").forEach((button) => {
      const active = button.getAttribute("data-message-filter") === state.messageFilter;
      button.classList.toggle("active", active);
    });
  }

  function openPanelModal(kind) {
    state.panelModalOpen = true;
    state.activePanelModal = kind;
    nodes.panelModal.hidden = false;
    if (kind === "history") {
      nodes.panelModalTitle.textContent = "Recent Backtests";
      nodes.panelModalBody.innerHTML = buildRecentRunsMarkup(state.recentRuns, true);
    } else if (kind === "signals") {
      nodes.panelModalTitle.textContent = "Active & Inactive Signals";
      nodes.panelModalBody.innerHTML = buildSignalsMarkup(
        (state.activeRun && state.activeRun.signals) || [],
        true,
      );
    } else {
      nodes.panelModalTitle.textContent = "Run Feed";
      nodes.panelModalBody.innerHTML = buildEventFeedMarkup(
        (state.activeRun && state.activeRun.events) || [],
        true,
      );
    }
    syncBodyModalState();
  }

  function closePanelModal() {
    if (!nodes.panelModal) {
      return;
    }
    state.panelModalOpen = false;
    state.activePanelModal = null;
    state.selectedSignalId = null;
    nodes.panelModal.hidden = true;
    syncBodyModalState();
  }

  function syncBodyModalState() {
    if (state.modalOpen || state.panelModalOpen) {
      document.body.classList.add("modal-open");
    } else {
      document.body.classList.remove("modal-open");
    }
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const payload = buildPayload();
    if (!payload) {
      return;
    }
    setFormStatus("Starting real backtest worker...", "working");
    nodes.startButton.disabled = true;
    try {
      const response = await fetch(withAuthPath("/api/backtests/start"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        const detail = data.detail || data.reason || "Backtest start failed.";
        setFormStatus(detail, "error");
        if (data.issues) {
          renderReadiness({ ...(bootstrap.readiness || {}), issues: data.issues, ready: false });
        }
        return;
      }
      if (data.blocked) {
        setFormStatus(data.reason || "Backtest is blocked.", "error");
        renderReadiness(data.readiness || { ready: false, issues: data.issues || [] });
        return;
      }
      state.activeRunId = data.run.run_id;
      state.activeRun = data.run;
      upsertRun(data.run);
      renderRun(data.run);
      setFormStatus("Backtest started. Streaming live progress now.", "success");
      if (!state.wsReady) {
        startPolling();
      }
    } catch (error) {
      setFormStatus(`Backtest start failed: ${error instanceof Error ? error.message : "unknown error"}`, "error");
    } finally {
      nodes.startButton.disabled = false;
    }
  }

  function buildPayload() {
    const fromValue = nodes.fromDate.value;
    const toValue = nodes.toDate.value;
    if (!fromValue || !toValue) {
      setFormStatus("Start and end dates are required.", "error");
      return null;
    }
    const fromDate = new Date(fromValue);
    const toDate = new Date(toValue);
    if (Number.isNaN(fromDate.getTime()) || Number.isNaN(toDate.getTime())) {
      setFormStatus("Date range is invalid.", "error");
      return null;
    }
    if (toDate <= fromDate) {
      setFormStatus("End date must be after start date.", "error");
      return null;
    }
    return {
      channel: nodes.channel.value.trim(),
      from_date: fromDate.toISOString(),
      to_date: toDate.toISOString(),
      start_message_link: nodes.startMessageLink.value.trim(),
      interval: nodes.interval.value,
      max_messages: Number(nodes.maxMessages.value || "1000"),
      initial_balance: nodes.initialBalance.value.trim(),
      risk_per_trade_pct: nodes.riskPerTradePct.value.trim(),
      strategy_key: (nodes.strategyKey ? nodes.strategyKey.value : "")
        || bootstrap.default_strategy_key
        || "default_risk_managed",
      use_ai: nodes.useAi.checked,
      send_log_channel: nodes.sendLogChannel.checked,
      log_per_message: nodes.logPerMessage.checked,
    };
  }

  function startPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
    }
    state.pollTimer = window.setInterval(fetchRun, 2000);
    fetchRun();
  }

  async function fetchRun() {
    if (!state.activeRunId) {
      return;
    }
    try {
      const response = await fetch(
        withAuthPath(`/api/backtests/runs/${encodeURIComponent(state.activeRunId)}`),
      );
      if (!response.ok) {
        return;
      }
      const run = await response.json();
      state.activeRun = run;
      upsertRun(run);
      renderRun(run);
      if (!isActiveStatus(run.status) && state.pollTimer) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    } catch (_error) {
      setFormStatus("Live run refresh failed. Retrying...", "warning");
    }
  }

  async function refreshRunsList() {
    if (state.listTimer) {
      window.clearTimeout(state.listTimer);
    }
    try {
      const response = await fetch(withAuthPath("/api/backtests/runs?limit=8"));
      if (response.ok) {
      const data = await response.json();
        state.recentRuns = data.runs || [];
        renderRecentRuns(state.recentRuns);
      }
    } catch (_error) {
      // Keep silent on list refresh; main run polling is more important.
    }
    state.listTimer = window.setTimeout(refreshRunsList, 10000);
  }

  function renderReadiness(readiness) {
    const ready = Boolean(readiness.ready);
    nodes.readinessHeadline.textContent = ready ? "Ready" : "Blocked";
    nodes.readinessBadges.innerHTML = "";
    const checks = [
      ["Real Backtest", readiness.real_backtest_enabled],
      ["Telegram Creds", readiness.telegram_credentials_present],
      ["Telegram Session", readiness.telegram_session_configured],
      ["Market Data", readiness.toobit_public_market_ready],
      ["AI Gateway", readiness.ai_gateway_enabled],
      ["Regex Fallback", readiness.regex_fallback_enabled],
      ["Log Channel", readiness.log_channel_enabled],
    ];
    checks.forEach(([label, value]) => {
      const badge = document.createElement("span");
      badge.className = `status-badge ${value ? "ok" : "warn"}`;
      badge.textContent = `${label}: ${value ? "on" : "off"}`;
      nodes.readinessBadges.appendChild(badge);
    });
    const issues = Array.isArray(readiness.issues) ? readiness.issues : [];
    if (!issues.length) {
      nodes.readinessIssues.innerHTML = '<div class="issue ok">All real backtest guards are satisfied.</div>';
      return;
    }
    nodes.readinessIssues.innerHTML = issues
      .map((issue) => `<div class="issue warn">${escapeHtml(issue)}</div>`)
      .join("");
  }

  function renderRun(run) {
    renderCurrentRunHeader(run);
    renderMetrics(run);
    renderEventFeed(run.events || []);
    renderMessages(run.messages || []);
    renderSignals(run.signals || []);
    renderRecentRuns(state.recentRuns);
    if (state.modalOpen && state.selectedMessageId) {
      const runMessages = Array.isArray(run.messages) ? run.messages : [];
      const trace = runMessages.find((item) => item.message_id === state.selectedMessageId);
      if (trace) {
        openModal(trace);
      }
    }
    if (state.panelModalOpen && state.activePanelModal === "signal-detail" && state.selectedSignalId) {
      const runSignals = Array.isArray(run.signals) ? run.signals : [];
      const signal = runSignals.find((item) => item.signal_id === state.selectedSignalId);
      if (signal) {
        nodes.panelModalTitle.textContent = `${signal.symbol || "Signal"} Lifecycle`;
        nodes.panelModalBody.innerHTML = buildSignalDetailMarkup(signal);
      }
    }
  }

  function renderCurrentRunHeader(run) {
    nodes.activeRunHeadline.textContent = isActiveStatus(run.status) ? "Streaming" : run.current_phase_label;
    nodes.runTitle.textContent = `${run.channel_resolved} • ${run.interval}`;
    const startMessageSuffix = run.start_message_id
      ? ` • from message ${run.start_message_id}`
      : "";
    const strategySuffix = run.strategy_key ? ` • strategy ${run.strategy_key}` : "";
    nodes.runSubtitle.textContent = `${formatDate(run.from_date)} → ${formatDate(run.to_date)}${startMessageSuffix}${strategySuffix}`;
    nodes.runPhasePill.textContent = run.current_phase_label;
    nodes.runPhasePill.className = `phase-pill phase-${run.status}`;
    renderRunActions(run);
    nodes.currentPhaseLabel.textContent = run.current_phase_label;
    nodes.currentPhaseSummary.textContent = run.current_phase_summary || "No summary yet.";
    const runMessages = Array.isArray(run.messages) ? run.messages : [];
    const currentTrace = runMessages.find((item) => item.message_id === run.current_message_id);
    nodes.currentMessageLabel.textContent = currentTrace ? `Message ${currentTrace.message_id}` : "None";
    nodes.currentMessageSummary.textContent = currentTrace
      ? `${currentTrace.current_stage} • ${currentTrace.result_summary || currentTrace.preview_text || "Processing"}`
      : "No message is being processed right now.";
  }

  function renderMetrics(run) {
    const cards = [
      ["Messages", run.total_messages],
      ["Classified", run.classified_messages],
      ["Parsed Signals", run.parsed_signals],
      ["Valid Signals", run.valid_signals],
      ["Ignored", run.ignored_messages],
      ["Ambiguous", run.ambiguous_messages],
      ["Trades Simulated", run.trades_simulated],
      ["Trades Filled", run.trades_filled],
      ["Initial Balance", run.initial_balance],
      ["Risk / Signal %", run.risk_per_trade_pct],
      ["Open Positions", run.live_open_positions],
      ["Closed Trades", run.live_closed_trades],
      ["Wins / Losses", `${run.live_wins} / ${run.live_losses}`],
      ["Live PnL", run.live_total_pnl],
      ["Realized PnL", run.live_realized_pnl],
      ["Unrealized PnL", run.live_unrealized_pnl],
      ["Realized Balance", run.live_realized_balance],
      ["Live Balance", run.live_current_balance],
    ];
    nodes.metrics.innerHTML = cards
      .map(([label, value]) => `
        <div class="metric-card">
          <span>${escapeHtml(String(label))}</span>
          <strong>${escapeHtml(String(value ?? 0))}</strong>
        </div>
      `)
      .join("");
  }

  function renderEventFeed(events) {
    if (!events.length) {
      nodes.eventFeed.textContent = "No activity yet.";
      nodes.eventFeed.classList.add("empty-state-box");
      return;
    }
    nodes.eventFeed.classList.remove("empty-state-box");
    const latest = events[events.length - 1];
    nodes.eventFeed.innerHTML = `
      <div class="preview-stack">
        <strong>${escapeHtml(replaceUnderscores(latest.phase))}</strong>
        <span>${escapeHtml(latest.summary)}</span>
        <small>${events.length} updates captured</small>
      </div>
    `;
    if (state.panelModalOpen && state.activePanelModal === "feed") {
      nodes.panelModalBody.innerHTML = buildEventFeedMarkup(events, true);
    }
  }

  function renderMessages(messages) {
    const filteredMessages = messages.filter(matchesMessageFilter);
    nodes.messageCountLabel.textContent = `${filteredMessages.length} of ${messages.length} messages`;
    if (!filteredMessages.length) {
      nodes.messageStream.textContent = messages.length
        ? "No messages match the current filter."
        : "No messages have been processed yet.";
      nodes.messageStream.classList.add("empty-state-box");
      return;
    }
    nodes.messageStream.classList.remove("empty-state-box");
    nodes.messageStream.innerHTML = messages
      .filter(matchesMessageFilter)
      .map((trace) => {
        const active = Boolean(state.activeRun && state.activeRun.current_message_id === trace.message_id);
        return `
          <button type="button" class="message-card ${active ? "active" : ""}" data-message-id="${escapeHtml(String(trace.message_id))}">
            <div class="message-card-top">
              <div>
                <strong>Message ${escapeHtml(String(trace.message_id))}</strong>
                <span>${escapeHtml(trace.channel_username || trace.channel_id)}</span>
              </div>
              <div class="message-badges">
                <span class="mini-badge state-${escapeHtml(trace.final_status)}">${escapeHtml(trace.final_status)}</span>
                <span class="mini-badge stage-${escapeHtml(trace.current_stage)}">${escapeHtml(trace.current_stage)}</span>
              </div>
            </div>
            <p class="message-card-preview">${escapeHtml(trace.preview_text || "(empty text message)")}</p>
            <div class="message-card-meta">
              <span>${escapeHtml(trace.classification || "unknown")}</span>
              <span>${escapeHtml(trace.parsed_action || "unknown")}</span>
              <span>${escapeHtml(trace.symbol || "no symbol")}</span>
              <span>${formatDate(trace.message_date)}</span>
            </div>
          </button>
        `;
      })
      .join("");
  }

  function renderRecentRuns(runs) {
    if (!runs.length) {
      nodes.recentRuns.textContent = "No previous runs found.";
      nodes.recentRuns.classList.add("empty-state-box");
      return;
    }
    nodes.recentRuns.classList.remove("empty-state-box");
    const latest = runs[0];
    nodes.recentRuns.innerHTML = `
      <div class="preview-stack">
        <strong>${escapeHtml(latest.channel_input || latest.channel_resolved)}</strong>
        <span>${escapeHtml(latest.current_phase_label || latest.current_phase || latest.status)}</span>
        <small>${runs.length} runs available</small>
      </div>
    `;
    if (state.panelModalOpen && state.activePanelModal === "history") {
      nodes.panelModalBody.innerHTML = buildRecentRunsMarkup(runs, true);
    }
  }

  function renderEmptyRun() {
    nodes.activeRunHeadline.textContent = "No active run";
    nodes.runTitle.textContent = "Waiting For A Backtest";
    nodes.runSubtitle.textContent = "Start a run to stream message-by-message progress.";
    nodes.runPhasePill.textContent = "Queued";
    nodes.runPhasePill.className = "phase-pill phase-queued";
    nodes.runActionBar.innerHTML = "";
    nodes.metrics.innerHTML = "";
    nodes.currentPhaseLabel.textContent = "Queued";
    nodes.currentPhaseSummary.textContent = "Waiting to start.";
    nodes.currentMessageLabel.textContent = "None";
    nodes.currentMessageSummary.textContent = "No message is being processed yet.";
    renderSignals([]);
  }

  function openModal(trace) {
    state.modalOpen = true;
    nodes.modal.hidden = false;
    nodes.modalTitle.textContent = `Message ${trace.message_id} Timeline`;
    nodes.modalStatus.textContent = trace.final_status;
    nodes.modalStatus.className = `phase-pill phase-${trace.final_status}`;
    nodes.modalMeta.innerHTML = `
      <div class="meta-chip"><strong>Classification</strong><span>${escapeHtml(trace.classification || "unknown")}</span></div>
      <div class="meta-chip"><strong>Action</strong><span>${escapeHtml(trace.parsed_action || "unknown")}</span></div>
      <div class="meta-chip"><strong>Symbol</strong><span>${escapeHtml(trace.symbol || "none")}</span></div>
      <div class="meta-chip"><strong>Confidence</strong><span>${escapeHtml(trace.confidence || "n/a")}</span></div>
      <div class="meta-chip"><strong>Time</strong><span>${formatDate(trace.message_date)}</span></div>
      <div class="meta-chip"><strong>Link</strong><span>${trace.message_link ? `<a href="${escapeHtml(trace.message_link)}" target="_blank" rel="noreferrer">Open</a>` : "not available"}</span></div>
    `;
    nodes.modalStageGraph.innerHTML = (trace.stages || [])
      .map((stage) => {
        const current = trace.current_stage === stage.key;
        return `
          <article class="stage-node stage-${escapeHtml(stage.status)} ${current ? "current" : ""}">
            <div class="stage-node-head">
              <span class="stage-dot"></span>
              <strong>${escapeHtml(stage.label)}</strong>
              <small>${escapeHtml(stage.status)}</small>
            </div>
            <p>${escapeHtml(stage.detail || "No detail yet.")}</p>
            <div class="stage-node-foot">
              <span>${escapeHtml(formatDuration(stage.duration_ms))}</span>
              <span>${stage.started_at ? escapeHtml(formatDate(stage.started_at)) : "pending"}</span>
            </div>
          </article>
        `;
      })
      .join("");
    nodes.modalPreview.textContent = trace.full_text || trace.preview_text || "(empty text message)";
    nodes.modalSummary.innerHTML = `
      <div class="summary-row"><strong>Final Status</strong><span>${escapeHtml(trace.final_status)}</span></div>
      <div class="summary-row"><strong>Current Stage</strong><span>${escapeHtml(trace.current_stage)}</span></div>
      <div class="summary-row"><strong>Processing Duration</strong><span>${escapeHtml(formatDuration(trace.processing_duration_ms))}</span></div>
      <div class="summary-row"><strong>Result</strong><span>${escapeHtml(trace.result_summary || "No final summary yet.")}</span></div>
      ${trace.signal_id ? `<div class="summary-row"><strong>Signal ID</strong><span>${escapeHtml(trace.signal_id)}</span></div>` : ""}
    `;
    nodes.modalDebug.innerHTML = (trace.debug_notes || []).length
      ? trace.debug_notes.map((note) => `<div class="debug-note">${escapeHtml(note)}</div>`).join("")
      : '<div class="debug-note empty">No debug notes.</div>';
    syncBodyModalState();
  }

  function closeModal() {
    if (!nodes.modal) {
      return;
    }
    state.modalOpen = false;
    state.selectedMessageId = null;
    nodes.modal.hidden = true;
    syncBodyModalState();
  }

  function renderSignals(signals) {
    const activeCount = signals.filter((signal) => signal.status_group === "active").length;
    const inactiveCount = signals.length - activeCount;
    if (!signals.length) {
      nodes.signalStatePreview.textContent = "No simulated signal state yet.";
      nodes.signalStatePreview.classList.add("empty-state-box");
      if (state.panelModalOpen && state.activePanelModal === "signals") {
        nodes.panelModalBody.innerHTML = buildSignalsMarkup(signals, true);
      }
      return;
    }
    nodes.signalStatePreview.classList.remove("empty-state-box");
    nodes.signalStatePreview.innerHTML = `
      <div class="preview-stack signal-preview-stack">
        <div class="signal-count-row">
          <span class="signal-state-chip active">${activeCount} active</span>
          <span class="signal-state-chip inactive">${inactiveCount} inactive</span>
        </div>
        ${buildSignalsMarkup(signals.slice(0, 3), false)}
      </div>
    `;
    if (state.panelModalOpen && state.activePanelModal === "signals") {
      nodes.panelModalBody.innerHTML = buildSignalsMarkup(signals, true);
    }
  }

  function buildSignalsMarkup(signals, expanded) {
    if (!signals.length) {
      return '<div class="empty-state-box">No simulated signal state yet.</div>';
    }
    return `
      <div class="signal-list ${expanded ? "panel-modal-list" : ""}">
        ${signals
          .map((signal) => {
            const active = signal.status_group === "active";
            const pnlClass = pnlClassName(signal.total_pnl);
            const entryTime = signal.entry_time_tehran || signal.entry_time;
            const tpCount = Array.isArray(signal.take_profits) ? signal.take_profits.length : 0;
            return `
              <button type="button" class="signal-card ${active ? "active" : "inactive"}" data-signal-id="${escapeHtml(signal.signal_id)}">
                <div class="signal-card-top">
                  <div>
                    <strong>${escapeHtml(signal.symbol || "unknown")}</strong>
                    <span>${escapeHtml(signal.side || "unknown")} • ${escapeHtml(signal.status || "unknown")}</span>
                  </div>
                  <span class="signal-state-chip ${active ? "active" : "inactive"}">${active ? "active" : "inactive"}</span>
                </div>
                <div class="signal-config-line">
                  <span>Entry ${escapeHtml(signal.entry_price || "n/a")}</span>
                  <span>Mark ${escapeHtml(signal.mark_price || "n/a")}</span>
                  <span>SL ${escapeHtml(signal.stop_loss || "n/a")}</span>
                  <span>${tpCount} TP</span>
                </div>
                <div class="signal-card-bottom">
                  <span>${formatTehranDate(entryTime)}</span>
                  <strong class="${pnlClass}">PnL ${escapeHtml(String(signal.total_pnl ?? "0"))}</strong>
                </div>
              </button>
            `;
          })
          .join("")}
      </div>
    `;
  }

  function openSignalModal(signalId) {
    const signals = state.activeRun && Array.isArray(state.activeRun.signals)
      ? state.activeRun.signals
      : [];
    const signal = signals.find((item) => item.signal_id === signalId);
    if (!signal) {
      return;
    }
    state.panelModalOpen = true;
    state.activePanelModal = "signal-detail";
    state.selectedSignalId = signalId;
    nodes.panelModal.hidden = false;
    nodes.panelModalTitle.textContent = `${signal.symbol || "Signal"} Lifecycle`;
    nodes.panelModalBody.innerHTML = buildSignalDetailMarkup(signal);
    renderSignalLifecycleChart(signal);
    syncBodyModalState();
  }

  function buildSignalDetailMarkup(signal) {
    const takeProfits = Array.isArray(signal.take_profits) ? signal.take_profits : [];
    return `
      <div class="signal-detail-shell">
        <div class="signal-detail-hero ${signal.status_group === "active" ? "active" : "inactive"}">
          <div>
            <p class="eyebrow">Signal State</p>
            <h3>${escapeHtml(signal.symbol || "unknown")} ${escapeHtml(signal.side || "")}</h3>
            <p>${escapeHtml(signal.signal_id || "unknown")}</p>
          </div>
          <span class="signal-state-chip ${signal.status_group === "active" ? "active" : "inactive"}">
            ${escapeHtml(signal.status || "unknown")}
          </span>
        </div>
        <div class="signal-detail-grid">
          ${detailMetric("Entry Time", formatTehranDate(signal.entry_time_tehran || signal.entry_time))}
          ${detailMetric("Exit Time", signal.exit_time_tehran || signal.exit_time ? formatTehranDate(signal.exit_time_tehran || signal.exit_time) : "open")}
          ${detailMetric("Entry Price", signal.entry_price || "n/a")}
          ${detailMetric("Mark Price", signal.mark_price || "n/a")}
          ${detailMetric("Stop Loss", signal.stop_loss || "n/a")}
          ${detailMetric("Original Quantity", signal.original_quantity || "0")}
          ${detailMetric("Open Quantity", signal.open_quantity || "0")}
          ${detailMetric("Risk Amount", signal.risk_amount || "0")}
          ${detailMetric("Notional Value", signal.notional_value || "0")}
          ${detailMetric("Targets Hit", signal.targets_hit ?? "0")}
          ${detailMetric("Total PnL", signal.total_pnl ?? "0", pnlClassName(signal.total_pnl))}
          ${detailMetric("Total PnL %", signal.total_pnl_pct ?? "0", pnlClassName(signal.total_pnl_pct))}
          ${detailMetric("Realized PnL", signal.realized_pnl ?? "0", pnlClassName(signal.realized_pnl))}
          ${detailMetric("Unrealized PnL", signal.unrealized_pnl ?? "0", pnlClassName(signal.unrealized_pnl))}
        </div>
        <section class="signal-detail-section">
          <h3>Price Lifecycle Chart</h3>
          <div class="signal-chart-meta">
            <span>Time: Tehran</span>
            <span>Candles: 5m</span>
            <span>Last refresh: ${formatTehranDate(signal.last_checkpoint_at_tehran || signal.last_checkpoint_at)}</span>
          </div>
          <div id="signal-lifecycle-chart" class="signal-lifecycle-chart"></div>
        </section>
        <section class="signal-detail-section">
          <h3>Take Profits</h3>
          <div class="target-pill-row">
            ${
              takeProfits.length
                ? takeProfits.map((target, index) => `<span class="target-pill">TP${index + 1}: ${escapeHtml(target)}</span>`).join("")
                : '<span class="target-pill muted">No configured targets.</span>'
            }
          </div>
        </section>
        <section class="signal-detail-section">
          <h3>Lifecycle</h3>
          ${buildLifecycleMarkup(signal)}
        </section>
      </div>
    `;
  }

  function detailMetric(label, value, extraClass = "") {
    return `
      <div class="signal-detail-metric ${extraClass}">
        <strong>${escapeHtml(label)}</strong>
        <span>${escapeHtml(String(value))}</span>
      </div>
    `;
  }

  function formatDuration(durationMs) {
    if (durationMs === null || durationMs === undefined) {
      return "n/a";
    }
    const value = Number(durationMs);
    if (!Number.isFinite(value)) {
      return "n/a";
    }
    if (value < 1000) {
      return `${value} ms`;
    }
    return `${(value / 1000).toFixed(2)} s`;
  }

  function buildLifecycleMarkup(signal) {
    const lifecycle = Array.isArray(signal.lifecycle) ? signal.lifecycle : [];
    const started = [{
      label: "Signal created",
      detail: `created_at=${signal.entry_time_tehran || signal.entry_time}`,
      timestamp_tehran: signal.entry_time_tehran || signal.entry_time,
    }];
    const items = [...started, ...lifecycle];
    if (!items.length) {
      return '<div class="empty-state-box">No lifecycle events yet.</div>';
    }
    return `
      <div class="signal-lifecycle">
        ${items
          .map((item, index) => `
            <article class="lifecycle-item ${index === items.length - 1 ? "current" : ""}">
              <span>${index + 1}</span>
              <div>
                <strong>${escapeHtml(String(item.label || "Lifecycle update"))}</strong>
                <p>${escapeHtml(String(item.detail || item))}</p>
                <small>${escapeHtml(formatTehranDate(item.timestamp_tehran || item.timestamp || signal.entry_time_tehran || signal.entry_time))}</small>
              </div>
            </article>
          `)
          .join("")}
      </div>
    `;
  }

  function ensureChart(id) {
    const el = document.getElementById(id);
    if (!el || typeof echarts === "undefined") {
      return null;
    }
    const existing = state.charts.get(id);
    if (existing) {
      return existing;
    }
    const chart = echarts.init(el);
    state.charts.set(id, chart);
    return chart;
  }

  function renderSignalLifecycleChart(signal) {
    const chart = ensureChart("signal-lifecycle-chart");
    if (!chart) {
      return;
    }
    const chartData = signal.chart || {};
    const candles = Array.isArray(chartData.candles) ? chartData.candles : [];
    const stopLossHistory = Array.isArray(chartData.stop_loss_history) ? chartData.stop_loss_history : [];
    const takeProfitHistory = Array.isArray(chartData.take_profit_history) ? chartData.take_profit_history : [];
    if (!candles.length) {
      chart.clear();
      return;
    }
    const xAxis = candles.map((item) => formatTehranDate(item.timestamp_tehran));
    const series = [
      {
        name: "Price",
        type: "candlestick",
        data: candles.map((item) => [
          Number(item.open),
          Number(item.close),
          Number(item.low),
          Number(item.high),
        ]),
        itemStyle: {
          color: "#0e7c66",
          color0: "#d14343",
          borderColor: "#0e7c66",
          borderColor0: "#d14343",
        },
      },
      ...buildLevelHistorySeries(stopLossHistory, xAxis, "#d14343", "dashed"),
      ...buildLevelHistorySeries(takeProfitHistory, xAxis, "#b7791f", "solid"),
    ];
    chart.setOption(
      {
        animation: false,
        grid: { left: 56, right: 24, top: 30, bottom: 64 },
        legend: { top: 0 },
        tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
        xAxis: {
          type: "category",
          data: xAxis,
          axisLabel: { rotate: 25, color: "#39524b" },
        },
        yAxis: {
          scale: true,
          axisLabel: { color: "#39524b" },
        },
        dataZoom: [{ type: "inside" }, { type: "slider", height: 24, bottom: 12 }],
        series,
      },
      { notMerge: true, lazyUpdate: true },
    );
  }

  function buildLevelHistorySeries(history, xAxis, color, styleType) {
    return history.map((item) => {
      const start = formatTehranDate(item.started_at_tehran || item.started_at);
      const end = item.ended_at_tehran || item.ended_at
        ? formatTehranDate(item.ended_at_tehran || item.ended_at)
        : xAxis[xAxis.length - 1];
      return {
        name: `${item.label} ${item.value}`,
        type: "line",
        symbol: "none",
        lineStyle: {
          color,
          type: styleType,
          width: item.ended_at ? 2 : 3,
        },
        data: xAxis.map((label) => (label >= start && label <= end ? Number(item.value) : null)),
      };
    });
  }

  function upsertRun(run) {
    const runs = Array.isArray(state.recentRuns) ? [...state.recentRuns] : [];
    const index = runs.findIndex((item) => item.run_id === run.run_id);
    if (index >= 0) {
      runs[index] = run;
    } else {
      runs.unshift(run);
    }
    state.recentRuns = runs
      .sort((left, right) => new Date(right.created_at) - new Date(left.created_at))
      .slice(0, 8);
  }

  function renderRunActions(run) {
    if (!nodes.runActionBar) {
      return;
    }
    const stopButton = isActiveStatus(run.status)
      ? `<button type="button" class="danger compact-action" data-stop-run-id="${escapeHtml(run.run_id)}">Stop Run</button>`
      : "";
    nodes.runActionBar.innerHTML = `
      <button type="button" class="ghost-button compact-action" data-rerun-run-id="${escapeHtml(run.run_id)}">Run Again</button>
      ${stopButton}
    `;
  }

  async function stopRun(runId) {
    if (!runId) {
      return;
    }
    setFormStatus("Requesting backtest stop...", "working");
    try {
      const response = await fetch(withAuthPath(`/api/backtests/runs/${encodeURIComponent(runId)}/stop`), {
        method: "POST",
      });
      const data = await response.json();
      if (data.run) {
        upsertRun(data.run);
        if (state.activeRunId === data.run.run_id) {
          state.activeRun = data.run;
          renderRun(data.run);
        }
      }
      if (!response.ok) {
        setFormStatus(`Stop rejected: ${data.reason || data.detail || "run is not stoppable"}`, "warning");
        return;
      }
      state.activeRunId = data.run.run_id;
      state.activeRun = data.run;
      renderRun(data.run);
      setFormStatus("Stop requested. Waiting for the next safe checkpoint.", "success");
      if (!state.wsReady) {
        startPolling();
      }
    } catch (error) {
      setFormStatus(`Stop failed: ${error instanceof Error ? error.message : "unknown error"}`, "error");
    }
  }

  async function rerunRun(runId) {
    if (!runId) {
      return;
    }
    setFormStatus("Starting rerun from saved backtest parameters...", "working");
    try {
      const response = await fetch(withAuthPath(`/api/backtests/runs/${encodeURIComponent(runId)}/rerun`), {
        method: "POST",
      });
      const data = await response.json();
      if (!response.ok) {
        setFormStatus(`Rerun failed: ${data.detail || "run not found"}`, "error");
        return;
      }
      state.activeRunId = data.run.run_id;
      state.activeRun = data.run;
      upsertRun(data.run);
      closePanelModal();
      closeModal();
      renderRun(data.run);
      setFormStatus("Rerun started with the previous run parameters.", "success");
      if (!state.wsReady) {
        startPolling();
      }
    } catch (error) {
      setFormStatus(`Rerun failed: ${error instanceof Error ? error.message : "unknown error"}`, "error");
    }
  }

  function connectWebSocket() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}${withAuthPath("/ws/backtests")}`;
    try {
      state.ws = new WebSocket(wsUrl);
    } catch (_error) {
      startPolling();
      return;
    }
    state.ws.onopen = () => {
      state.wsReady = true;
      if (state.pollTimer) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    };
    state.ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data || "{}"));
        handleRealtimeMessage(payload);
      } catch (_error) {
        // no-op
      }
    };
    state.ws.onclose = () => {
      state.wsReady = false;
      state.ws = null;
      startPolling();
      window.setTimeout(connectWebSocket, 1500);
    };
    state.ws.onerror = () => {
      state.wsReady = false;
    };
  }

  function handleRealtimeMessage(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.type === "bootstrap") {
      renderReadiness(payload.readiness || {});
      state.recentRuns = payload.runs || [];
      renderRecentRuns(state.recentRuns);
      if (!state.activeRunId && state.recentRuns.length) {
        state.activeRunId = state.recentRuns[0].run_id;
        state.activeRun = state.recentRuns[0];
        renderRun(state.activeRun);
      }
      renderFilterBar();
      return;
    }
    if (payload.type !== "backtest_run" || !payload.run) {
      return;
    }
    const run = payload.run;
    upsertRun(run);
    renderRecentRuns(state.recentRuns);
    if (!state.activeRunId || state.activeRunId === run.run_id || run.status === "running") {
      state.activeRunId = run.run_id;
      state.activeRun = run;
      renderRun(run);
    }
  }

  function setFormStatus(message, kind) {
    nodes.formStatus.textContent = message;
    nodes.formStatus.className = `inline-status ${kind}`;
  }

  function applyDateRange(fromIso, toIso) {
    nodes.fromDate.value = toLocalInputValue(fromIso);
    nodes.toDate.value = toLocalInputValue(toIso);
  }

  function toLocalInputValue(isoString) {
    if (!isoString) {
      return "";
    }
    const value = new Date(isoString);
    if (Number.isNaN(value.getTime())) {
      return "";
    }
    const offset = value.getTimezoneOffset();
    const local = new Date(value.getTime() - offset * 60 * 1000);
    return local.toISOString().slice(0, 16);
  }

  function formatDate(value) {
    if (!value) {
      return "n/a";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  function formatTehranDate(value) {
    if (!value) {
      return "n/a";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Tehran",
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  function pnlClassName(value) {
    const numeric = Number(value || 0);
    if (numeric > 0) {
      return "pnl-positive";
    }
    if (numeric < 0) {
      return "pnl-negative";
    }
    return "pnl-flat";
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function matchesMessageFilter(trace) {
    switch (state.messageFilter) {
      case "signals":
        return trace.parsed_action === "open" || trace.classification === "new_signal";
      case "updates":
        return ["update_sl", "update_tp", "update_leverage", "cancel", "close"].includes(trace.parsed_action);
      case "invalid":
        return ["invalid_signal", "market_data_unavailable"].includes(trace.final_status);
      case "ignored":
        return trace.final_status === "ignored" || trace.parsed_action === "ignore";
      case "ambiguous":
        return trace.final_status === "ambiguous" || trace.classification === "ambiguous";
      default:
        return true;
    }
  }

  function isActiveStatus(status) {
    return status === "queued" || status === "running" || status === "cancelling";
  }

  function buildEventFeedMarkup(events, expanded) {
    if (!events.length) {
      return '<div class="empty-state-box">No activity yet.</div>';
    }
    return `
      <div class="event-feed ${expanded ? "panel-modal-list" : ""}">
        ${events
          .slice()
          .reverse()
          .slice(0, 80)
          .map((event) => `
            <article class="event-item event-${escapeHtml(event.status)}">
              <div class="event-line">
                <strong>${escapeHtml(replaceUnderscores(event.phase))}</strong>
                <span>${formatDate(event.at)}</span>
              </div>
              <p>${escapeHtml(event.summary)}</p>
              ${event.current_message_id ? `<small>message ${escapeHtml(String(event.current_message_id))}</small>` : ""}
            </article>
          `)
          .join("")}
      </div>
    `;
  }

  function buildRecentRunsMarkup(runs, expanded) {
    if (!runs.length) {
      return '<div class="empty-state-box">No previous runs found.</div>';
    }
    return `
      <div class="recent-runs ${expanded ? "panel-modal-list" : ""}">
        ${runs
          .map((run) => `
            <article class="recent-run-card ${state.activeRunId === run.run_id ? "active" : ""}">
              <button type="button" class="recent-run-select" data-run-id="${escapeHtml(run.run_id)}">
                <strong>${escapeHtml(run.channel_input || run.channel_resolved)}</strong>
                <span>${escapeHtml(run.current_phase_label || run.current_phase || run.status)}</span>
                <small>${escapeHtml(run.strategy_key || "default_risk_managed")}</small>
                <small>${formatDate(run.created_at)}</small>
              </button>
              <div class="recent-run-actions">
                ${
                  isActiveStatus(run.status)
                    ? `<button type="button" class="danger compact-action" data-stop-run-id="${escapeHtml(run.run_id)}">Stop</button>`
                    : ""
                }
                <button type="button" class="ghost-button compact-action" data-rerun-run-id="${escapeHtml(run.run_id)}">Rerun</button>
              </div>
            </article>
          `)
          .join("")}
      </div>
    `;
  }

  function formatStrategyKey(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function replaceUnderscores(value) {
    return String(value || "").replace(/_/g, " ");
  }

  function getAuthToken() {
    const search = new URLSearchParams(window.location.search || "");
    return search.get("token") || "";
  }

  function withAuthPath(path) {
    const token = getAuthToken();
    if (!token) {
      return path;
    }
    const url = new URL(path, window.location.origin);
    if (!url.searchParams.get("token")) {
      url.searchParams.set("token", token);
    }
    return `${url.pathname}${url.search}${url.hash}`;
  }
})();
