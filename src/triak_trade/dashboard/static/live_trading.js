/* Live trading dashboard: multi-session orchestration and detail views */
(function () {
  "use strict";

  const state = {
    ws: null,
    bootstrap: null,
    overview: null,
    sessions: {},
    recentMessages: [],
    openTrades: [],
    closedTrades: [],
    sessionDetails: {},
    selectedSessionId: null,
    selectedChannel: "",
    savedChannels: [],
    accountInfo: null,
    reconnectDelay: 2000,
    pingInterval: null,
    refreshTimer: null,
  };

  function init() {
    const raw = document.getElementById("live-bootstrap");
    if (raw?.textContent) {
      try {
        state.bootstrap = JSON.parse(raw.textContent);
      } catch (_) {}
    }
    populateStrategies();
    applyDefaults();
    setupForm();
    setupSavedChannels();
    setupModals();
    connectWS();
    fetchOverview();
    fetchAccount();
    setInterval(fetchOverview, 30000);
    setInterval(fetchAccount, 60000);
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/live`;
    try {
      state.ws = new WebSocket(url);
    } catch (_) {
      setTimeout(connectWS, state.reconnectDelay);
      return;
    }
    state.ws.onopen = () => {
      clearInterval(state.pingInterval);
      state.pingInterval = setInterval(() => {
        if (state.ws?.readyState === WebSocket.OPEN) {
          state.ws.send("ping");
          return;
        }
        clearInterval(state.pingInterval);
      }, 10000);
    };
    state.ws.onmessage = (event) => {
      try {
        handleWS(JSON.parse(event.data));
      } catch (_) {}
    };
    state.ws.onclose = () => {
      state.ws = null;
      clearInterval(state.pingInterval);
      setTimeout(connectWS, state.reconnectDelay);
    };
  }

  function handleWS(message) {
    if (message.type === "live_bootstrap") {
      if (message.bootstrap) {
        state.bootstrap = message.bootstrap;
        populateStrategies();
        applyDefaults();
      }
      if (message.overview) {
        applyOverview(message.overview);
      }
      return;
    }
    if (message.type === "live_session" || message.type === "live_trade" || message.type === "live_message") {
      scheduleRefresh(message);
      return;
    }
  }

  function scheduleRefresh(message) {
    if (state.refreshTimer) {
      clearTimeout(state.refreshTimer);
    }
    state.refreshTimer = setTimeout(() => {
      state.refreshTimer = null;
      fetchOverview();
      const sessionId =
        message?.session?.session_id ||
        message?.trade?.session_id ||
        message?.message?.session_id;
      if (sessionId && state.selectedSessionId === sessionId) {
        fetchSessionDetail(sessionId, { silent: true });
      }
    }, 250);
  }

  async function fetchOverview() {
    try {
      const response = await api("/api/live/overview");
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      if (payload.overview) {
        applyOverview(payload.overview);
      }
    } catch (_) {}
  }

  function applyOverview(overview) {
    state.overview = overview;
    state.sessions = {};
    (overview.recent_sessions || []).forEach((session) => {
      state.sessions[session.session_id] = session;
    });
    state.recentMessages = overview.recent_messages || [];
    state.openTrades = overview.open_trades || [];
    state.closedTrades = overview.recent_closed_trades || [];
    renderHero(overview);
    renderSessions(overview.recent_sessions || []);
    renderMessages(state.recentMessages, document.getElementById("lt-messages-list"), "Start a session to display messages here.");
    renderTradeCollection(
      document.getElementById("lt-positions-list"),
      state.openTrades,
      true,
      "No open positions."
    );
    renderTradeCollection(
      document.getElementById("lt-history-list"),
      state.closedTrades,
      false,
      "No closed trades yet."
    );
    setText("lt-positions-count", `${state.openTrades.length} open`);
    setText("lt-msg-count", `${state.recentMessages.length} messages`);
    setText("lt-session-count", `${(overview.recent_sessions || []).length} sessions`);
  }

  function renderHero(overview) {
    const totals = overview?.totals || {};
    setText("lt-running-hl", totals.active_sessions ?? 0);
    setText("lt-pos-hl", totals.open_positions ?? 0);
    setText("lt-msg-hl", totals.messages_processed ?? 0);
    setText("lt-pnl-hl", fmtUSDT(totals.realized_pnl || 0));
  }

  function renderSessions(sessions) {
    const el = document.getElementById("lt-sessions-list");
    if (!el) {
      return;
    }
    if (!sessions.length) {
      el.innerHTML = '<p class="empty-state">No sessions started yet.</p>';
      return;
    }
    el.innerHTML = sessions.map(renderSessionCard).join("");
    el.querySelectorAll("[data-session-open]").forEach((button) => {
      button.addEventListener("click", () => {
        openSessionModal(button.dataset.sessionOpen);
      });
    });
    el.querySelectorAll("[data-session-stop]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        await stopSession(button.dataset.sessionStop);
      });
    });
  }

  function renderSessionCard(session) {
    const status = session.status || "unknown";
    const openCls = session.trading_mode === "live" ? "mode-live" : "mode-demo";
    const channelLabel = session.channel_labels?.[0] || session.channels?.[0] || "—";
    const realizedClass = parseFloat(session.total_realized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    const canStop = status === "running" || status === "starting";
    return `
      <article class="session-card" data-session-open="${esc(session.session_id)}">
        <div class="session-card-head">
          <div>
            <h3>${esc(session.label || channelLabel)}</h3>
            <p class="subtle">${esc(channelLabel)}</p>
          </div>
          <div class="session-card-badges">
            <span class="badge ${openCls}">${session.trading_mode === "live" ? "LIVE" : "DEMO"}</span>
            <span class="phase-pill ${phaseClass(status)}">${esc(statusLabel(status))}</span>
          </div>
        </div>
        <div class="session-card-grid">
          <div><span>Strategy</span><strong>${esc(session.strategy_key)}</strong></div>
          <div><span>Risk</span><strong>${esc(session.risk_per_trade_pct)}%</strong></div>
          <div><span>Open</span><strong>${session.open_positions_count || 0}</strong></div>
          <div><span>Messages</span><strong>${session.total_messages_processed || 0}</strong></div>
          <div><span>Realized PnL</span><strong class="${realizedClass}">${fmtUSDT(session.total_realized_pnl || 0)}</strong></div>
          <div><span>Started</span><strong>${fmtDate(session.started_at)}</strong></div>
        </div>
        <div class="session-card-actions">
          <button type="button" class="btn btn-sm" data-session-open="${esc(session.session_id)}">View Details</button>
          ${canStop ? `<button type="button" class="btn btn-danger btn-sm" data-session-stop="${esc(session.session_id)}">Stop</button>` : ""}
        </div>
      </article>
    `;
  }

  async function openSessionModal(sessionId) {
    state.selectedSessionId = sessionId;
    document.getElementById("lt-session-modal").style.display = "flex";
    setText("lt-session-modal-title", "Loading session details...");
    document.getElementById("lt-session-modal-summary").innerHTML = "";
    document.getElementById("lt-session-modal-messages").innerHTML = '<p class="subtle">Loading…</p>';
    document.getElementById("lt-session-modal-open-trades").innerHTML = '<p class="subtle">Loading…</p>';
    document.getElementById("lt-session-modal-closed-trades").innerHTML = '<p class="subtle">Loading…</p>';
    await fetchSessionDetail(sessionId);
  }

  async function fetchSessionDetail(sessionId, options = {}) {
    try {
      const response = await api(`/api/live/sessions/${encodeURIComponent(sessionId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.detail) {
        if (!options.silent) {
          showError(payload.detail || "Failed to load session details.");
        }
        return;
      }
      state.sessionDetails[sessionId] = payload.detail;
      if (state.selectedSessionId === sessionId) {
        renderSessionDetail(payload.detail);
      }
    } catch (error) {
      if (!options.silent) {
        showError(`Network error: ${error.message}`);
      }
    }
  }

  function renderSessionDetail(detail) {
    const session = detail.session;
    setText("lt-session-modal-title", session.label || session.channel_labels?.[0] || session.session_id);
    const statusEl = document.getElementById("lt-session-modal-status");
    if (statusEl) {
      statusEl.textContent = statusLabel(session.status);
      statusEl.className = `phase-pill ${phaseClass(session.status)}`;
    }
    const stopBtn = document.getElementById("lt-session-modal-stop-btn");
    if (stopBtn) {
      stopBtn.style.display = session.status === "running" || session.status === "starting" ? "" : "none";
      stopBtn.onclick = () => stopSession(session.session_id);
    }
    renderSessionSummary(detail);
    renderMessages(
      detail.messages || [],
      document.getElementById("lt-session-modal-messages"),
      "No messages recorded for this session yet."
    );
    renderTradeCollection(
      document.getElementById("lt-session-modal-open-trades"),
      detail.open_trades || [],
      true,
      "No open positions in this session."
    );
    renderTradeCollection(
      document.getElementById("lt-session-modal-closed-trades"),
      detail.closed_trades || [],
      false,
      "No closed trades in this session."
    );
  }

  function renderSessionSummary(detail) {
    const session = detail.session || {};
    const snapshot = detail.snapshot || {};
    const summary = document.getElementById("lt-session-modal-summary");
    if (!summary) {
      return;
    }
    const balance = session.trading_mode === "demo"
      ? fmtUSDT(session.paper_balance || session.initial_balance || 0)
      : fmtUSDT(session.account_info?.available_balance || session.account_info?.wallet_balance || 0);
    const realizedClass = parseFloat(session.total_realized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    const unrealizedClass = parseFloat(session.total_unrealized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    summary.innerHTML = `
      <div class="metric"><span>Channel</span><strong>${esc(session.channel_labels?.[0] || session.channels?.[0] || "—")}</strong></div>
      <div class="metric"><span>Mode</span><strong>${session.trading_mode === "live" ? "Live" : "Demo"}</strong></div>
      <div class="metric"><span>Balance</span><strong>${balance}</strong></div>
      <div class="metric"><span>Strategy</span><strong>${esc(session.strategy_key || "—")}</strong></div>
      <div class="metric"><span>Risk / Trade</span><strong>${esc(session.risk_per_trade_pct || "0")}%</strong></div>
      <div class="metric"><span>Open Positions</span><strong>${(snapshot.open_trades || detail.open_trades || []).length}</strong></div>
      <div class="metric"><span>Closed Trades</span><strong>${session.closed_trades_count || 0}</strong></div>
      <div class="metric"><span>Realized PnL</span><strong class="${realizedClass}">${fmtUSDT(session.total_realized_pnl || 0)}</strong></div>
      <div class="metric"><span>Unrealized PnL</span><strong class="${unrealizedClass}">${fmtUSDT(session.total_unrealized_pnl || 0)}</strong></div>
      <div class="metric"><span>Messages</span><strong>${session.total_messages_processed || 0}</strong></div>
      <div class="metric"><span>Signals Opened</span><strong>${session.total_signals_opened || 0}</strong></div>
      <div class="metric"><span>Wins / Losses</span><strong>${session.wins || 0} / ${session.losses || 0}</strong></div>
    `;
  }

  function renderMessages(messages, target, emptyText) {
    if (!target) {
      return;
    }
    const items = [...messages].slice(0, 80);
    if (!items.length) {
      target.innerHTML = `<p class="empty-state">${esc(emptyText)}</p>`;
      return;
    }
    target.innerHTML = items.map(renderMessageCard).join("");
  }

  function renderMessageCard(message) {
    const statusClass = messageBadgeClass(message.final_status);
    const side = message.side
      ? `<span class="badge ${message.side === "long" ? "side-long" : "side-short"}">${esc(message.side.toUpperCase())}</span>`
      : "";
    const symbol = message.symbol
      ? `<span class="badge badge-neutral">${esc(message.symbol)}</span>`
      : "";
    const trade = message.trade_id
      ? `<span class="subtle">trade ${esc(message.trade_id.slice(-8))}</span>`
      : "";
    return `
      <article class="msg-card">
        <div class="msg-card-header">
          <span class="badge ${statusClass}">${esc(message.final_status || "processing")}</span>
          ${side}
          ${symbol}
          <span class="subtle">${esc(message.channel_label || message.channel_id || "—")}</span>
          <span class="subtle msg-time">${fmtDate(message.message_date)}</span>
        </div>
        <div class="msg-card-body">
          ${message.preview_text ? `<span class="msg-preview">${esc(message.preview_text.slice(0, 160))}</span>` : ""}
          ${message.effect_summary ? `<span class="subtle effect-summary">${esc(message.effect_summary)}</span>` : ""}
          ${trade}
        </div>
      </article>
    `;
  }

  function renderTradeCollection(target, trades, isOpen, emptyText) {
    if (!target) {
      return;
    }
    if (!trades.length) {
      target.innerHTML = `<p class="empty-state">${esc(emptyText)}</p>`;
      return;
    }
    target.innerHTML = trades.slice(0, 40).map((trade) => tradeCard(trade, isOpen)).join("");
    target.querySelectorAll(".trade-card").forEach((card) => {
      card.addEventListener("click", () => openTradeModal(card.dataset.tradeId, card.dataset.sessionId));
    });
  }

  function tradeCard(trade, isOpen) {
    const pnl = parseFloat(isOpen ? trade.unrealized_pnl || 0 : trade.realized_pnl || 0);
    const pnlClass = pnl >= 0 ? "metric-pos" : "metric-neg";
    const sideClass = trade.side === "long" ? "side-long" : "side-short";
    const channel = trade.channel_label || trade.channel_id || "—";
    const badge = isOpen
      ? '<span class="trade-status-badge open">OPEN</span>'
      : `<span class="trade-status-badge closed">${esc(trade.close_reason || "closed")}</span>`;
    return `
      <article class="trade-card msg-card" data-trade-id="${esc(trade.trade_id)}" data-session-id="${esc(trade.session_id)}">
        <div class="msg-card-header">
          <span class="badge ${sideClass}">${esc((trade.side || "").toUpperCase())} ${esc(trade.symbol || "")}</span>
          ${badge}
          <span class="subtle">${esc(channel)}</span>
        </div>
        <div class="msg-card-body">
          <span>Entry: ${fmtPrice(trade.entry_price)}</span>
          <span>Lev: ${esc(trade.leverage || 1)}x</span>
          <span class="${pnlClass}">${isOpen ? "Unrealized" : "PnL"}: ${fmtUSDT(pnl)}</span>
          ${trade.stop_loss ? `<span class="subtle">SL: ${fmtPrice(trade.stop_loss)}</span>` : ""}
        </div>
        <div class="msg-card-footer subtle">${esc(trade.session_id.slice(-8))} · ${fmtDate(trade.opened_at)}</div>
      </article>
    `;
  }

  function openTradeModal(tradeId, sessionId) {
    const trade =
      findTrade(state.openTrades, tradeId, sessionId) ||
      findTrade(state.closedTrades, tradeId, sessionId) ||
      findTrade(state.sessionDetails[sessionId]?.open_trades || [], tradeId, sessionId) ||
      findTrade(state.sessionDetails[sessionId]?.closed_trades || [], tradeId, sessionId);
    if (!trade) {
      return;
    }
    setText("lt-modal-title", `${(trade.side || "").toUpperCase()} ${trade.symbol || ""}`);
    document.getElementById("lt-modal-content").innerHTML = buildTradeModal(trade);
    document.getElementById("lt-trade-modal").style.display = "flex";
  }

  function buildTradeModal(trade) {
    const history = (trade.message_history || []).map((item) => `
      <tr>
        <td>${fmtDate(item.message_date)}</td>
        <td><span class="badge badge-neutral">${esc(item.action)}</span></td>
        <td>${esc(item.channel_label || item.channel_id)}</td>
        <td class="mono">#${esc(item.message_id)}</td>
        <td class="subtle">${esc((item.message_preview || "").slice(0, 120))}</td>
        <td class="subtle">${esc((item.notes || []).join("; "))}</td>
      </tr>
    `).join("");
    const realized = parseFloat(trade.realized_pnl || 0);
    const unrealized = parseFloat(trade.unrealized_pnl || 0);
    return `
      <div class="modal-metrics">
        <div class="metric"><span>Session</span><strong>${esc(trade.session_id)}</strong></div>
        <div class="metric"><span>Symbol</span><strong>${esc(trade.symbol)}</strong></div>
        <div class="metric"><span>Side</span><strong class="${trade.side === "long" ? "side-long" : "side-short"}">${esc((trade.side || "").toUpperCase())}</strong></div>
        <div class="metric"><span>Leverage</span><strong>${esc(trade.leverage || 1)}x</strong></div>
        <div class="metric"><span>Entry</span><strong>${fmtPrice(trade.entry_price)}</strong></div>
        <div class="metric"><span>Quantity</span><strong>${esc(trade.quantity)}</strong></div>
        <div class="metric"><span>Margin</span><strong>${fmtUSDT(trade.margin || 0)}</strong></div>
        <div class="metric"><span>Stop Loss</span><strong>${trade.stop_loss ? fmtPrice(trade.stop_loss) : "—"}</strong></div>
        <div class="metric"><span>Take Profits</span><strong>${(trade.take_profits || []).map(fmtPrice).join(", ") || "—"}</strong></div>
        <div class="metric"><span>Status</span><strong>${esc(trade.status || "—")}</strong></div>
        <div class="metric"><span>Realized PnL</span><strong class="${realized >= 0 ? "metric-pos" : "metric-neg"}">${fmtUSDT(realized)}</strong></div>
        <div class="metric"><span>Unrealized PnL</span><strong class="${unrealized >= 0 ? "metric-pos" : "metric-neg"}">${fmtUSDT(unrealized)}</strong></div>
        <div class="metric"><span>Fees</span><strong>${fmtUSDT(trade.fees || 0)}</strong></div>
        <div class="metric"><span>Channel</span><strong>${esc(trade.channel_label || trade.channel_id || "—")}</strong></div>
      </div>
      <h4 style="margin-top:1rem">Message Attribution History</h4>
      ${history ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>Date</th><th>Action</th><th>Channel</th><th>Message</th><th>Preview</th><th>Notes</th></tr></thead><tbody>${history}</tbody></table></div>` : "<p class='subtle'>No history.</p>"}
    `;
  }

  function setupForm() {
    document.getElementById("lt-start-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await startSession();
    });
    document.getElementById("lt-mode")?.addEventListener("change", syncBalanceControls);
    document.getElementById("lt-refresh-account-btn")?.addEventListener("click", fetchAccount);
    document.getElementById("lt-channel-save-btn")?.addEventListener("click", async () => {
      const value = document.getElementById("lt-channel-input")?.value?.trim() || "";
      if (!value) {
        showError("Channel is required.");
        return;
      }
      state.selectedChannel = value;
      renderSelectedChannel();
      await saveChannel(value);
    });
  }

  function applyDefaults() {
    if (!state.bootstrap) {
      return;
    }
    setVal("lt-balance", state.bootstrap.default_initial_balance || "100");
    setVal("lt-risk", state.bootstrap.default_risk_per_trade_pct || "120");
    setVal("lt-mode", state.bootstrap.default_trading_mode || "demo");
    const ai = document.getElementById("lt-use-ai");
    if (ai) {
      ai.checked = !!state.bootstrap.use_ai_default;
    }
    if (state.bootstrap.saved_channels?.length) {
      state.savedChannels = state.bootstrap.saved_channels;
    }
    if (!state.selectedChannel && state.bootstrap.default_channels?.length) {
      state.selectedChannel = state.bootstrap.default_channels[0];
    }
    updateReadinessBadge(state.bootstrap.readiness);
    renderSelectedChannel();
    syncBalanceControls();
  }

  function syncBalanceControls() {
    const mode = document.getElementById("lt-mode")?.value || "demo";
    const input = document.getElementById("lt-balance");
    const help = document.getElementById("lt-balance-help");
    const liveMode = mode === "live";
    if (input) {
      input.disabled = liveMode;
      input.readOnly = liveMode;
      input.style.opacity = liveMode ? "0.6" : "";
    }
    if (help) {
      help.textContent = liveMode
        ? "Live mode derives capital from the connected Toobit account balance."
        : "Demo mode uses this value.";
    }
  }

  async function startSession() {
    const channel = state.selectedChannel.trim();
    if (!channel) {
      showError("Exactly one Telegram channel is required for each session.");
      return;
    }
    const startBtn = document.getElementById("lt-start-btn");
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.textContent = "Starting...";
    }
    const tradingMode = document.getElementById("lt-mode")?.value || "demo";
    const payload = {
      channels: [channel],
      trading_mode: tradingMode,
      initial_balance: tradingMode === "demo" ? (document.getElementById("lt-balance")?.value || "100") : null,
      risk_per_trade_pct: document.getElementById("lt-risk")?.value || "120",
      strategy_key: document.getElementById("lt-strategy")?.value || null,
      use_ai: document.getElementById("lt-use-ai")?.checked ?? true,
      label: document.getElementById("lt-label")?.value?.trim() || null,
    };
    try {
      const response = await api("/api/live/sessions/start", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok || !result.started) {
        showError(result.detail || "Failed to start session.");
        return;
      }
      document.getElementById("lt-label").value = "";
      await fetchOverview();
      if (result.session?.session_id) {
        await fetchSessionDetail(result.session.session_id, { silent: true });
      }
    } catch (error) {
      showError(`Network error: ${error.message}`);
    } finally {
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.textContent = "Start Session";
      }
    }
  }

  async function stopSession(sessionId) {
    if (!sessionId) {
      return;
    }
    try {
      const response = await api(`/api/live/sessions/${encodeURIComponent(sessionId)}/stop`, {
        method: "POST",
      });
      const result = await response.json();
      if (!response.ok || !result.stopped) {
        showError(result.detail || "Failed to stop session.");
        return;
      }
      await fetchOverview();
      if (state.selectedSessionId === sessionId) {
        await fetchSessionDetail(sessionId, { silent: true });
      }
    } catch (error) {
      showError(`Network error: ${error.message}`);
    }
  }

  function setupSavedChannels() {
    document.getElementById("lt-show-saved-btn")?.addEventListener("click", async () => {
      const container = document.getElementById("lt-saved-channels");
      const button = document.getElementById("lt-show-saved-btn");
      if (!container || !button) {
        return;
      }
      const shouldShow = container.style.display === "none" || !container.style.display;
      if (shouldShow) {
        await loadSavedChannels();
        container.style.display = "";
        button.textContent = "Hide saved channels ▴";
      } else {
        container.style.display = "none";
        button.textContent = "Choose from saved channels ▾";
      }
    });
  }

  async function loadSavedChannels() {
    try {
      const response = await api("/api/live/channels");
      const payload = await response.json();
      state.savedChannels = payload.channels || [];
    } catch (_) {}
    renderSavedChannels();
  }

  function renderSelectedChannel() {
    const target = document.getElementById("lt-selected-channel");
    if (!target) {
      return;
    }
    if (!state.selectedChannel) {
      target.innerHTML = '<p class="subtle">No channel selected.</p>';
      return;
    }
    target.innerHTML = `
      <span class="channel-tag">
        ${esc(state.selectedChannel)}
        <button type="button" class="tag-remove" id="lt-selected-channel-remove">×</button>
      </span>
    `;
    document.getElementById("lt-selected-channel-remove")?.addEventListener("click", () => {
      state.selectedChannel = "";
      renderSelectedChannel();
    });
  }

  function renderSavedChannels() {
    const target = document.getElementById("lt-saved-channels");
    if (!target) {
      return;
    }
    if (!state.savedChannels.length) {
      target.innerHTML = '<p class="subtle">No saved channels.</p>';
      return;
    }
    target.innerHTML = `
      ${state.savedChannels.map((item) => `
        <div class="saved-ch-row">
          <span>${esc(item.label || item.channel_resolved || item.channel_input)}</span>
          <button type="button" class="btn btn-sm" data-saved-channel="${esc(item.channel_input || item.channel_resolved)}">Use</button>
          <button type="button" class="btn btn-sm btn-danger" data-remove-channel="${esc(item.channel_resolved || item.channel_input)}">Delete</button>
        </div>
      `).join("")}
    `;
    target.querySelectorAll("[data-saved-channel]").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedChannel = button.dataset.savedChannel || "";
        renderSelectedChannel();
      });
    });
    target.querySelectorAll("[data-remove-channel]").forEach((button) => {
      button.addEventListener("click", async () => {
        await deleteSavedChannel(button.dataset.removeChannel);
      });
    });
  }

  async function saveChannel(channel) {
    try {
      const response = await api("/api/live/channels", {
        method: "POST",
        body: JSON.stringify({ channel }),
      });
      const payload = await response.json();
      if (!response.ok) {
        showError(payload.detail || "Failed to save channel.");
        return;
      }
      state.savedChannels = payload.channels || [];
      renderSavedChannels();
    } catch (error) {
      showError(`Network error: ${error.message}`);
    }
  }

  async function deleteSavedChannel(channel) {
    try {
      const response = await api("/api/live/channels", {
        method: "DELETE",
        body: JSON.stringify({ channel }),
      });
      const payload = await response.json();
      if (!response.ok) {
        showError(payload.detail || "Failed to delete channel.");
        return;
      }
      state.savedChannels = payload.channels || [];
      renderSavedChannels();
    } catch (error) {
      showError(`Network error: ${error.message}`);
    }
  }

  async function fetchAccount() {
    const target = document.getElementById("lt-account-content");
    if (target) {
      target.innerHTML = '<p class="subtle">Fetching account information...</p>';
    }
    try {
      const response = await api("/api/live/account");
      const payload = await response.json();
      setText("lt-account-ts", `Updated ${fmtDate(new Date().toISOString())}`);
      if (!response.ok || !payload.success) {
        if (target) {
          target.innerHTML = `<p class="error-note">${esc(payload.error || "Unable to fetch account information.")}</p>`;
        }
        return;
      }
      state.accountInfo = payload;
      renderAccountInfo(payload);
    } catch (error) {
      if (target) {
        target.innerHTML = `<p class="error-note">Connection error: ${esc(error.message)}</p>`;
      }
    }
  }

  function renderAccountInfo(payload) {
    const target = document.getElementById("lt-account-content");
    if (!target) {
      return;
    }
    const futures = payload.futures || {};
    const spot = payload.spot || {};
    target.innerHTML = `
      <div class="account-section">
        <p class="eyebrow">Overview</p>
        <div class="metrics backtest-metrics">
          <div class="metric"><span>User ID</span><strong>${esc(payload.user_id || "—")}</strong></div>
          <div class="metric"><span>Key Type</span><strong>${esc(payload.api_key_type || "—")}</strong></div>
        </div>
      </div>
      <div class="account-section">
        <p class="eyebrow">Futures</p>
        <div class="metrics backtest-metrics">
          <div class="metric"><span>Wallet Balance</span><strong>${fmtUSDT(futures.wallet_balance || 0)}</strong></div>
          <div class="metric"><span>Available Balance</span><strong>${fmtUSDT(futures.available_balance || 0)}</strong></div>
          <div class="metric"><span>Unrealized PnL</span><strong class="${parseFloat(futures.unrealized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg"}">${fmtUSDT(futures.unrealized_pnl || 0)}</strong></div>
          <div class="metric"><span>Position Margin</span><strong>${fmtUSDT(futures.position_margin || 0)}</strong></div>
          <div class="metric"><span>Today PnL</span><strong class="${parseFloat(futures.day_profit || 0) >= 0 ? "metric-pos" : "metric-neg"}">${fmtUSDT(futures.day_profit || 0)}</strong></div>
          <div class="metric"><span>Day PnL Rate</span><strong>${parseFloat(futures.day_profit_rate || 0).toFixed(4)}%</strong></div>
        </div>
      </div>
      <div class="account-section">
        <p class="eyebrow">Spot USDT</p>
        <div class="metrics backtest-metrics">
          <div class="metric"><span>Total</span><strong>${fmtUSDT(spot.total || 0)}</strong></div>
          <div class="metric"><span>Free</span><strong>${fmtUSDT(spot.free || 0)}</strong></div>
          <div class="metric"><span>Locked</span><strong>${fmtUSDT(spot.locked || 0)}</strong></div>
        </div>
      </div>
    `;
  }

  function populateStrategies() {
    const select = document.getElementById("lt-strategy");
    if (!select || !state.bootstrap?.available_strategies) {
      return;
    }
    select.innerHTML = "";
    state.bootstrap.available_strategies.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.key;
      option.textContent = item.name + (item.active ? " (default)" : "");
      if (item.key === state.bootstrap.default_strategy_key) {
        option.selected = true;
      }
      select.appendChild(option);
    });
  }

  function updateReadinessBadge(readiness) {
    const badge = document.getElementById("lt-readiness-badge");
    const issues = document.getElementById("lt-readiness-issues");
    if (badge) {
      badge.innerHTML = readiness?.ready
        ? '<span class="badge badge-green">Ready</span>'
        : '<span class="badge badge-red">Not Ready</span>';
    }
    if (!issues) {
      return;
    }
    const list = readiness?.issues || [];
    if (list.length) {
      issues.innerHTML = list.map((item) => `<p class="issue-item">${esc(item)}</p>`).join("");
      issues.style.display = "";
      return;
    }
    issues.style.display = "none";
    issues.innerHTML = "";
  }

  function setupModals() {
    document.getElementById("lt-modal-close")?.addEventListener("click", closeTradeModal);
    document.getElementById("lt-session-modal-close")?.addEventListener("click", closeSessionModal);
    document.getElementById("lt-trade-modal")?.addEventListener("click", (event) => {
      if (event.target.id === "lt-trade-modal") {
        closeTradeModal();
      }
    });
    document.getElementById("lt-session-modal")?.addEventListener("click", (event) => {
      if (event.target.id === "lt-session-modal") {
        closeSessionModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeTradeModal();
        closeSessionModal();
      }
    });
  }

  function closeTradeModal() {
    document.getElementById("lt-trade-modal").style.display = "none";
  }

  function closeSessionModal() {
    document.getElementById("lt-session-modal").style.display = "none";
    state.selectedSessionId = null;
  }

  function findTrade(trades, tradeId, sessionId) {
    return trades.find((item) => item.trade_id === tradeId && item.session_id === sessionId);
  }

  function messageBadgeClass(status) {
    const mapping = {
      ignored: "badge-gray",
      pending_consolidation: "badge-yellow",
      signal_updated: "badge-blue",
      opened_trade: "badge-green",
      updated_trade: "badge-blue",
      closed_trade: "badge-red",
      partial_close: "badge-orange",
      updated_sl: "badge-blue",
      updated_tp: "badge-blue",
      invalid: "badge-red",
      no_match: "badge-gray",
      no_open_trade: "badge-gray",
      processing: "badge-yellow",
    };
    return mapping[status] || "badge-gray";
  }

  function statusLabel(status) {
    const mapping = {
      starting: "Starting",
      running: "Running",
      stopping: "Stopping",
      stopped: "Stopped",
      error: "Error",
    };
    return mapping[status] || status || "Unknown";
  }

  function phaseClass(status) {
    if (status === "running") {
      return "phase-running";
    }
    if (status === "error") {
      return "phase-failed";
    }
    return "phase-queued";
  }

  function fmtUSDT(value) {
    const number = parseFloat(value || 0);
    const sign = number < 0 ? "−" : "";
    return `${sign}$${Math.abs(number).toFixed(2)}`;
  }

  function fmtPrice(value) {
    if (value == null || value === "") {
      return "—";
    }
    const number = parseFloat(value);
    if (Number.isNaN(number)) {
      return esc(value);
    }
    if (number >= 1000) {
      return number.toLocaleString("en-US", { maximumFractionDigits: 2 });
    }
    if (number >= 1) {
      return number.toFixed(4);
    }
    return number.toFixed(6);
  }

  function fmtDate(value) {
    if (!value) {
      return "—";
    }
    try {
      return new Date(value).toLocaleString("en-US", {
        timeZone: "Asia/Tehran",
        hour12: false,
      });
    } catch (_) {
      return String(value).slice(0, 19);
    }
  }

  function showError(message) {
    const target = document.getElementById("lt-readiness-issues");
    if (target) {
      target.innerHTML = `<p class="issue-item error-note">${esc(message)}</p>`;
      target.style.display = "";
      setTimeout(() => {
        if (target) {
          target.style.display = "none";
        }
      }, 6000);
    }
    console.error("[live_trading]", message);
  }

  function setVal(id, value) {
    const el = document.getElementById(id);
    if (el) {
      el.value = value;
    }
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = value;
    }
  }

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(url, { ...options, headers });
  }

  const style = document.createElement("style");
  style.textContent = `
    .side-long { color: #22c55e; }
    .side-short { color: #ef4444; }
    .mode-live { background:#ef444422; color:#ef4444; }
    .mode-demo { background:#eab30822; color:#eab308; }
    .metric-pos { color: #22c55e; }
    .metric-neg { color: #ef4444; }
    .badge-green { background:#22c55e22; color:#22c55e; }
    .badge-red { background:#ef444422; color:#ef4444; }
    .badge-yellow { background:#eab30822; color:#eab308; }
    .badge-blue { background:#3b82f622; color:#3b82f6; }
    .badge-gray { background:#94a3b822; color:#94a3b8; }
    .badge-orange { background:#f9731622; color:#f97316; }
    .badge-neutral { background:#1e293b; color:#94a3b8; }
    .modal-metrics { display:flex; flex-wrap:wrap; gap:.6rem; }
    .modal-metrics .metric { min-width:140px; }
    .table-scroll { overflow-x:auto; margin-top:.8rem; }
    .data-table { width:100%; border-collapse:collapse; font-size:.82rem; }
    .data-table th, .data-table td { padding:.4rem .6rem; border-bottom:1px solid #1e293b; text-align:left; }
    .data-table th { color:#94a3b8; font-weight:500; }
    .mono { font-family:monospace; font-size:.8rem; }
    .form-row-2 { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
    .lt-action-row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
    .error-note { color:#ef4444; }
    .msg-stream { max-height:420px; overflow-y:auto; display:flex; flex-direction:column; gap:.5rem; }
    .modal-stream { max-height:260px; }
    .msg-preview { display:block; font-size:.82rem; color:#cbd5e1; }
    .effect-summary { display:block; font-size:.78rem; }
    .msg-time { font-size:.72rem; }
    .msg-card-footer { font-size:.75rem; margin-top:.2rem; }
    .channel-tag-row { display:flex; flex-wrap:wrap; gap:.4rem; margin-top:.5rem; }
    .channel-tag { background:#1e293b; border-radius:4px; padding:3px 8px; display:flex; align-items:center; gap:.3rem; font-size:.82rem; }
    .tag-remove { background:none; border:none; color:#ef4444; cursor:pointer; padding:0; line-height:1; }
    .channel-input-row { display:flex; gap:.5rem; }
    .channel-input-row input { flex:1; }
    .btn-link { background:none; border:none; color:#3b82f6; cursor:pointer; font-size:.82rem; padding:0; }
    .saved-channel-list { background:#0f172a; border-radius:4px; padding:.6rem; margin-top:.5rem; }
    .saved-ch-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:.5rem; align-items:center; padding:.2rem 0; }
    .saved-ch-row span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .session-card-list { display:flex; flex-direction:column; gap:.75rem; }
    .session-card { border:1px solid #1e293b; border-radius:8px; padding:.9rem; background:#0f172a; cursor:pointer; }
    .session-card-head { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; margin-bottom:.75rem; }
    .session-card-head h3 { margin:0 0 .2rem 0; font-size:1rem; }
    .session-card-badges { display:flex; gap:.4rem; flex-wrap:wrap; align-items:center; }
    .session-card-grid { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:.65rem; }
    .session-card-grid span { display:block; font-size:.74rem; color:#94a3b8; }
    .session-card-grid strong { display:block; margin-top:.12rem; font-size:.86rem; }
    .session-card-actions { display:flex; gap:.5rem; margin-top:.85rem; }
    .trade-status-badge { font-size:.72rem; padding:2px 6px; border-radius:3px; font-weight:600; }
    .trade-status-badge.open { background:#22c55e22; color:#22c55e; }
    .trade-status-badge.closed { background:#94a3b822; color:#94a3b8; }
    .lt-session-detail-grid { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:1rem; margin-top:1rem; }
    .lt-session-detail-grid h4 { margin:0 0 .6rem 0; }
    .lt-modal-actions { display:flex; gap:.6rem; align-items:center; }
    @media (max-width: 900px) {
      .form-row-2, .session-card-grid, .lt-session-detail-grid { grid-template-columns:1fr; }
      .session-card-head { flex-direction:column; }
    }
  `;
  document.head.appendChild(style);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
