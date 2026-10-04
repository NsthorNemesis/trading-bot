# Multi-Agent Algorithmic Trading System

An automated Forex trading research platform built with Python. Four asynchronous agents monitor markets, detect signals, manage risk, and audit execution — with live control via Telegram and a web dashboard.

> **Status:** Research phase — paper trading only. No real capital until the system validates 6–12 months against backtests.

## Architecture

- **MarketAgent** — market data ingestion (OANDA REST API), maintains M15/H4 candle data
- **SignalAgent** — pattern detection across 6 strategies with regime-aware filters (trend vs. reversal), session filters, and cooldowns
- **RiskExecutionAgent** — position sizing, stop-loss / take-profit, break-even logic, daily drawdown guard
- **AuditAgent** — trade logging and error notifications
- **Webapp** — dashboard with charts, admin panel, log viewer, shadow-mode results
- **Telegram bot** — remote control (`/pausar`, `/reanudar`, `/shadow`, live parameter updates)

## Key features

- Hot-reload strategy parameters (no restarts needed)
- Shadow mode: paused strategies keep paper-trading so their edge can be evaluated later
- Backtesting harness with a single engine that mirrors live logic
- Drawdown protection and per-strategy risk controls

## Strategies

| Strategy | Type | Status |
|---|---|---|
| EMA_Crossover | Trend | Active (paper) |
| Engulfing | Trend | Research |
| Engulfing + RSI Divergence | Trend | Research |
| Hammer | Reversal | Archived |
| Doji | Reversal | Archived |
| RSI + Bollinger | Reversal | Archived |

## Research methodology

Every strategy goes through a staged funnel before going live: multi-pair screening → filter ablation → session analysis → 5-year regime test → multi-timeframe validation. Quality over volume — a strategy must prove its edge before it trades.

## Tech stack

Python 3.12 · OANDA v20 API · asyncio · systemd · Linux VPS · Telegram Bot API

## Disclaimer

Personal research project. Past backtest performance does not guarantee future results. Not financial advice.
