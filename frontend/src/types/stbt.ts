// Types for the STBT (Sell Today Buy Tomorrow) tab — mirrors blueprints/stbt.py
// and the status JSON written by strategies/stbt/engine.py (strategy_type
// 'stbt') and strategies/stbt/btst_engine.py (strategy_type 'btst').

export type StbtStrategyType = 'stbt' | 'btst'

export interface StbtParams {
  underlying: string
  strategy_type?: StbtStrategyType
  // STBT (short strangle) params
  entry_drop_pct?: number
  sl_pct?: number
  max_reentries?: number
  reentry_method?: 'CANDLE_CLOSE' | 'LTP'
  allow_day2_reentry?: boolean
  hedge_target_premium?: number
  take_profit_pct?: number
  entry_time?: string
  hedge_time?: string
  // BTST (paper-short flip long) params
  moneyness?: number
  drop_pct?: number
  vsl_pct?: number
  real_sl_pct?: number
  vix_max?: number
  dte_min?: number
  dte_max?: number
  entry_weekdays?: string[]
  ref_time?: string
  entry_start_time?: string
  entry_end_time?: string
  // shared
  lot_multiplier?: number
  max_loss?: number
  telegram_alerts?: boolean
  ws_close_time?: string
  day2_open_time?: string
  force_exit_time?: string
}

export interface StbtConfig {
  strategy_id: string
  name: string
  strategy_type?: StbtStrategyType
  underlying: string
  params: StbtParams
  is_running: boolean
  is_scheduled: boolean
  is_error: boolean
  error_message?: string | null
  manually_stopped: boolean
  schedule_start?: string
  schedule_stop?: string
  schedule_days: string[]
  created_at?: string
  last_started?: string
  last_stopped?: string
}

export type StbtLegState =
  | 'WATCHING'
  | 'IN_SHORT'
  | 'SL_HIT'
  | 'PAPER_SHORT'
  | 'IN_LONG'
  | 'DONE'

export interface StbtLeg {
  symbol: string
  opt_type: 'CE' | 'PE'
  quantity: number
  state: StbtLegState
  ref_premium: number
  entry_price: number
  sl_price: number
  reentries?: number
  // BTST flip legs: the virtual paper-short levels
  v_entry?: number
  v_sl?: number
  realized_pnl: number
  charges_total: number
  ltp?: number
  mtm_pnl?: number
}

export interface StbtHedge {
  symbol: string
  opt_type: string
  quantity: number
  state: 'OPEN' | 'DONE'
  buy_price: number
  realized_pnl: number
  charges_total: number
  ltp?: number
  mtm_pnl?: number
}

export interface StbtLiveStatus {
  version: string
  strategy_id: string
  underlying: string
  phase: string
  message?: string
  trade_date: string
  expiry: string
  quantity: number
  main_legs: StbtLeg[]
  hedge: StbtHedge | null
  gross_pnl: number
  charges: number
  net_pnl: number
  mtm_pnl?: number
  total_net_pnl?: number
  max_loss?: number
  last_update: string
}

export interface StbtStatusResponse {
  status: string
  is_running: boolean
  is_error: boolean
  error_message?: string | null
  live: StbtLiveStatus | null
}

export interface StbtHistoryLeg {
  symbol: string
  opt_type: string
  state: string
  cycles: number
  realized_pnl: number
  charges: number
}

export interface StbtHistoryRecord {
  trade_date: string
  ended_at: string
  final_phase: string
  expiry: string
  quantity: number
  legs: StbtHistoryLeg[]
  hedge: StbtHistoryLeg & { buy_price?: number } | null
  gross_pnl: number
  charges: number
  net_pnl: number
}

export interface StbtHistoryResponse {
  status: string
  records: StbtHistoryRecord[]
  total_net: number
}

export interface StbtDailyPnl {
  date: string
  gross: number
  charges: number
  net: number
  cycles: number
  wins: number
  losses: number
}

export interface StbtCurvePoint {
  date: string
  cumulative_net: number
  cumulative_gross: number
}

export interface StbtAnalyticsTotals {
  gross: number
  charges: number
  net: number
  cycles: number
  trading_days: number
  win_days: number
  loss_days: number
  best_day: StbtDailyPnl | null
  worst_day: StbtDailyPnl | null
}

export interface StbtAnalyticsConfig {
  strategy_id: string
  name: string
  underlying: string
  strategy_type?: StbtStrategyType
}

export interface StbtAnalyticsResponse {
  status: string
  daily: StbtDailyPnl[]
  curve: StbtCurvePoint[]
  totals: StbtAnalyticsTotals
  configs: StbtAnalyticsConfig[]
}

export interface StbtPanicResponse {
  status: string // "success" | "partial" | "error"
  closed: string[]
  already_flat: string[]
  failed: string[]
  message: string
}

export const SUPPORTED_UNDERLYINGS = [
  'SENSEX',
  'BANKEX',
  'NIFTY',
  'BANKNIFTY',
  'FINNIFTY',
  'MIDCPNIFTY',
] as const

export const SCHEDULE_DAY_OPTIONS = [
  { value: 'mon', label: 'Mon' },
  { value: 'tue', label: 'Tue' },
  { value: 'wed', label: 'Wed' },
  { value: 'thu', label: 'Thu' },
  { value: 'fri', label: 'Fri' },
] as const

export const PHASE_LABELS: Record<string, string> = {
  STARTING: 'Starting',
  DAY1_WAIT: 'Day 1 — waiting for entry window',
  DAY1_LIVE: 'Day 1 — live monitoring',
  HEDGED: 'Day 1 — hedged overnight',
  DAY1_DONE: 'Day 1 complete — carry overnight',
  DAY2: 'Day 2 — exit session',
  EXPIRY_DAY: 'Expiry day — no new positions',
  NO_ENTRY: 'Filters block entries today',
  DONE: 'Session finished',
  KILLED: 'KILLED — max loss hit',
  TARGET_HIT: 'Target hit — booked out flat',
  ERROR: 'Engine error — check logs',
}

export const LEG_STATE_STYLES: Record<StbtLegState, string> = {
  WATCHING: 'bg-blue-500/15 text-blue-600 dark:text-blue-400',
  IN_SHORT: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  SL_HIT: 'bg-red-500/15 text-red-600 dark:text-red-400',
  PAPER_SHORT: 'bg-purple-500/15 text-purple-600 dark:text-purple-400',
  IN_LONG: 'bg-green-500/15 text-green-600 dark:text-green-400',
  DONE: 'bg-muted text-muted-foreground',
}

export const STRATEGY_TYPE_LABELS: Record<StbtStrategyType, string> = {
  stbt: 'STBT short',
  btst: 'BTST flip',
}

export const ENTRY_WEEKDAY_OPTIONS = [
  { value: 'mon', label: 'Mon' },
  { value: 'tue', label: 'Tue' },
  { value: 'wed', label: 'Wed' },
  { value: 'thu', label: 'Thu' },
  { value: 'fri', label: 'Fri' },
] as const
