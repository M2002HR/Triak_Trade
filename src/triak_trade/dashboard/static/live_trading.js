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
      "No closed trades yet.",
      { allowDelete: true }
    );
    renderSessionSummaryStrip(overview.recent_sessions || []);
    renderMessageSummaryStrip(state.recentMessages);
    renderOpenTradeSummaryStrip(state.openTrades);
    renderClosedTradeSummaryStrip(state.closedTrades);
    setText("lt-positions-count", `${state.openTrades.length} open`);
    setText("lt-msg-count", `${state.recentMessages.length} messages`);
    setText("lt-session-count", `${(overview.recent_sessions || []).length} sessions`);
    setText("lt-history-count", `${state.closedTrades.length} closed`);
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

  function renderSessionSummaryStrip(sessions) {
    const running = sessions.filter((session) => session.status === "running").length;
    const demo = sessions.filter((session) => session.trading_mode === "demo").length;
    const live = sessions.filter((session) => session.trading_mode === "live").length;
    const attention = sessions.filter((session) => (session.last_error || "").trim() || session.status === "error").length;
    setText("lt-session-running-count", running);
    setText("lt-session-demo-count", demo);
    setText("lt-session-live-count", live);
    setText("lt-session-attention-count", attention);
  }

  function renderSessionCard(session) {
    const status = session.status || "unknown";
    const openCls = session.trading_mode === "live" ? "mode-live" : "mode-demo";
    const statusTone = session.status === "running"
      ? "session-state-running"
      : session.status === "error"
        ? "session-state-error"
        : "session-state-idle";
    const channelLabel = session.channel_labels?.[0] || session.channels?.[0] || "—";
    const realizedClass = parseFloat(session.total_realized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    const unrealizedClass = parseFloat(session.total_unrealized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    const canStop = status === "running" || status === "starting";
    const attention = (session.last_error || "").trim();
    const messages = session.total_messages_processed || 0;
    const openPositions = session.open_positions_count || 0;
    const identity = esc(session.label || "Session");
    const startedAt = fmtDate(session.started_at);
    return `
      <article class="session-card session-card-rich ${statusTone}" data-session-open="${esc(session.session_id)}">
        <div class="session-card-head">
          <div class="session-card-title">
            <h3>${identity}</h3>
            <div class="session-card-subline">
              <span class="session-card-handle">${esc(channelLabel)}</span>
              <span class="session-card-id">ID ${esc(session.session_id.slice(-8))}</span>
            </div>
          </div>
          <div class="session-card-badges">
            <span class="badge ${openCls}">${session.trading_mode === "live" ? "LIVE" : "DEMO"}</span>
            <span class="phase-pill ${phaseClass(status)}">${esc(statusLabel(status))}</span>
          </div>
        </div>
        <div class="session-card-ribbon">
          <span class="mini-badge">${esc(session.strategy_key)}</span>
          <span class="mini-badge">Risk ${esc(session.risk_per_trade_pct)}%</span>
          <span class="mini-badge">W/L ${session.wins || 0}/${session.losses || 0}</span>
        </div>
        <div class="session-card-grid session-card-grid-compact">
          <div class="session-metric-tile"><span>Open</span><strong>${openPositions}</strong></div>
          <div class="session-metric-tile"><span>Messages</span><strong>${messages}</strong></div>
          <div class="session-metric-tile"><span>Closed</span><strong>${session.closed_trades_count || 0}</strong></div>
          <div class="session-metric-tile"><span>Signals</span><strong>${session.total_signals_opened || 0}</strong></div>
          <div class="session-metric-tile pnl-tile"><span>Realized PnL</span><strong class="${realizedClass}">${fmtUSDT(session.total_realized_pnl || 0)}</strong></div>
          <div class="session-metric-tile pnl-tile"><span>Unrealized PnL</span><strong class="${unrealizedClass}">${fmtUSDT(session.total_unrealized_pnl || 0)}</strong></div>
        </div>
        <div class="session-card-foot">
          <span class="session-foot-item"><strong>Started</strong>${startedAt}</span>
          <span class="session-foot-item"><strong>Mode</strong>${session.trading_mode === "live" ? "Live Execution" : "Demo Execution"}</span>
        </div>
        ${attention ? `<p class="session-card-alert">${esc(attention)}</p>` : ""}
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
    document.getElementById("lt-session-modal-signals").innerHTML = '<p class="subtle">Loading…</p>';
    document.getElementById("lt-session-modal-exchange").innerHTML = '<p class="subtle">Loading…</p>';
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
    const deleteBtn = document.getElementById("lt-session-modal-delete-btn");
    if (deleteBtn) {
      deleteBtn.onclick = () => deleteSessionHistory(session.session_id);
    }
    renderSessionSummary(detail);
    renderMessages(
      detail.messages || [],
      document.getElementById("lt-session-modal-messages"),
      "No messages recorded for this session yet.",
      { allowDelete: true }
    );
    renderTradeCollection(
      document.getElementById("lt-session-modal-open-trades"),
      detail.open_trades || [],
      true,
      "No open positions in this session.",
      { allowDelete: false }
    );
    renderTradeCollection(
      document.getElementById("lt-session-modal-closed-trades"),
      detail.closed_trades || [],
      false,
      "No closed trades in this session.",
      { allowDelete: true }
    );
    renderSignalCollection(document.getElementById("lt-session-modal-signals"), detail.signals || []);
    renderExchangeSnapshot(
      document.getElementById("lt-session-modal-exchange"),
      session.exchange_snapshot || detail.snapshot?.session?.exchange_snapshot || null
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
      ? fmtUSDT(session.account_info?.available_balance || session.account_info?.wallet_balance || session.paper_balance || 0)
      : fmtUSDT(session.account_info?.available_balance || session.account_info?.wallet_balance || 0);
    const realizedClass = parseFloat(session.total_realized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    const unrealizedClass = parseFloat(session.total_unrealized_pnl || 0) >= 0 ? "metric-pos" : "metric-neg";
    summary.innerHTML = `
      <div class="metric"><span>Channel</span><strong>${esc(session.channel_labels?.[0] || session.channels?.[0] || "—")}</strong></div>
      <div class="metric"><span>Mode</span><strong>${session.trading_mode === "live" ? "Live" : "Demo"}</strong></div>
      <div class="metric"><span>Balance</span><strong>${balance}</strong></div>
      <div class="metric"><span>Strategy</span><strong>${esc(session.strategy_key || "—")}</strong></div>
      <div class="metric"><span>Last Error</span><strong>${esc(session.last_error || "—")}</strong></div>
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

  function renderMessages(messages, target, emptyText, options = {}) {
    if (!target) {
      return;
    }
    const items = [...messages].slice(0, 80);
    if (!items.length) {
      target.innerHTML = `<p class="empty-state">${esc(emptyText)}</p>`;
      return;
    }
    target.innerHTML = items.map((message) => renderMessageCard(message, options)).join("");
    if (options.allowDelete) {
      target.querySelectorAll("[data-delete-message]").forEach((button) => {
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          await deleteMessageRecord(
            button.dataset.sessionId,
            button.dataset.messageId,
            button.dataset.channelId
          );
        });
      });
    }
  }

  function renderMessageSummaryStrip(messages) {
    let opened = 0;
    let updated = 0;
    let closed = 0;
    let ignored = 0;
    messages.forEach((message) => {
      const bucket = messageOutcomeGroup(message.final_status);
      if (bucket === "opened") {
        opened += 1;
      } else if (bucket === "updated") {
        updated += 1;
      } else if (bucket === "closed") {
        closed += 1;
      } else {
        ignored += 1;
      }
    });
    setText("lt-msg-opened-count", opened);
    setText("lt-msg-updated-count", updated);
    setText("lt-msg-closed-count", closed);
    setText("lt-msg-ignored-count", ignored);
  }

  function renderOpenTradeSummaryStrip(trades) {
    let longCount = 0;
    let shortCount = 0;
    let syncedCount = 0;
    let unsyncedCount = 0;
    trades.forEach((trade) => {
      if ((trade.side || "").toLowerCase() === "long") {
        longCount += 1;
      } else if ((trade.side || "").toLowerCase() === "short") {
        shortCount += 1;
      }
      if (trade.exchange_position) {
        syncedCount += 1;
      } else {
        unsyncedCount += 1;
      }
    });
    setText("lt-pos-long-count", longCount);
    setText("lt-pos-short-count", shortCount);
    setText("lt-pos-synced-count", syncedCount);
    setText("lt-pos-unsynced-count", unsyncedCount);
  }

  function renderMessageCard(message, options = {}) {
    const statusClass = messageBadgeClass(message.final_status);
    const outcomeGroup = messageOutcomeGroup(message.final_status);
    const side = message.side
      ? `<span class="badge ${message.side === "long" ? "side-long" : "side-short"}">${esc(message.side.toUpperCase())}</span>`
      : "";
    const symbol = message.symbol
      ? `<span class="badge badge-neutral">${esc(message.symbol)}</span>`
      : "";
    const trade = message.trade_id
      ? `<span class="subtle">trade ${esc(message.trade_id.slice(-8))}</span>`
      : "";
    const impactNotes = Array.isArray(message.impact_notes) && message.impact_notes.length
      ? `<span class="subtle effect-summary">${esc(message.impact_notes.join(" · "))}</span>`
      : "";
    const correlation = message.correlation_method
      ? `<span class="subtle">link ${esc(message.correlation_method)}${message.correlation_note ? ` · ${esc(message.correlation_note)}` : ""}</span>`
      : "";
    const deleteButton = options.allowDelete
      ? `<button type="button" class="btn btn-sm" data-delete-message="1" data-session-id="${esc(message.session_id)}" data-message-id="${esc(message.message_id)}" data-channel-id="${esc(message.channel_id)}">Delete</button>`
      : "";
    return `
      <article class="msg-card msg-card-rich msg-card-${outcomeGroup}">
        <div class="msg-card-header">
          <div class="msg-card-topic">
            <span class="badge ${statusClass}">${esc(messageStatusLabel(message.final_status || "processing"))}</span>
            ${side}
            ${symbol}
          </div>
          <div class="msg-card-meta">
            <span class="subtle">${esc(message.channel_label || message.channel_id || "—")}</span>
            <span class="subtle msg-time">${fmtDate(message.message_date)}</span>
            ${deleteButton}
          </div>
        </div>
        <div class="msg-card-body">
          ${message.preview_text ? `<p class="msg-preview">${esc(message.preview_text.slice(0, 180))}</p>` : ""}
          <div class="msg-card-insight-row">
            ${message.effect_summary ? `<span class="effect-summary">${esc(message.effect_summary)}</span>` : '<span class="effect-summary">No effect summary recorded.</span>'}
            ${trade}
          </div>
          ${(impactNotes || correlation) ? `<div class="msg-card-note-row">${impactNotes}${correlation}</div>` : ""}
        </div>
      </article>
    `;
  }

  function renderTradeCollection(target, trades, isOpen, emptyText, options = {}) {
    if (!target) {
      return;
    }
    if (!trades.length) {
      target.innerHTML = `<p class="empty-state">${esc(emptyText)}</p>`;
      return;
    }
    target.innerHTML = trades.slice(0, 40).map((trade) => tradeCard(trade, isOpen, options)).join("");
    target.querySelectorAll(".trade-card").forEach((card) => {
      card.addEventListener("click", (event) => {
        if (event.target.closest("[data-delete-trade]")) {
          return;
        }
        openTradeModal(card.dataset.tradeId, card.dataset.sessionId);
      });
    });
    if (options.allowDelete) {
      target.querySelectorAll("[data-delete-trade]").forEach((button) => {
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          await deleteTradeRecord(button.dataset.sessionId, button.dataset.tradeId);
        });
      });
    }
  }

  function renderClosedTradeSummaryStrip(trades) {
    let winners = 0;
    let losers = 0;
    let flat = 0;
    trades.forEach((trade) => {
      const pnl = parseFloat(trade.realized_pnl || 0);
      if (pnl > 0) {
        winners += 1;
      } else if (pnl < 0) {
        losers += 1;
      } else {
        flat += 1;
      }
    });
    setText("lt-history-win-count", winners);
    setText("lt-history-loss-count", losers);
    setText("lt-history-flat-count", flat);
  }

  function tradeCard(trade, isOpen, options = {}) {
    const pnl = parseFloat(isOpen ? trade.unrealized_pnl || 0 : trade.realized_pnl || 0);
    const pnlClass = pnl >= 0 ? "metric-pos" : "metric-neg";
    const sideClass = trade.side === "long" ? "side-long" : "side-short";
    const channel = trade.channel_label || trade.channel_id || "—";
    const badge = isOpen
      ? '<span class="trade-status-badge open">OPEN</span>'
      : `<span class="trade-status-badge closed">${esc(closeReasonLabel(trade.close_reason || "closed"))}</span>`;
    const exchangePill = trade.exchange_position
      ? '<span class="badge badge-blue">API synced</span>'
      : '<span class="badge badge-gray">Pending sync</span>';
    const deleteButton = options.allowDelete
      ? `<button type="button" class="btn btn-sm" data-delete-trade="1" data-session-id="${esc(trade.session_id)}" data-trade-id="${esc(trade.trade_id)}">Delete</button>`
      : "";
    const statusTone = isOpen ? "trade-card-open" : pnl >= 0 ? "trade-card-win" : pnl < 0 ? "trade-card-loss" : "trade-card-flat";
    return `
      <article class="trade-card msg-card trade-card-rich ${statusTone}" data-trade-id="${esc(trade.trade_id)}" data-session-id="${esc(trade.session_id)}">
        <div class="msg-card-header">
          <div class="msg-card-topic">
            <span class="badge ${sideClass}">${esc((trade.side || "").toUpperCase())} ${esc(trade.symbol || "")}</span>
            ${badge}
            ${exchangePill}
          </div>
          <div class="msg-card-meta">
            <span class="subtle">${esc(channel)}</span>
            <span class="subtle">${fmtDate(isOpen ? trade.opened_at : trade.closed_at || trade.updated_at || trade.opened_at)}</span>
            ${deleteButton}
          </div>
        </div>
        <div class="msg-card-body">
          <div class="trade-kpi-grid">
            <span><strong>Entry</strong>${fmtPrice(trade.entry_price)}</span>
            <span><strong>Leverage</strong>${esc(trade.leverage || 1)}x</span>
            <span><strong>${isOpen ? "Qty" : "Closed Qty"}</strong>${esc(trade.quantity)}</span>
            ${trade.stop_loss ? `<span><strong>SL</strong>${fmtPrice(trade.stop_loss)}</span>` : ""}
            ${!isOpen && trade.exit_price ? `<span><strong>Exit</strong>${fmtPrice(trade.exit_price)}</span>` : ""}
          </div>
          ${trade.exchange_symbol ? `<span class="subtle">API symbol: ${esc(trade.exchange_symbol)}</span>` : ""}
          <span class="trade-pnl-line ${pnlClass}">${isOpen ? "Unrealized" : "Realized PnL"}: ${fmtUSDT(pnl)}</span>
          ${trade.last_exchange_sync_error ? `<span class="error-note">Exchange error: ${esc(trade.last_exchange_sync_error)}</span>` : ""}
        </div>
        <div class="msg-card-footer subtle">${esc(trade.session_id.slice(-8))} · ${closeReasonSummary(trade, isOpen)}</div>
      </article>
    `;
  }

  function renderSignalCollection(target, signals) {
    if (!target) {
      return;
    }
    if (!signals.length) {
      target.innerHTML = '<p class="empty-state">No signal state recorded yet.</p>';
      return;
    }
    target.innerHTML = signals.slice(0, 60).map((signal) => {
      const latestNote = Array.isArray(signal.notes) && signal.notes.length
        ? signal.notes[signal.notes.length - 1]
        : "";
      const sideBadge = signal.side
        ? `<span class="badge ${signal.side === "long" ? "side-long" : "side-short"}">${esc(signal.side.toUpperCase())}</span>`
        : "";
      return `
        <article class="msg-card">
          <div class="msg-card-header">
            <span class="badge ${signal.status_group === "active" ? "badge-green" : "badge-gray"}">${esc(signal.status || "unknown")}</span>
            ${sideBadge}
            ${signal.symbol ? `<span class="badge badge-neutral">${esc(signal.symbol)}</span>` : ""}
            ${signal.exchange_symbol ? `<span class="subtle">API ${esc(signal.exchange_symbol)}</span>` : ""}
            <span class="subtle">signal ${esc(signal.signal_id.slice(-8))}</span>
          </div>
          <div class="msg-card-body">
            <span>SL: ${signal.stop_loss ? fmtPrice(signal.stop_loss) : "—"}</span>
            <span>TPs: ${(signal.take_profits || []).map(fmtPrice).join(", ") || "—"}</span>
            <span>Lev: ${esc(signal.leverage || "—")}x</span>
            <span>Msgs: ${esc(signal.message_count || 0)}</span>
            ${signal.trade_id ? `<span class="subtle">trade ${esc(signal.trade_id.slice(-8))}</span>` : ""}
            ${latestNote ? `<span class="subtle effect-summary">${esc(latestNote)}</span>` : ""}
          </div>
          <div class="msg-card-footer subtle">${fmtDate(signal.updated_at)}</div>
        </article>
      `;
    }).join("");
  }

  function renderExchangeSnapshot(target, snapshot) {
    if (!target) {
      return;
    }
    if (!snapshot) {
      target.innerHTML = '<p class="empty-state">No exchange snapshot has been synced yet.</p>';
      return;
    }
    const positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
    const orders = Array.isArray(snapshot.recent_orders) ? snapshot.recent_orders : [];
    const error = snapshot.error ? `<p class="error-note">${esc(snapshot.error)}</p>` : "";
    target.innerHTML = `
      <article class="msg-card">
        <div class="msg-card-header">
          <span class="badge badge-neutral">Sync</span>
          <span class="subtle">${fmtDate(snapshot.fetched_at)}</span>
        </div>
        <div class="msg-card-body">
          <span>Open Positions: ${positions.length}</span>
          <span>Recent Orders: ${orders.length}</span>
        </div>
      </article>
      ${error}
      ${positions.map((position) => `
        <article class="msg-card">
          <div class="msg-card-header">
            <span class="badge ${position.side === "LONG" ? "side-long" : "side-short"}">${esc(position.side)}</span>
            <span class="badge badge-neutral">${esc(position.symbol)}</span>
            ${position.exchange_symbol ? `<span class="subtle">API ${esc(position.exchange_symbol)}</span>` : ""}
          </div>
          <div class="msg-card-body">
            <span>Qty: ${esc(position.quantity)}</span>
            <span>Avg: ${fmtPrice(position.avg_price)}</span>
            <span>Mark: ${fmtPrice(position.mark_price)}</span>
            <span>uPnL: ${fmtUSDT(position.unrealized_pnl || 0)}</span>
          </div>
        </article>
      `).join("")}
      ${orders.slice(0, 12).map((order) => `
        <article class="msg-card">
          <div class="msg-card-header">
            <span class="badge badge-blue">${esc(order.status || "unknown")}</span>
            <span class="badge badge-neutral">${esc(order.symbol)}</span>
            ${order.exchange_symbol ? `<span class="subtle">API ${esc(order.exchange_symbol)}</span>` : ""}
            <span class="subtle">${esc(order.side)}</span>
          </div>
          <div class="msg-card-body">
            <span>Executed: ${esc(order.executed_qty || 0)} / ${esc(order.orig_qty || 0)}</span>
            <span>Avg: ${fmtPrice(order.avg_price)}</span>
            <span>Lev: ${esc(order.leverage || 1)}x</span>
          </div>
        </article>
      `).join("")}
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
    const exchangeOrders = (trade.exchange_order_history || []).map((item) => `
      <tr>
        <td class="mono">${esc(item.order_id || "—")}</td>
        <td>${esc(item.side || "—")}</td>
        <td>${esc(item.status || "—")}</td>
        <td>${esc(item.executed_qty || "0")} / ${esc(item.orig_qty || "0")}</td>
        <td>${fmtPrice(item.avg_price)}</td>
      </tr>
    `).join("");
    const realized = parseFloat(trade.realized_pnl || 0);
    const unrealized = parseFloat(trade.unrealized_pnl || 0);
    return `
      <div class="modal-metrics">
        <div class="metric"><span>Session</span><strong>${esc(trade.session_id)}</strong></div>
        <div class="metric"><span>Symbol</span><strong>${esc(trade.symbol)}</strong></div>
        <div class="metric"><span>API Symbol</span><strong>${esc(trade.exchange_symbol || "—")}</strong></div>
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
        <div class="metric"><span>Exchange Sync</span><strong>${trade.exchange_position ? "Yes" : "No"}</strong></div>
        <div class="metric"><span>Last Exchange Error</span><strong>${esc(trade.last_exchange_sync_error || "—")}</strong></div>
      </div>
      ${trade.exchange_position ? `
        <h4 style="margin-top:1rem">Exchange Position Snapshot</h4>
        <div class="modal-metrics">
          <div class="metric"><span>Exchange Side</span><strong>${esc(trade.exchange_position.side || "—")}</strong></div>
          <div class="metric"><span>Exchange Qty</span><strong>${esc(trade.exchange_position.quantity || "0")}</strong></div>
          <div class="metric"><span>Exchange Avg</span><strong>${fmtPrice(trade.exchange_position.avg_price)}</strong></div>
          <div class="metric"><span>Exchange Mark</span><strong>${fmtPrice(trade.exchange_position.mark_price)}</strong></div>
          <div class="metric"><span>Exchange uPnL</span><strong>${fmtUSDT(trade.exchange_position.unrealized_pnl || 0)}</strong></div>
        </div>
      ` : ""}
      <h4 style="margin-top:1rem">Message Attribution History</h4>
      ${history ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>Date</th><th>Action</th><th>Channel</th><th>Message</th><th>Preview</th><th>Notes</th></tr></thead><tbody>${history}</tbody></table></div>` : "<p class='subtle'>No history.</p>"}
      <h4 style="margin-top:1rem">Exchange Order History</h4>
      ${exchangeOrders ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>Order</th><th>Side</th><th>Status</th><th>Executed</th><th>Avg Price</th></tr></thead><tbody>${exchangeOrders}</tbody></table></div>` : "<p class='subtle'>No exchange orders synced for this trade.</p>"}
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
    setVal("lt-risk", state.bootstrap.default_risk_per_trade_pct || "120");
    setVal("lt-mode", state.bootstrap.default_trading_mode || "demo");
    syncModeOptions();
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

  function syncModeOptions() {
    const select = document.getElementById("lt-mode");
    if (!select) {
      return;
    }
    const liveOption = [...select.options].find((item) => item.value === "live");
    const enabled = !!state.bootstrap?.live_mode_enabled;
    if (liveOption) {
      liveOption.disabled = !enabled;
      liveOption.textContent = enabled
        ? "Live (Real Toobit Account)"
        : "Live (Enable Env Flag)";
    }
    if (!enabled && select.value === "live") {
      select.value = "demo";
    }
  }

  function syncBalanceControls() {
    const mode = document.getElementById("lt-mode")?.value || "demo";
    const help = document.getElementById("lt-balance-help");
    const liveMode = mode === "live";
    const liveEnabled = !!state.bootstrap?.live_mode_enabled;
    if (help) {
      help.textContent = liveMode
        ? "Live mode derives capital, sizing, and PnL from the connected Toobit account."
        : !liveEnabled
          ? "Demo mode also uses the connected Toobit demo account balance. Enable LIVE_TRADING_LIVE_MODE_ENABLED in root .env.local to unlock live sessions."
        : "Demo mode also uses the connected Toobit demo account balance.";
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

  async function deleteSessionHistory(sessionId) {
    if (!sessionId || !confirm("Delete this session and its stored history from the dashboard?")) {
      return;
    }
    try {
      const response = await api(`/api/live/sessions/${encodeURIComponent(sessionId)}`, {
        method: "DELETE",
      });
      const result = await response.json();
      if (!response.ok || !result.deleted) {
        showError(result.detail || "Failed to delete session history.");
        return;
      }
      closeSessionModal();
      await fetchOverview();
    } catch (error) {
      showError(`Network error: ${error.message}`);
    }
  }

  async function deleteTradeRecord(sessionId, tradeId) {
    if (!sessionId || !tradeId || !confirm("Delete this trade record from dashboard history?")) {
      return;
    }
    try {
      const response = await api(
        `/api/live/sessions/${encodeURIComponent(sessionId)}/trades/${encodeURIComponent(tradeId)}`,
        { method: "DELETE" }
      );
      const result = await response.json();
      if (!response.ok || !result.deleted) {
        showError(result.detail || "Failed to delete trade record.");
        return;
      }
      await fetchOverview();
      if (state.selectedSessionId === sessionId) {
        await fetchSessionDetail(sessionId, { silent: true });
      }
      closeTradeModal();
    } catch (error) {
      showError(`Network error: ${error.message}`);
    }
  }

  async function deleteMessageRecord(sessionId, messageId, channelId) {
    if (!sessionId || !messageId || !channelId || !confirm("Delete this message record from dashboard history?")) {
      return;
    }
    try {
      const query = new URLSearchParams({ channel_id: channelId });
      const response = await api(
        `/api/live/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}?${query.toString()}`,
        { method: "DELETE" }
      );
      const result = await response.json();
      if (!response.ok || !result.deleted) {
        showError(result.detail || "Failed to delete message record.");
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
      updated_leverage: "badge-blue",
      cancelled_trade: "badge-red",
      invalid: "badge-red",
      invalid_symbol: "badge-red",
      invalid_sizing: "badge-red",
      no_match: "badge-gray",
      no_open_trade: "badge-gray",
      processing: "badge-yellow",
    };
    return mapping[status] || "badge-gray";
  }

  function messageOutcomeGroup(status) {
    if (status === "opened_trade") {
      return "opened";
    }
    if (["updated_trade", "updated_sl", "updated_tp", "updated_leverage", "signal_updated", "partial_close"].includes(status)) {
      return "updated";
    }
    if (["closed_trade", "cancelled_trade"].includes(status)) {
      return "closed";
    }
    return "ignored";
  }

  function messageStatusLabel(status) {
    const mapping = {
      ignored: "Ignored",
      pending_consolidation: "Pending",
      signal_updated: "Signal Updated",
      opened_trade: "Opened Trade",
      updated_trade: "Updated Trade",
      closed_trade: "Closed Trade",
      partial_close: "Partial Close",
      updated_sl: "Stop Updated",
      updated_tp: "Target Updated",
      updated_leverage: "Leverage Updated",
      cancelled_trade: "Cancelled",
      invalid: "Invalid",
      invalid_symbol: "Bad Symbol",
      invalid_sizing: "Bad Sizing",
      no_match: "No Match",
      no_open_trade: "No Open Trade",
      processing: "Processing",
    };
    return mapping[status] || (status || "Processing");
  }

  function closeReasonLabel(reason) {
    const mapping = {
      sl_hit: "Stop Hit",
      tp_hit: "Target Hit",
      manual_close: "Manual Close",
      cancelled: "Cancelled",
      closed: "Closed",
    };
    return mapping[reason] || String(reason || "closed").split("_").join(" ");
  }

  function closeReasonSummary(trade, isOpen) {
    if (isOpen) {
      return `opened ${fmtDate(trade.opened_at)}`;
    }
    const reason = closeReasonLabel(trade.close_reason || "closed");
    const closedAt = fmtDate(trade.closed_at || trade.updated_at || trade.opened_at);
    return `${reason} · ${closedAt}`;
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
