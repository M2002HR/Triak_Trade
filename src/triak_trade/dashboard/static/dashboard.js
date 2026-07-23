document.documentElement.dataset.dashboardReady = "true";

(() => {
  const bootstrapNode = document.getElementById("backtest-bootstrap");
  if (!bootstrapNode) {
    return;
  }

  const bootstrap = JSON.parse(bootstrapNode.textContent || "{}");
  const initialRecentRuns = Array.isArray(bootstrap.recent_runs) ? bootstrap.recent_runs : [];
  const activeRunIdFromUrl = new URLSearchParams(window.location.search).get("run_id");
  const runStorageKey = `triak-active-backtest-${bootstrap.default_run_type || "portfolio"}`;
  const storedActiveRunId = readStoredRunId(runStorageKey);
  const state = {
    bootstrap,
    runType: bootstrap.default_run_type || "portfolio",
    savedChannels: Array.isArray(bootstrap.saved_channels) ? bootstrap.saved_channels : [],
    activeRunId: activeRunIdFromUrl
      || bootstrap.active_run_id
      || storedActiveRunId
      || (initialRecentRuns.length ? initialRecentRuns[0].run_id : null),
    activeRun: null,
    recentRuns: initialRecentRuns,
    recentRunsTotal: initialRecentRuns.length,
    recentRunsHasMore: false,
    recentRunsLoadingMore: false,
    recentRunsPageSize: 8,
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
    wsPingTimer: null,
    recoveringLatest: false,
  };

  const nodes = {
    form: document.getElementById("backtest-live-form"),
    runType: document.getElementById("backtest-run-type"),
    modeTabs: document.getElementById("backtest-mode-tabs"),
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
    isolatedConfig: document.getElementById("isolated-backtest-config"),
    isolatedCapitalPerSignal: document.getElementById("isolated-capital-per-signal"),
    isolatedFillPolicy: document.getElementById("isolated-fill-policy"),
    isolatedLeverageSource: document.getElementById("isolated-leverage-source"),
    isolatedFixedLeverage: document.getElementById("isolated-fixed-leverage"),
    isolatedMaxEffectiveLeverage: document.getElementById("isolated-max-effective-leverage"),
    isolatedDefaultSignalLeverage: document.getElementById("isolated-default-signal-leverage"),
    isolatedMinAllocationPct: document.getElementById("isolated-min-allocation-pct"),
    isolatedMaxAllocationPct: document.getElementById("isolated-max-allocation-pct"),
    isolatedDefaultStopPct: document.getElementById("isolated-default-stop-pct"),
    isolatedSyntheticStopMaxLossPct: document.getElementById("isolated-synthetic-stop-max-loss-pct"),
    isolatedFeeRatePct: document.getElementById("isolated-fee-rate-pct"),
    isolatedLifecycleRefreshInterval: document.getElementById("isolated-lifecycle-refresh-interval"),
    isolatedMaxParallelSignals: document.getElementById("isolated-max-parallel-signals"),
    isolatedIncludeNotFilledSignals: document.getElementById("isolated-include-not-filled-signals"),
    isolatedCloseOpenPositionsAtEnd: document.getElementById("isolated-close-open-positions-at-end"),
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
    progressLabel: document.getElementById("backtest-progress-label"),
    runtimeLabel: document.getElementById("backtest-runtime-label"),
    progressTrack: document.getElementById("backtest-progress-track"),
    progressFill: document.getElementById("backtest-progress-fill"),
    progressMeta: document.getElementById("backtest-progress-meta"),
    phaseProgressGrid: document.getElementById("phase-progress-grid"),
    messageCountLabel: document.getElementById("message-count-label"),
    messageFilterBar: document.getElementById("message-filter-bar"),
    messageStream: document.getElementById("message-stream"),
    eventFeed: document.getElementById("event-feed"),
    recentRuns: document.getElementById("recent-runs"),
    signalStatePreview: document.getElementById("signal-state-preview"),
    isolatedAggregatePreview: document.getElementById("isolated-aggregate-preview"),
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
  renderEmptyRun();
  if (state.activeRunId) {
    fetchRun();
  }
  connectWebSocket();
  refreshRunsList();

  function seedDefaults() {
    nodes.interval.value = bootstrap.default_interval || "1m";
    nodes.maxMessages.value = String(bootstrap.default_max_messages || 1000);
    if (nodes.initialBalance) {
      nodes.initialBalance.value = String(bootstrap.default_initial_balance || "100");
    }
    nodes.riskPerTradePct.value = String(bootstrap.default_risk_per_trade_pct || "120");
    if (nodes.runType) {
      nodes.runType.value = state.runType;
    }
    if (nodes.isolatedCapitalPerSignal) {
      nodes.isolatedCapitalPerSignal.value = String(bootstrap.default_capital_per_signal || "100");
      nodes.isolatedFillPolicy.value = bootstrap.default_fill_policy || "conservative";
      nodes.isolatedLeverageSource.value = bootstrap.default_leverage_source || "signal_or_default";
      nodes.isolatedFixedLeverage.value = String(bootstrap.default_fixed_leverage || "50");
      nodes.isolatedMaxEffectiveLeverage.value = String(bootstrap.default_max_effective_leverage || "50");
      nodes.isolatedDefaultSignalLeverage.value = String(bootstrap.default_signal_leverage || "50");
      nodes.isolatedMinAllocationPct.value = String(bootstrap.default_min_allocation_pct || "2");
      nodes.isolatedMaxAllocationPct.value = String(bootstrap.default_max_allocation_pct || "20");
      nodes.isolatedSyntheticStopMaxLossPct.value = String(bootstrap.default_synthetic_stop_max_loss_pct || "5");
      nodes.isolatedFeeRatePct.value = String(bootstrap.default_fee_rate_pct || "0");
      nodes.isolatedLifecycleRefreshInterval.value = bootstrap.default_lifecycle_refresh_interval || "30m";
      nodes.isolatedMaxParallelSignals.value = String(bootstrap.default_max_parallel_signals || "4");
      nodes.isolatedIncludeNotFilledSignals.checked = Boolean(bootstrap.default_include_not_filled_signals);
      nodes.isolatedCloseOpenPositionsAtEnd.checked = Boolean(bootstrap.default_close_open_positions_at_end);
    }
    if (nodes.strategyKey) {
      nodes.strategyKey.value = bootstrap.default_strategy_key || "default_risk_managed";
    }
    if (nodes.useAi) {
      nodes.useAi.checked = Boolean(bootstrap.default_use_ai);
    }
    if (nodes.sendLogChannel) {
      nodes.sendLogChannel.checked = Boolean(bootstrap.default_send_log_channel);
    }
    if (nodes.logPerMessage) {
      nodes.logPerMessage.checked = Boolean(bootstrap.default_log_per_message);
    }
    applyDateRange(bootstrap.default_from_date, bootstrap.default_to_date);
    applyRunType(state.runType);
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
    if (nodes.modeTabs) {
      nodes.modeTabs.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-backtest-mode]") : null;
        if (!target) {
          return;
        }
        applyRunType(target.getAttribute("data-backtest-mode") || "portfolio");
      });
    }
    if (nodes.isolatedLeverageSource) {
      nodes.isolatedLeverageSource.addEventListener("change", syncIsolatedLeverageInputs);
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
      const loadMoreTarget = event.target instanceof Element ? event.target.closest("[data-load-more-runs]") : null;
      if (loadMoreTarget) {
        event.preventDefault();
        event.stopPropagation();
        loadMoreRuns();
        return;
      }
      const target = event.target instanceof Element ? event.target.closest("[data-run-id]") : null;
      if (!target) {
        return;
      }
      rememberActiveRun(target.getAttribute("data-run-id"), { updateUrl: true });
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

  function applyRunType(runType) {
    state.runType = runType === "isolated" ? "isolated" : "portfolio";
    if (nodes.runType) {
      nodes.runType.value = state.runType;
    }
    if (nodes.modeTabs) {
      nodes.modeTabs.querySelectorAll("[data-backtest-mode]").forEach((button) => {
        const active = button.getAttribute("data-backtest-mode") === state.runType;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
    }
    if (nodes.isolatedConfig) {
      nodes.isolatedConfig.hidden = state.runType !== "isolated";
    }
    syncIsolatedLeverageInputs();
  }

  function syncIsolatedLeverageInputs() {
    if (!nodes.isolatedFixedLeverage || !nodes.isolatedLeverageSource) {
      return;
    }
    nodes.isolatedFixedLeverage.disabled = nodes.isolatedLeverageSource.value !== "fixed";
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
    } else if (kind === "aggregate") {
      nodes.panelModalTitle.textContent = "Aggregate Analytics";
      nodes.panelModalBody.innerHTML = buildAggregateMarkup(
        (state.activeRun && state.activeRun.isolated_aggregate) || {},
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
    disposeChart("signal-lifecycle-chart");
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
      rememberActiveRun(data.run.run_id);
      state.activeRun = mergeRunPayload(state.activeRun, data.run);
      upsertRun(data.run);
      renderRun(state.activeRun);
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
    const payload = {
      run_type: state.runType,
      channel: nodes.channel.value.trim(),
      from_date: fromDate.toISOString(),
      to_date: toDate.toISOString(),
      start_message_link: nodes.startMessageLink.value.trim(),
      interval: nodes.interval.value,
      max_messages: Number(nodes.maxMessages.value || "1000"),
      initial_balance: nodes.initialBalance ? nodes.initialBalance.value.trim() : "",
      risk_per_trade_pct: nodes.riskPerTradePct.value.trim(),
      strategy_key: (nodes.strategyKey ? nodes.strategyKey.value : "")
        || bootstrap.default_strategy_key
        || "default_risk_managed",
      use_ai: nodes.useAi ? nodes.useAi.checked : Boolean(bootstrap.default_use_ai),
      send_log_channel: nodes.sendLogChannel
        ? nodes.sendLogChannel.checked
        : Boolean(bootstrap.default_send_log_channel),
      log_per_message: nodes.logPerMessage
        ? nodes.logPerMessage.checked
        : Boolean(bootstrap.default_log_per_message),
    };
    if (state.runType === "isolated") {
      payload.capital_per_signal = nodes.isolatedCapitalPerSignal.value.trim();
      payload.fill_policy = nodes.isolatedFillPolicy.value;
      payload.leverage_source = nodes.isolatedLeverageSource.value;
      payload.fixed_leverage = nodes.isolatedFixedLeverage.value.trim();
      payload.max_effective_leverage = nodes.isolatedMaxEffectiveLeverage.value.trim();
      payload.default_signal_leverage = nodes.isolatedDefaultSignalLeverage.value.trim();
      payload.min_allocation_pct = nodes.isolatedMinAllocationPct.value.trim();
      payload.max_allocation_pct = nodes.isolatedMaxAllocationPct.value.trim();
      payload.synthetic_stop_max_loss_pct = nodes.isolatedSyntheticStopMaxLossPct.value.trim();
      payload.fee_rate_pct = nodes.isolatedFeeRatePct.value.trim();
      payload.lifecycle_refresh_interval = nodes.isolatedLifecycleRefreshInterval.value;
      payload.max_parallel_signals = Number(nodes.isolatedMaxParallelSignals.value || "1");
      payload.include_not_filled_signals = nodes.isolatedIncludeNotFilledSignals.checked;
      payload.close_open_positions_at_end = nodes.isolatedCloseOpenPositionsAtEnd.checked;
    }
    return payload;
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
        if (response.status === 404) {
          await recoverLatestRun();
        }
        return;
      }
      const run = await response.json();
      if (!belongsToWorkbench(run)) {
        await recoverLatestRun();
        return;
      }
      state.activeRun = mergeRunPayload(state.activeRun, run);
      upsertRun(run);
      renderRun(state.activeRun);
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
      const response = await fetch(
        withAuthPath(
          `/api/backtests/runs?limit=${state.recentRunsPageSize}&offset=0&run_type=${encodeURIComponent(state.runType)}`,
        ),
      );
      if (response.ok) {
        const data = await response.json();
        applyRunsResponse(data, { preserveLoadedTail: true });
        if (!state.activeRunId && state.recentRuns.length) {
          rememberActiveRun(state.recentRuns[0].run_id);
          fetchRun();
        } else if (state.activeRunId && !state.activeRun) {
          fetchRun();
        }
      }
    } catch (_error) {
      // Keep silent on list refresh; main run polling is more important.
    }
    state.listTimer = window.setTimeout(refreshRunsList, 10000);
  }

  async function loadMoreRuns() {
    if (state.recentRunsLoadingMore || !state.recentRunsHasMore) {
      return;
    }
    state.recentRunsLoadingMore = true;
    if (state.panelModalOpen && state.activePanelModal === "history") {
      nodes.panelModalBody.innerHTML = buildRecentRunsMarkup(state.recentRuns, true);
    }
    try {
      const response = await fetch(
        withAuthPath(
          `/api/backtests/runs?limit=${state.recentRunsPageSize}&offset=${state.recentRuns.length}&run_type=${encodeURIComponent(state.runType)}`,
        ),
      );
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      applyRunsResponse(data, { append: true });
    } finally {
      state.recentRunsLoadingMore = false;
      if (state.panelModalOpen && state.activePanelModal === "history") {
        nodes.panelModalBody.innerHTML = buildRecentRunsMarkup(state.recentRuns, true);
      }
    }
  }

  function applyRunsResponse(payload, options = {}) {
    const incoming = Array.isArray(payload.runs)
      ? payload.runs.filter(belongsToWorkbench)
      : [];
    const totalRuns = Number(payload.total_runs || incoming.length || 0);
    const hasMore = Boolean(payload.has_more);
    state.recentRunsTotal = totalRuns;
    state.recentRunsHasMore = hasMore;

    if (options.append) {
      const runs = Array.isArray(state.recentRuns) ? [...state.recentRuns] : [];
      incoming.forEach((run) => {
        const index = runs.findIndex((item) => item.run_id === run.run_id);
        if (index >= 0) {
          runs[index] = run;
        } else {
          runs.push(run);
        }
      });
      state.recentRuns = sortRuns(runs);
    } else if (options.preserveLoadedTail && state.recentRuns.length > incoming.length) {
      const existingTail = state.recentRuns.filter(
        (item) => !incoming.some((run) => run.run_id === item.run_id),
      );
      state.recentRuns = sortRuns([...incoming, ...existingTail]);
    } else {
      state.recentRuns = sortRuns(incoming);
    }

    renderRecentRuns(state.recentRuns);
  }

  function sortRuns(runs) {
    return [...runs].sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
  }

  function mergeRunPayload(existingRun, incomingRun, latestTrace = null) {
    const base = existingRun && existingRun.run_id === incomingRun.run_id ? existingRun : null;
    const merged = { ...(base || {}), ...incomingRun };
    const incomingMessages = Array.isArray(incomingRun.messages) ? incomingRun.messages : null;
    if (incomingMessages) {
      merged.messages = incomingMessages;
    } else if (base && Array.isArray(base.messages)) {
      merged.messages = base.messages;
    } else {
      merged.messages = [];
    }
    if (latestTrace && typeof latestTrace === "object") {
      merged.messages = upsertTrace(merged.messages, latestTrace);
    }
    return merged;
  }

  function upsertTrace(existingMessages, trace) {
    const messages = Array.isArray(existingMessages) ? [...existingMessages] : [];
    const index = messages.findIndex((item) => item.message_id === trace.message_id);
    if (index >= 0) {
      messages[index] = { ...messages[index], ...trace };
    } else {
      messages.push(trace);
    }
    return messages
      .sort((left, right) => new Date(right.message_date) - new Date(left.message_date))
      .slice(0, 500);
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
    renderProgress(run);
    renderPhaseProgress(run);
    renderEventFeed(run.events || []);
    renderMessages(run.messages || []);
    renderSignals(run.signals || []);
    renderAggregate(run.isolated_aggregate || {});
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
    } else if (state.panelModalOpen && state.activePanelModal === "aggregate") {
      nodes.panelModalBody.innerHTML = buildAggregateMarkup(run.isolated_aggregate || {}, true);
    }
  }

  function renderCurrentRunHeader(run) {
    nodes.activeRunHeadline.textContent = isActiveStatus(run.status) ? "Streaming" : run.current_phase_label;
    const modeLabel = run.run_type === "isolated" ? "Isolated" : "Portfolio";
    nodes.runTitle.textContent = `${run.channel_resolved} • ${run.interval} • ${modeLabel}`;
    const startMessageSuffix = run.start_message_id
      ? ` • from message ${run.start_message_id}`
      : "";
    const strategySuffix = run.strategy_key ? ` • strategy ${run.strategy_key}` : "";
    const checkpointSuffix = run.heartbeat_at
      ? ` • checkpoint ${formatDate(run.heartbeat_at)}`
      : "";
    nodes.runSubtitle.textContent = `${formatDate(run.from_date)} → ${formatDate(run.to_date)}${startMessageSuffix}${strategySuffix}${checkpointSuffix}`;
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

  function renderProgress(run) {
    if (!nodes.progressLabel || !nodes.progressTrack || !nodes.progressFill || !nodes.progressMeta) {
      return;
    }
    if (nodes.runtimeLabel) {
      nodes.runtimeLabel.textContent = formatElapsedMs(Number(run.runtime_duration_ms || 0));
    }
    const totalMessages = Number(run.total_messages || 0);
    const fallbackProcessed = Array.isArray(run.messages) ? run.messages.length : 0;
    const processedMessages = Math.max(Number(run.processed_messages || 0), fallbackProcessed);
    const safeProcessed = totalMessages > 0
      ? Math.min(processedMessages, totalMessages)
      : processedMessages;
    const percent = totalMessages > 0
      ? Math.min(100, Math.round((safeProcessed / totalMessages) * 100))
      : 0;

    nodes.progressLabel.textContent = `${percent}%`;
    nodes.progressFill.style.width = `${percent}%`;
    nodes.progressTrack.setAttribute("aria-valuenow", String(percent));

    if (!totalMessages) {
      nodes.progressMeta.textContent = isActiveStatus(run.status)
        ? "Preparing the message queue..."
        : "Waiting for Telegram history.";
      return;
    }

    const remaining = Math.max(totalMessages - safeProcessed, 0);
    const noun = totalMessages === 1 ? "message" : "messages";
    const remainingNoun = remaining === 1 ? "message" : "messages";
    nodes.progressMeta.textContent = `${safeProcessed} of ${totalMessages} ${noun} processed • ${remaining} ${remainingNoun} remaining`;
  }

  function renderMetrics(run) {
    const cards = [
      ["Run Type", run.run_type === "isolated" ? "Isolated" : "Portfolio"],
      ["Messages", run.total_messages],
      ["Classified", run.classified_messages],
      ["Parsed Signals", run.parsed_signals],
      ["Valid Signals", run.valid_signals],
      ["Ignored", run.ignored_messages],
      ["Ambiguous", run.ambiguous_messages],
      ["Trades Simulated", run.trades_simulated],
      ["Trades Filled", run.trades_filled],
      ["Initial Balance", run.initial_balance],
      ...(run.run_type === "isolated"
        ? [["Capital Per Signal", run.capital_per_signal || run.initial_balance]]
        : []),
      ["Allocation Factor", run.risk_per_trade_pct],
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
        <small>${state.recentRunsTotal || runs.length} runs available</small>
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
    if (nodes.runtimeLabel) {
      nodes.runtimeLabel.textContent = "00:00:00";
    }
    nodes.currentPhaseLabel.textContent = "Queued";
    nodes.currentPhaseSummary.textContent = "Waiting to start.";
    nodes.currentMessageLabel.textContent = "None";
    nodes.currentMessageSummary.textContent = "No message is being processed yet.";
    if (nodes.progressLabel) {
      nodes.progressLabel.textContent = "0%";
    }
    if (nodes.progressFill) {
      nodes.progressFill.style.width = "0%";
    }
    if (nodes.progressTrack) {
      nodes.progressTrack.setAttribute("aria-valuenow", "0");
    }
    if (nodes.progressMeta) {
      nodes.progressMeta.textContent = "Waiting for Telegram history.";
    }
    renderPhaseProgress({
      status: "queued",
      current_phase: "queued",
      total_messages: 0,
      classified_messages: 0,
      history_steps_total: 1,
      history_steps_completed: 0,
      market_data_targets_total: 0,
      market_data_targets_completed: 0,
      simulation_targets_total: 0,
      simulation_targets_completed: 0,
      report_steps_total: 1,
      report_steps_completed: 0,
      events: [],
    });
    renderSignals([]);
    renderAggregate({});
  }

  function renderPhaseProgress(run) {
    if (!nodes.phaseProgressGrid) {
      return;
    }
    const phaseOrder = ["fetch_history", "classify_messages", "fetch_market_data", "simulate", "report"];
    const eventByPhase = new Map();
    (Array.isArray(run.events) ? run.events : []).forEach((event) => {
      if (event && typeof event.phase === "string") {
        eventByPhase.set(event.phase, event);
      }
    });
    const phaseSpecs = [
      {
        key: "fetch_history",
        label: "Telegram History",
        completed: Number(run.history_steps_completed || 0),
        total: Math.max(Number(run.history_steps_total || 1), 1),
        fallbackSummary: "Fetch Telegram history for the requested range.",
      },
      {
        key: "classify_messages",
        label: "Classification",
        completed: Number(run.classified_messages || 0),
        total: Number(run.total_messages || 0),
        fallbackSummary: "Classify each message and build the replay timeline.",
      },
      {
        key: "fetch_market_data",
        label: "Market Data",
        completed: Number(run.market_data_targets_completed || 0),
        total: Number(run.market_data_targets_total || 0),
        fallbackSummary: "Fetch candle sets for the detected replay symbols.",
      },
      {
        key: "simulate",
        label: "Simulation",
        completed: Number(run.simulation_targets_completed || 0),
        total: Number(run.simulation_targets_total || 0),
        fallbackSummary: "Run the trade simulation for each eligible signal.",
      },
      {
        key: "report",
        label: "Report",
        completed: Number(run.report_steps_completed || 0),
        total: Math.max(Number(run.report_steps_total || 1), 1),
        fallbackSummary: "Write the final report payload and summary files.",
      },
    ];
    nodes.phaseProgressGrid.innerHTML = phaseSpecs
      .map((phase) => {
        const latestEvent = eventByPhase.get(phase.key);
        const status = resolvePhaseStatus(run, phase.key, latestEvent, phaseOrder, phase);
        const phaseDurationMs = phaseDurationForRun(run, phase.key);
        const { percent, meta } = phaseProgressMetrics(phase, status, phaseDurationMs);
        const summary = (latestEvent && latestEvent.summary)
          || (run.current_phase === phase.key ? run.current_phase_summary : "")
          || phase.fallbackSummary;
        return `
          <article class="phase-progress-card">
            <div class="phase-progress-card-head">
              <strong>${escapeHtml(phase.label)}</strong>
              <span class="phase-pill phase-${escapeHtml(status)}">${escapeHtml(replaceUnderscores(status))}</span>
            </div>
            <div
              class="backtest-progress-track"
              role="progressbar"
              aria-label="${escapeHtml(phase.label)} progress"
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow="${escapeHtml(String(percent))}"
            >
              <div class="backtest-progress-fill" style="width: ${escapeHtml(String(percent))}%"></div>
            </div>
            <p class="backtest-progress-meta">${escapeHtml(meta)}</p>
            <small>${escapeHtml(summary)}</small>
          </article>
        `;
      })
      .join("");
  }

  function phaseDurationForRun(run, phaseKey) {
    const phaseDurations = (run && typeof run.phase_durations_ms === "object" && run.phase_durations_ms) || {};
    if (run && run.current_phase === phaseKey && Number(run.current_phase_elapsed_ms || 0) > 0) {
      return Number(run.current_phase_elapsed_ms || 0);
    }
    return Number(phaseDurations[phaseKey] || 0);
  }

  function resolvePhaseStatus(run, phaseKey, latestEvent, phaseOrder, phase) {
    if (latestEvent && latestEvent.status) {
      return latestEvent.status;
    }
    if (run.status === "failed" && run.current_phase === phaseKey) {
      return "failed";
    }
    if (run.status === "cancelled" || run.status === "cancelling") {
      if (run.current_phase === phaseKey) {
        return run.status;
      }
    }
    if (phase.total > 0 && phase.completed >= phase.total) {
      return "completed";
    }
    if (run.current_phase === phaseKey) {
      return "running";
    }
    const currentIndex = phaseOrder.indexOf(run.current_phase);
    const phaseIndex = phaseOrder.indexOf(phaseKey);
    if (currentIndex > phaseIndex && phase.completed > 0) {
      return "completed";
    }
    return "queued";
  }

  function phaseProgressMetrics(phase, status, durationMs) {
    const total = Number(phase.total || 0);
    const completed = Math.max(0, Number(phase.completed || 0));
    const durationLabel = Number(durationMs || 0) > 0
      ? `Elapsed ${formatElapsedMs(durationMs)}`
      : null;
    if (total > 0) {
      const safeCompleted = Math.min(completed, total);
      return {
        percent: Math.min(100, Math.round((safeCompleted / total) * 100)),
        meta: durationLabel
          ? `${safeCompleted} of ${total} complete · ${durationLabel}`
          : `${safeCompleted} of ${total} complete`,
      };
    }
    if (status === "completed") {
      return {
        percent: 100,
        meta: durationLabel ? `Completed · ${durationLabel}` : "Completed",
      };
    }
    if (status === "running") {
      return {
        percent: 0,
        meta: durationLabel ? `Running · ${durationLabel}` : "Running",
      };
    }
    if (status === "failed") {
      return {
        percent: 0,
        meta: durationLabel ? `Failed · ${durationLabel}` : "Failed",
      };
    }
    return {
      percent: 0,
      meta: durationLabel ? `Waiting · ${durationLabel}` : "Waiting",
    };
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

  function renderAggregate(aggregate) {
    if (!nodes.isolatedAggregatePreview) {
      return;
    }
    if (!aggregate || !Object.keys(aggregate).length) {
      nodes.isolatedAggregatePreview.textContent = "No isolated aggregate metrics yet.";
      nodes.isolatedAggregatePreview.classList.add("empty-state-box");
      if (state.panelModalOpen && state.activePanelModal === "aggregate") {
        nodes.panelModalBody.innerHTML = buildAggregateMarkup({}, true);
      }
      return;
    }
    nodes.isolatedAggregatePreview.classList.remove("empty-state-box");
    nodes.isolatedAggregatePreview.innerHTML = `
      <div class="preview-stack signal-preview-stack">
        <strong>${escapeHtml(String(aggregate.total_signals || 0))} signals</strong>
        <span>PnL ${escapeHtml(String(aggregate.total_pnl || "0"))} • Win rate ${escapeHtml(formatPercentValue(aggregate.win_rate))}</span>
        <small>${escapeHtml(String(aggregate.open_signals || 0))} open • ${escapeHtml(String(aggregate.closed_signals || 0))} closed</small>
      </div>
    `;
    if (state.panelModalOpen && state.activePanelModal === "aggregate") {
      nodes.panelModalBody.innerHTML = buildAggregateMarkup(aggregate, true);
    }
  }

  function buildAggregateMarkup(aggregate, expanded) {
    if (!aggregate || !Object.keys(aggregate).length) {
      return '<div class="empty-state-box">No isolated aggregate metrics yet.</div>';
    }
    const dailyRows = buildPeriodRows(aggregate.period_pnl && aggregate.period_pnl.daily);
    const weeklyRows = buildPeriodRows(aggregate.period_pnl && aggregate.period_pnl.weekly);
    const monthlyRows = buildPeriodRows(aggregate.period_pnl && aggregate.period_pnl.monthly);
    const symbolRows = Array.isArray(aggregate.symbol_summary) ? aggregate.symbol_summary : [];
    return `
      <div class="signal-detail-shell ${expanded ? "panel-modal-list" : ""}">
        <div class="metrics backtest-metrics">
          ${[
            ["Total Signals", aggregate.total_signals],
            ["Filled", aggregate.filled_signals],
            ["Wins / Losses", `${aggregate.wins} / ${aggregate.losses}`],
            ["Win Rate", formatPercentValue(aggregate.win_rate)],
            ["Total PnL", aggregate.total_pnl],
            ["Avg PnL", aggregate.avg_pnl],
            ["Median PnL", aggregate.median_pnl],
            ["Max Drawdown", aggregate.max_drawdown],
            ["Final Balance", aggregate.total_final_balance],
          ].map(([label, value]) => `
            <div class="metric-card">
              <span>${escapeHtml(String(label))}</span>
              <strong>${escapeHtml(String(value ?? 0))}</strong>
            </div>
          `).join("")}
        </div>
        <div class="signal-detail-meta">
          <div class="meta-chip"><strong>Status Counts</strong><span>${escapeHtml(JSON.stringify(aggregate.status_counts || {}))}</span></div>
          <div class="meta-chip"><strong>Profit Factor</strong><span>${escapeHtml(String(aggregate.profit_factor ?? "n/a"))}</span></div>
        </div>
        <div class="signal-detail-section">
          <h3>Daily</h3>
          ${dailyRows}
        </div>
        <div class="signal-detail-section">
          <h3>Weekly</h3>
          ${weeklyRows}
        </div>
        <div class="signal-detail-section">
          <h3>Monthly</h3>
          ${monthlyRows}
        </div>
        <div class="signal-detail-section">
          <h3>By Symbol</h3>
          ${symbolRows.length ? `
            <div class="signal-list ${expanded ? "panel-modal-list" : ""}">
              ${symbolRows.map((row) => `
                <div class="signal-card inactive">
                  <div class="signal-card-top">
                    <div>
                      <strong>${escapeHtml(row.symbol || "unknown")}</strong>
                      <span>${escapeHtml(String(row.signals || 0))} signals</span>
                    </div>
                    <span class="signal-state-chip inactive">PnL ${escapeHtml(String(row.pnl || "0"))}</span>
                  </div>
                </div>
              `).join("")}
            </div>
          ` : '<div class="empty-state-box">No symbol analytics yet.</div>'}
        </div>
      </div>
    `;
  }

  function buildPeriodRows(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return '<div class="empty-state-box">No closed isolated signals in this period.</div>';
    }
    return `
      <div class="signal-list panel-modal-list">
        ${rows.map((row) => `
          <div class="signal-card inactive">
            <div class="signal-card-top">
              <div>
                <strong>${escapeHtml(String(row.period || ""))}</strong>
                <span>${escapeHtml(String(row.signals || row.trades || 0))} signals</span>
              </div>
              <span class="signal-state-chip inactive">PnL ${escapeHtml(String(row.pnl || "0"))}</span>
            </div>
            <div class="signal-card-bottom">
              <span>${escapeHtml(String(row.wins || 0))} wins</span>
              <strong>${escapeHtml(String(row.losses || 0))} losses</strong>
            </div>
          </div>
        `).join("")}
      </div>
    `;
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
    disposeChart("signal-lifecycle-chart");
    nodes.panelModalBody.innerHTML = buildSignalDetailMarkup(signal);
    window.requestAnimationFrame(() => {
      renderSignalLifecycleChart(signal);
      resizeChart("signal-lifecycle-chart");
    });
    syncBodyModalState();
  }

  function buildSignalDetailMarkup(signal) {
    const takeProfits = Array.isArray(signal.take_profits) ? signal.take_profits : [];
    const leverageLabel = formatSignalLeverage(signal);
    const visibleChartPoints = Number(
      signal.chart?.visible_points ?? ((signal.chart?.candles || []).length || 0),
    );
    const sourceChartPoints = Number(signal.chart?.source_points ?? visibleChartPoints);
    const pointLabel = sourceChartPoints > visibleChartPoints
      ? `${visibleChartPoints} of ${sourceChartPoints}`
      : String(visibleChartPoints);
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
          ${detailMetric("Leverage", leverageLabel)}
          ${detailMetric("Margin", signal.margin || "0")}
          ${detailMetric("Balance Basis", signal.balance_basis || "0")}
          ${detailMetric("Original Quantity", signal.original_quantity || "0")}
          ${detailMetric("Open Quantity", signal.open_quantity || "0")}
          ${detailMetric("Risk Amount", signal.risk_amount || "0")}
          ${detailMetric("Notional Value", signal.notional_value || "0")}
          ${detailMetric("Targets Hit", signal.targets_hit ?? "0")}
          ${detailMetric("Total PnL", signal.total_pnl ?? "0", pnlClassName(signal.total_pnl))}
          ${detailMetric("Total PnL %", signal.total_pnl_pct ?? "0", pnlClassName(signal.total_pnl_pct))}
          ${detailMetric("Margin ROI %", signal.margin_pnl_pct ?? "0", pnlClassName(signal.margin_pnl_pct))}
          ${detailMetric("Realized PnL", signal.realized_pnl ?? "0", pnlClassName(signal.realized_pnl))}
          ${detailMetric("Unrealized PnL", signal.unrealized_pnl ?? "0", pnlClassName(signal.unrealized_pnl))}
          ${signal.message_link ? `<div class="metric-item"><span class="metric-label">Telegram Link</span><span class="metric-value"><a href="${escapeHtml(signal.message_link)}" target="_blank" rel="noreferrer">Open Message ↗</a></span></div>` : ""}
        </div>
        <section class="signal-detail-section">
          <h3>Price Lifecycle Chart</h3>
          <div class="signal-chart-meta">
            <span>Time: Tehran</span>
            <span>Candles: ${escapeHtml(signal.chart?.interval || "n/a")}</span>
            <span>Visible points: ${escapeHtml(pointLabel)}</span>
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

  function disposeChart(id) {
    const existing = state.charts.get(id);
    if (!existing) {
      return;
    }
    existing.dispose();
    state.charts.delete(id);
  }

  function resizeChart(id) {
    const existing = state.charts.get(id);
    if (!existing) {
      return;
    }
    existing.resize();
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
    const candleSeries = candles
      .map((item) => buildCandleChartPoint(item))
      .filter(Boolean);
    if (!candleSeries.length) {
      chart.clear();
      return;
    }
    const leverageLabel = formatSignalLeverage(signal);
    const markLines = [];
    if (signal.entry_price_raw || signal.entry_price) {
      markLines.push(markLineItem("Entry", signal.entry_price_raw || signal.entry_price, "#1f5f8b"));
    }
    if (signal.mark_price_raw || signal.mark_price) {
      markLines.push(markLineItem("Mark", signal.mark_price_raw || signal.mark_price, "#0e7c66"));
    }
    const series = [
      {
        name: "Price",
        type: "candlestick",
        data: candleSeries,
        itemStyle: {
          color: "#0e7c66",
          color0: "#d14343",
          borderColor: "#0e7c66",
          borderColor0: "#d14343",
        },
        markLine: markLines.length ? {
          symbol: ["none", "none"],
          label: {
            formatter: (params) => `${params.name}: ${formatDashboardNumber(params.value)}`,
          },
          lineStyle: { width: 1.5, opacity: 0.9 },
          data: markLines,
        } : undefined,
      },
      ...buildLevelHistorySeries(stopLossHistory, candles, "#d14343", "dashed"),
      ...buildLevelHistorySeries(takeProfitHistory, candles, "#b7791f", "solid"),
    ];
    chart.setOption(
      {
        animation: false,
        backgroundColor: "transparent",
        grid: { left: 64, right: 28, top: 54, bottom: 74 },
        legend: { top: 10, textStyle: { color: "#39524b" } },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "cross" },
          backgroundColor: "rgba(17, 31, 28, 0.94)",
          borderWidth: 0,
          textStyle: { color: "#f4fbf8" },
          formatter: (params) => formatSignalChartTooltip(params),
        },
        title: {
          left: 12,
          top: 8,
          text: `${signal.symbol || "Signal"} · ${String(signal.side || "").toUpperCase()} · Lev ${leverageLabel}`,
          textStyle: { fontSize: 12, fontWeight: 700, color: "#39524b" },
        },
        xAxis: {
          type: "time",
          axisLabel: {
            color: "#39524b",
            hideOverlap: true,
            formatter: (value) => formatTehranShortDate(value),
          },
          splitLine: { show: false },
        },
        yAxis: {
          scale: true,
          axisLabel: {
            color: "#39524b",
            formatter: (value) => formatDashboardNumber(value),
          },
          splitLine: { lineStyle: { color: "rgba(57, 82, 75, 0.10)" } },
        },
        dataZoom: [{ type: "inside" }, { type: "slider", height: 24, bottom: 12 }],
        series,
      },
      { notMerge: true, lazyUpdate: true },
    );
  }

  function buildLevelHistorySeries(history, candles, color, styleType) {
    return history.map((item) => {
      const points = buildLevelSpanPoints(item, candles);
      return {
        name: `${item.label} ${item.value_display || item.value}`,
        type: "line",
        symbol: "none",
        connectNulls: false,
        lineStyle: {
          color,
          type: styleType,
          width: item.ended_at ? 2 : 3,
        },
        data: points,
      };
    });
  }

  function buildLevelSpanPoints(item, candles) {
    const startMs = Number(item.started_at_ms || Date.parse(item.started_at || ""));
    const endMs = item.ended_at_ms
      ? Number(item.ended_at_ms)
      : Number((candles[candles.length - 1] || {}).close_timestamp_ms || 0);
    const value = Number(item.value);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || !Number.isFinite(value)) {
      return [];
    }
    const points = [];
    candles.forEach((candle) => {
      const openMs = Number(candle.timestamp_ms || 0);
      const closeMs = Number(candle.close_timestamp_ms || 0);
      if (!Number.isFinite(openMs) || !Number.isFinite(closeMs)) {
        return;
      }
      if (closeMs < startMs || openMs > endMs) {
        return;
      }
      points.push([openMs, value]);
      points.push([closeMs, value]);
    });
    return points;
  }

  function buildCandleChartPoint(item) {
    const timestamp = Number(item.timestamp_ms || Date.parse(item.timestamp || ""));
    const open = Number(item.open);
    const close = Number(item.close);
    const low = Number(item.low);
    const high = Number(item.high);
    if (![timestamp, open, close, low, high].every(Number.isFinite)) {
      return null;
    }
    return [timestamp, open, close, low, high];
  }

  function markLineItem(label, value, color) {
    return {
      name: label,
      yAxis: Number(value),
      lineStyle: {
        color,
        type: label === "Entry" ? "solid" : "dotted",
      },
    };
  }

  function formatSignalChartTooltip(params) {
    const items = Array.isArray(params) ? params : [params];
    const first = items[0];
    const axisValue = first && first.axisValue ? first.axisValue : null;
    const sections = [];
    if (axisValue) {
      sections.push(`<strong>${escapeHtml(formatTehranDate(axisValue))}</strong>`);
    }
    const candle = items.find((item) => item && item.seriesType === "candlestick");
    if (candle && Array.isArray(candle.data)) {
      const [, open, close, low, high] = candle.data;
      sections.push(
        [
          `Open ${formatDashboardNumber(open)}`,
          `High ${formatDashboardNumber(high)}`,
          `Low ${formatDashboardNumber(low)}`,
          `Close ${formatDashboardNumber(close)}`,
        ].join("<br>"),
      );
    }
    items
      .filter((item) => item && item.seriesType !== "candlestick")
      .forEach((item) => {
        const value = Array.isArray(item.data) ? item.data[1] : item.value;
        sections.push(`${escapeHtml(item.seriesName || "Level")}: ${escapeHtml(formatDashboardNumber(value))}`);
      });
    return sections.join("<br><br>");
  }

  function upsertRun(run) {
    if (!belongsToWorkbench(run)) {
      return;
    }
    const runs = Array.isArray(state.recentRuns) ? [...state.recentRuns] : [];
    const index = runs.findIndex((item) => item.run_id === run.run_id);
    if (index >= 0) {
      runs[index] = run;
    } else {
      runs.unshift(run);
    }
    const maxLoadedRuns = Math.max(state.recentRuns.length, state.recentRunsPageSize);
    state.recentRuns = sortRuns(runs).slice(0, maxLoadedRuns);
    state.recentRunsTotal = Math.max(state.recentRunsTotal, state.recentRuns.length);
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
          state.activeRun = mergeRunPayload(state.activeRun, data.run);
          renderRun(state.activeRun);
        }
      }
      if (!response.ok) {
        setFormStatus(`Stop rejected: ${data.reason || data.detail || "run is not stoppable"}`, "warning");
        return;
      }
      rememberActiveRun(data.run.run_id);
      state.activeRun = mergeRunPayload(state.activeRun, data.run);
      renderRun(state.activeRun);
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
      rememberActiveRun(data.run.run_id);
      state.activeRun = mergeRunPayload(state.activeRun, data.run);
      upsertRun(data.run);
      closePanelModal();
      closeModal();
      renderRun(state.activeRun);
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
    const wsUrl = `${protocol}//${window.location.host}${withAuthPath(`/ws/backtests?run_type=${encodeURIComponent(state.runType)}`)}`;
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
      if (state.wsPingTimer) {
        window.clearInterval(state.wsPingTimer);
      }
      state.wsPingTimer = window.setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          try {
            state.ws.send("ping");
          } catch (_error) {
            // no-op
          }
        }
      }, 10000);
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
      if (state.wsPingTimer) {
        window.clearInterval(state.wsPingTimer);
        state.wsPingTimer = null;
      }
      startPolling();
      window.setTimeout(connectWebSocket, 1500);
    };
    state.ws.onerror = () => {
      state.wsReady = false;
      startPolling();
    };
  }

  function handleRealtimeMessage(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.type === "bootstrap") {
      renderReadiness(payload.readiness || {});
      applyRunsResponse({
        runs: payload.runs || [],
        total_runs: Array.isArray(payload.runs) ? payload.runs.length : 0,
        has_more: false,
      });
      if (!state.activeRunId && state.recentRuns.length) {
        rememberActiveRun(state.recentRuns[0].run_id);
        fetchRun();
      } else if (state.activeRunId && !state.activeRun) {
        fetchRun();
      }
      renderFilterBar();
      return;
    }
    if (payload.type !== "backtest_run" || !payload.run) {
      return;
    }
    const run = payload.run;
    if (!belongsToWorkbench(run)) {
      return;
    }
    upsertRun(run);
    renderRecentRuns(state.recentRuns);
    if (!state.activeRunId || state.activeRunId === run.run_id) {
      rememberActiveRun(run.run_id);
      state.activeRun = mergeRunPayload(state.activeRun, run, payload.trace || null);
      renderRun(state.activeRun);
    }
  }

  async function recoverLatestRun() {
    if (state.recoveringLatest) {
      return;
    }
    state.recoveringLatest = true;
    try {
      const response = await fetch(
        withAuthPath(
          `/api/backtests/runs/latest?run_type=${encodeURIComponent(state.runType)}&prefer_active=true`,
        ),
      );
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      if (!payload.run) {
        state.activeRunId = null;
        state.activeRun = null;
        clearStoredRunId(runStorageKey);
        renderEmptyRun();
        return;
      }
      rememberActiveRun(payload.run.run_id);
      state.activeRun = mergeRunPayload(null, payload.run);
      upsertRun(payload.run);
      renderRun(state.activeRun);
    } catch (_error) {
      // The regular list refresh will retry without interrupting the worker.
    } finally {
      state.recoveringLatest = false;
    }
  }

  function belongsToWorkbench(run) {
    if (!run || typeof run !== "object") {
      return false;
    }
    const runType = run.run_type || "portfolio";
    return runType === state.runType;
  }

  function rememberActiveRun(runId, options = {}) {
    state.activeRunId = runId || null;
    if (state.activeRunId) {
      try {
        window.localStorage.setItem(runStorageKey, state.activeRunId);
      } catch (_error) {
        // Storage can be disabled; persisted server state remains authoritative.
      }
    }
    if (options.updateUrl) {
      const url = new URL(window.location.href);
      if (state.activeRunId) {
        url.searchParams.set("run_id", state.activeRunId);
      } else {
        url.searchParams.delete("run_id");
      }
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }

  function readStoredRunId(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  }

  function clearStoredRunId(key) {
    try {
      window.localStorage.removeItem(key);
    } catch (_error) {
      // no-op
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

  function formatElapsedMs(value) {
    const totalSeconds = Math.max(0, Math.floor(Number(value || 0) / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
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

  function formatTehranShortDate(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Tehran",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function formatPercentValue(value) {
    const numeric = Number(value || 0);
    if (!Number.isFinite(numeric)) {
      return String(value || "0");
    }
    return `${stripTrailingZeros((numeric * 100).toFixed(2))}%`;
  }

  function formatSignalLeverage(signal) {
    const declared = signal && signal.declared_leverage ? String(signal.declared_leverage) : "";
    const effective = signal && signal.effective_leverage ? String(signal.effective_leverage) : "";
    if (declared && effective && declared !== effective) {
      return `${declared}× (effective ${effective}×)`;
    }
    if (declared) {
      return `${declared}×`;
    }
    if (effective) {
      return `${effective}×`;
    }
    if (signal && signal.leverage) {
      return `${signal.leverage}×`;
    }
    return "n/a";
  }

  function formatDashboardNumber(value) {
    if (value === null || value === undefined || value === "") {
      return "0";
    }
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return String(value);
    }
    if (numeric === 0) {
      return "0";
    }
    const absolute = Math.abs(numeric);
    if (absolute >= 0.1) {
      return stripTrailingZeros(numeric.toFixed(3));
    }
    const magnitude = Math.floor(Math.log10(absolute));
    const decimalPlaces = Math.max(0, Math.abs(magnitude) + 3);
    return numeric.toFixed(decimalPlaces);
  }

  function stripTrailingZeros(value) {
    return value.includes(".")
      ? value.replace(/\.?0+$/, "")
      : value;
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
                <small>${escapeHtml(run.run_type === "isolated" ? "isolated" : "portfolio")}</small>
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
      ${
        expanded
          ? `<div class="recent-runs-footer">
              <small>${escapeHtml(String(state.recentRunsTotal || runs.length))} runs indexed</small>
              ${
                state.recentRunsHasMore
                  ? `<button type="button" class="ghost-button compact-action" data-load-more-runs="true" ${state.recentRunsLoadingMore ? "disabled" : ""}>
                      ${state.recentRunsLoadingMore ? "Loading..." : "Load Older Runs"}
                    </button>`
                  : `<small>All indexed runs are loaded.</small>`
              }
            </div>`
          : ""
      }
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
