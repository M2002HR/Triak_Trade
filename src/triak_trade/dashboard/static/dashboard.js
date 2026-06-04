document.documentElement.dataset.dashboardReady = "true";

(() => {
  const bootstrapNode = document.getElementById("backtest-bootstrap");
  if (!bootstrapNode) {
    return;
  }

  const bootstrap = JSON.parse(bootstrapNode.textContent || "{}");
  const state = {
    bootstrap,
    activeRunId: bootstrap.recent_runs?.[0]?.run_id || null,
    activeRun: bootstrap.recent_runs?.[0] || null,
    recentRuns: bootstrap.recent_runs || [],
    selectedMessageId: null,
    modalOpen: false,
    panelModalOpen: false,
    activePanelModal: null,
    messageFilter: "all",
    pollTimer: null,
    listTimer: null,
    ws: null,
    wsReady: false,
  };

  const nodes = {
    form: document.getElementById("backtest-live-form"),
    channel: document.getElementById("backtest-channel"),
    fromDate: document.getElementById("backtest-from-date"),
    toDate: document.getElementById("backtest-to-date"),
    startMessageLink: document.getElementById("backtest-start-message-link"),
    interval: document.getElementById("backtest-interval"),
    maxMessages: document.getElementById("backtest-max-messages"),
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
    nodes.useAi.checked = Boolean(bootstrap.default_use_ai);
    nodes.sendLogChannel.checked = Boolean(bootstrap.default_send_log_channel);
    nodes.logPerMessage.checked = Boolean(bootstrap.default_log_per_message);
    applyDateRange(bootstrap.default_from_date, bootstrap.default_to_date);
  }

  function bindEvents() {
    nodes.form?.addEventListener("submit", handleSubmit);
    document.querySelectorAll("[data-preset-hours]").forEach((button) => {
      button.addEventListener("click", () => {
        const hours = Number(button.getAttribute("data-preset-hours") || "24");
        const end = new Date();
        const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
        applyDateRange(start.toISOString(), end.toISOString());
      });
    });
    nodes.messageFilterBar?.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-message-filter]") : null;
      if (!target) {
        return;
      }
      state.messageFilter = target.getAttribute("data-message-filter") || "all";
      renderFilterBar();
      renderMessages(state.activeRun?.messages || []);
    });
    nodes.messageStream?.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-message-id]") : null;
      if (!target) {
        return;
      }
      const messageId = Number(target.getAttribute("data-message-id") || "0");
      state.selectedMessageId = messageId;
      const trace = state.activeRun?.messages?.find((item) => item.message_id === messageId);
      if (trace) {
        openModal(trace);
      }
    });
    document.addEventListener("click", (event) => {
      const panelTarget = event.target instanceof Element ? event.target.closest("[data-open-panel-modal]") : null;
      if (panelTarget) {
        const kind = panelTarget.getAttribute("data-open-panel-modal") || "feed";
        openPanelModal(kind);
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

  function renderFilterBar() {
    nodes.messageFilterBar?.querySelectorAll("[data-message-filter]").forEach((button) => {
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
    } else {
      nodes.panelModalTitle.textContent = "Run Feed";
      nodes.panelModalBody.innerHTML = buildEventFeedMarkup(state.activeRun?.events || [], true);
    }
    syncBodyModalState();
  }

  function closePanelModal() {
    if (!nodes.panelModal) {
      return;
    }
    state.panelModalOpen = false;
    state.activePanelModal = null;
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
      const response = await fetch("/api/backtests/start", {
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
      const response = await fetch(`/api/backtests/runs/${state.activeRunId}`);
      if (!response.ok) {
        return;
      }
      const run = await response.json();
      state.activeRun = run;
      upsertRun(run);
      renderRun(run);
      if (run.status !== "running" && run.status !== "queued" && state.pollTimer) {
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
      const response = await fetch("/api/backtests/runs?limit=8");
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
    renderRecentRuns(state.recentRuns);
    if (state.modalOpen && state.selectedMessageId) {
      const trace = run.messages?.find((item) => item.message_id === state.selectedMessageId);
      if (trace) {
        openModal(trace);
      }
    }
  }

  function renderCurrentRunHeader(run) {
    nodes.activeRunHeadline.textContent = run.status === "running" ? "Streaming" : run.current_phase_label;
    nodes.runTitle.textContent = `${run.channel_resolved} • ${run.interval}`;
    const startMessageSuffix = run.start_message_id
      ? ` • from message ${run.start_message_id}`
      : "";
    nodes.runSubtitle.textContent = `${formatDate(run.from_date)} → ${formatDate(run.to_date)}${startMessageSuffix}`;
    nodes.runPhasePill.textContent = run.current_phase_label;
    nodes.runPhasePill.className = `phase-pill phase-${run.status}`;
    nodes.currentPhaseLabel.textContent = run.current_phase_label;
    nodes.currentPhaseSummary.textContent = run.current_phase_summary || "No summary yet.";
    const currentTrace = run.messages?.find((item) => item.message_id === run.current_message_id);
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
      ["Open Positions", run.live_open_positions],
      ["Closed Trades", run.live_closed_trades],
      ["Wins / Losses", `${run.live_wins} / ${run.live_losses}`],
      ["Live PnL", run.live_total_pnl],
      ["Realized PnL", run.live_realized_pnl],
      ["Unrealized PnL", run.live_unrealized_pnl],
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
        <strong>${escapeHtml(latest.phase.replaceAll("_", " "))}</strong>
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
        const active = state.activeRun?.current_message_id === trace.message_id;
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
    nodes.metrics.innerHTML = "";
    nodes.currentPhaseLabel.textContent = "Queued";
    nodes.currentPhaseSummary.textContent = "Waiting to start.";
    nodes.currentMessageLabel.textContent = "None";
    nodes.currentMessageSummary.textContent = "No message is being processed yet.";
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
          </article>
        `;
      })
      .join("");
    nodes.modalPreview.textContent = trace.full_text || trace.preview_text || "(empty text message)";
    nodes.modalSummary.innerHTML = `
      <div class="summary-row"><strong>Final Status</strong><span>${escapeHtml(trace.final_status)}</span></div>
      <div class="summary-row"><strong>Current Stage</strong><span>${escapeHtml(trace.current_stage)}</span></div>
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

  function connectWebSocket() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/backtests`;
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

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
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
                <strong>${escapeHtml(event.phase.replaceAll("_", " "))}</strong>
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
            <button type="button" class="recent-run-card ${state.activeRunId === run.run_id ? "active" : ""}" data-run-id="${escapeHtml(run.run_id)}">
              <strong>${escapeHtml(run.channel_input || run.channel_resolved)}</strong>
              <span>${escapeHtml(run.current_phase_label || run.current_phase || run.status)}</span>
              <small>${formatDate(run.created_at)}</small>
            </button>
          `)
          .join("")}
      </div>
    `;
  }
})();
