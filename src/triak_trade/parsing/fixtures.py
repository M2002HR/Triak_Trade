"""Parser fixture messages for deterministic tests."""

from __future__ import annotations

PARSER_FIXTURES: list[dict[str, str]] = [
    {
        "name": "valid_long_one_line",
        "text": "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
    },
    {
        "name": "valid_short_one_line",
        "text": "BTCUSDT SHORT Entry: 68200 - 68400 SL: 69000 TP: 67600 / 67000 Leverage: 3x",
    },
    {
        "name": "valid_multiline_long",
        "text": (
            "BTCUSDT LONG\nEntry: 68000 - 68200\nSL: 67400\n"
            "TP1: 69000\nTP2: 70000\nLeverage: 5x"
        ),
    },
    {
        "name": "valid_multiline_short",
        "text": (
            "ETHUSDT SHORT\nEntry: 3800 - 3820\nSL: 3880\n"
            "TP1: 3740\nTP2: 3700\nLeverage: 4x"
        ),
    },
    {
        "name": "persian_digits",
        "text": "BTCUSDT LONG Entry: \u06f6\u06f8\u06f0\u06f0\u06f0 - "
        "\u06f6\u06f8\u06f2\u06f0\u06f0 SL: \u06f6\u06f7\u06f4\u06f0\u06f0 "
        "TP: \u06f6\u06f9\u06f0\u06f0\u06f0 / \u06f7\u06f0\u06f0\u06f0\u06f0",
    },
    {
        "name": "arabic_digits",
        "text": "BTCUSDT LONG Entry: \u0666\u0668\u0660\u0660\u0660 - "
        "\u0666\u0668\u0662\u0660\u0660 SL: \u0666\u0667\u0664\u0660\u0660 "
        "TP: \u0666\u0669\u0660\u0660\u0660 / \u0667\u0660\u0660\u0660\u0660",
    },
    {"name": "missing_sl", "text": "BTCUSDT LONG Entry: 68000 - 68200 TP: 69000 / 70000"},
    {"name": "missing_tp", "text": "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400"},
    {
        "name": "multiple_tps",
        "text": "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 Targets: 69000, 70000, 71500",
    },
    {"name": "entry_range", "text": "BTC LONG Entry 68000-68200 SL:67400 TP:69000"},
    {"name": "market_entry", "text": "BTCUSDT LONG MARKET SL: 67400 TP: 69000"},
    {"name": "hashtag_symbol", "text": "#BTC LONG Entry: 68000 SL: 67400 TP: 69000"},
    {"name": "slash_symbol", "text": "BTC/USDT SHORT Entry: 68000 SL: 69000 TP: 67000"},
    {"name": "dash_symbol", "text": "BTC-USDT LONG Entry: 68000 SL: 67400 TP: 69000"},
    {"name": "lowercase", "text": "btcusdt long entry 68000 sl 67400 tp 69000"},
    {"name": "leverage_5x", "text": "BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000 Lev 5"},
    {
        "name": "leverage_too_high",
        "text": "BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000 Leverage: 50x",
    },
    {"name": "cancel_signal", "text": "cancel BTC signal"},
    {"name": "close_signal", "text": "close BTC"},
    {"name": "close_partial", "text": "close 50% BTC"},
    {"name": "move_sl_entry", "text": "move SL to entry"},
    {"name": "move_sl_breakeven", "text": "move stop to breakeven"},
    {"name": "update_leverage", "text": "update leverage to 3x"},
    {"name": "update_tp", "text": "TP updated to 70500"},
    {"name": "profit_report", "text": "TP1 hit ✅"},
    {"name": "result_report", "text": "+120% profit"},
    {"name": "advertisement", "text": "Join VIP now! best signals guaranteed"},
    {"name": "giveaway", "text": "Giveaway promo code, subscribe now"},
    {"name": "market_analysis", "text": "General market analysis for BTC trend this week"},
    {"name": "news_message", "text": "Breaking news: CPI release impacts crypto"},
    {"name": "ambiguous_good", "text": "BTC looking good"},
    {"name": "dont_enter", "text": "Don't enter now"},
    {"name": "wait_confirmation", "text": "Wait for confirmation"},
    {"name": "entry_updated", "text": "Entry updated"},
    {"name": "sl_hit", "text": "SL hit"},
    {"name": "target_reached", "text": "Target reached"},
    {"name": "spot_buy", "text": "Spot buy BTCUSDT Entry: 68000 TP: 69000"},
    {"name": "spot_sell", "text": "Spot sell ETHUSDT Entry: 3800 TP: 3700"},
    {"name": "emoji_noise", "text": "🚀 BTCUSDT LONG 😎 Entry: 68000 SL:67400 TP:69000"},
    {
        "name": "persian_terms",
        "text": "BTCUSDT \u0644\u0627\u0646\u06af \u0648\u0631\u0648\u062f 68000 "
        "\u062d\u062f \u0636\u0631\u0631 67400 \u062a\u0627\u0631\u06af\u062a 69000 "
        "\u0627\u0647\u0631\u0645 5",
    },
]
