import {
  AlertTriangle,
  Calendar,
  ChevronDown,
  ChevronUp,
  Clock,
  History,
  Moon,
  MoreVertical,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Square,
  Trash2,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { stbtApi, type StbtConfigPayload } from '@/api/stbt'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import StbtAnalytics from '@/components/stbt/StbtAnalytics'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  ENTRY_WEEKDAY_OPTIONS,
  LEG_STATE_STYLES,
  PHASE_LABELS,
  SCHEDULE_DAY_OPTIONS,
  type StbtConfig,
  type StbtHistoryRecord,
  type StbtLeg,
  type StbtStatusResponse,
  type StbtStrategyType,
  STRATEGY_TYPE_LABELS,
  SUPPORTED_UNDERLYINGS,
} from '@/types/stbt'
import { showToast } from '@/utils/toast'

interface FormState {
  name: string
  strategy_type: StbtStrategyType
  underlying: string
  // STBT short-strangle fields
  entry_drop_pct: string
  sl_pct: string
  max_reentries: string
  hedge_target_premium: string
  take_profit_pct: string
  reentry_method: 'CANDLE_CLOSE' | 'LTP'
  allow_day2_reentry: boolean
  entry_time: string
  hedge_time: string
  // BTST flip fields
  moneyness: string
  drop_pct: string
  vsl_pct: string
  real_sl_pct: string
  be_trigger_pct: string
  vix_max: string
  trigger_mode: 'TICK' | 'CANDLE_CLOSE'
  candle_source: 'WS' | 'HISTORY'
  dte_min: string
  dte_max: string
  entry_weekdays: string[]
  ref_time: string
  entry_start_time: string
  entry_end_time: string
  // shared
  lot_multiplier: string
  max_loss: string
  telegram_alerts: boolean
  ws_close_time: string
  day2_open_time: string
  force_exit_time: string
  schedule_start: string
  schedule_stop: string
  schedule_days: string[]
}

const DEFAULT_FORM: FormState = {
  name: '',
  strategy_type: 'stbt',
  underlying: 'SENSEX',
  entry_drop_pct: '5',
  sl_pct: '20',
  max_reentries: '1',
  hedge_target_premium: '20',
  take_profit_pct: '0',
  reentry_method: 'CANDLE_CLOSE',
  allow_day2_reentry: true,
  entry_time: '11:00',
  hedge_time: '15:26',
  moneyness: '2',
  drop_pct: '5',
  vsl_pct: '20',
  real_sl_pct: '30',
  be_trigger_pct: '30',
  vix_max: '18',
  trigger_mode: 'TICK',
  candle_source: 'WS',
  dte_min: '1',
  dte_max: '3',
  entry_weekdays: ['mon', 'tue', 'wed', 'thu'],
  ref_time: '11:00',
  entry_start_time: '11:01',
  entry_end_time: '14:59',
  lot_multiplier: '1',
  max_loss: '0',
  telegram_alerts: true,
  ws_close_time: '15:29',
  day2_open_time: '09:16',
  force_exit_time: '10:30',
  schedule_start: '09:10',
  schedule_stop: '16:00',
  schedule_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
}

function formFromConfig(config: StbtConfig): FormState {
  const p = config.params || {}
  return {
    name: config.name,
    strategy_type: config.strategy_type === 'btst' ? 'btst' : 'stbt',
    underlying: config.underlying,
    entry_drop_pct: String(p.entry_drop_pct ?? 5),
    sl_pct: String(p.sl_pct ?? 20),
    max_reentries: String(p.max_reentries ?? 1),
    hedge_target_premium: String(p.hedge_target_premium ?? 20),
    take_profit_pct: String(p.take_profit_pct ?? 0),
    reentry_method: p.reentry_method === 'LTP' ? 'LTP' : 'CANDLE_CLOSE',
    allow_day2_reentry: p.allow_day2_reentry ?? true,
    entry_time: p.entry_time ?? '11:00',
    hedge_time: p.hedge_time ?? '15:26',
    moneyness: String(p.moneyness ?? 2),
    drop_pct: String(p.drop_pct ?? 5),
    vsl_pct: String(p.vsl_pct ?? 20),
    real_sl_pct: String(p.real_sl_pct ?? 30),
    be_trigger_pct: String(p.be_trigger_pct ?? 30),
    vix_max: String(p.vix_max ?? 18),
    trigger_mode: p.trigger_mode === 'CANDLE_CLOSE' ? 'CANDLE_CLOSE' : 'TICK',
    candle_source: p.candle_source === 'HISTORY' ? 'HISTORY' : 'WS',
    dte_min: String(p.dte_min ?? 1),
    dte_max: String(p.dte_max ?? 3),
    entry_weekdays: p.entry_weekdays?.length ? p.entry_weekdays : ['mon', 'tue', 'wed', 'thu'],
    ref_time: p.ref_time ?? '11:00',
    entry_start_time: p.entry_start_time ?? '11:01',
    entry_end_time: p.entry_end_time ?? '14:59',
    lot_multiplier: String(p.lot_multiplier ?? 1),
    max_loss: String(p.max_loss ?? 0),
    telegram_alerts: p.telegram_alerts ?? true,
    ws_close_time: p.ws_close_time ?? '15:29',
    day2_open_time: p.day2_open_time ?? '09:16',
    force_exit_time: p.force_exit_time ?? '10:30',
    schedule_start: config.schedule_start ?? '09:10',
    schedule_stop: config.schedule_stop ?? '16:00',
    schedule_days: config.schedule_days?.length
      ? config.schedule_days
      : ['mon', 'tue', 'wed', 'thu', 'fri'],
  }
}

function payloadFromForm(form: FormState): StbtConfigPayload {
  const shared = {
    underlying: form.underlying,
    strategy_type: form.strategy_type,
    lot_multiplier: Number(form.lot_multiplier),
    max_loss: Number(form.max_loss),
    telegram_alerts: form.telegram_alerts,
    ws_close_time: form.ws_close_time,
    day2_open_time: form.day2_open_time,
    force_exit_time: form.force_exit_time,
    schedule_start: form.schedule_start,
    schedule_stop: form.schedule_stop,
    schedule_days: form.schedule_days,
  }
  if (form.strategy_type === 'btst') {
    return {
      ...shared,
      name: form.name || `${form.underlying} BTST Flip`,
      moneyness: Number(form.moneyness),
      drop_pct: Number(form.drop_pct),
      vsl_pct: Number(form.vsl_pct),
      real_sl_pct: Number(form.real_sl_pct),
      be_trigger_pct: Number(form.be_trigger_pct),
      vix_max: Number(form.vix_max),
      trigger_mode: form.trigger_mode,
      candle_source: form.candle_source,
      dte_min: Number(form.dte_min),
      dte_max: Number(form.dte_max),
      entry_weekdays: form.entry_weekdays,
      ref_time: form.ref_time,
      entry_start_time: form.entry_start_time,
      entry_end_time: form.entry_end_time,
    }
  }
  return {
    ...shared,
    name: form.name || `${form.underlying} STBT`,
    entry_drop_pct: Number(form.entry_drop_pct),
    sl_pct: Number(form.sl_pct),
    max_reentries: Number(form.max_reentries),
    hedge_target_premium: Number(form.hedge_target_premium),
    take_profit_pct: Number(form.take_profit_pct),
    reentry_method: form.reentry_method,
    allow_day2_reentry: form.allow_day2_reentry,
    entry_time: form.entry_time,
    hedge_time: form.hedge_time,
  }
}

function pnlClass(value: number): string {
  if (value > 0) return 'text-green-600 dark:text-green-400'
  if (value < 0) return 'text-red-600 dark:text-red-400'
  return 'text-muted-foreground'
}

function LegRow({ leg }: { leg: StbtLeg }) {
  const isOpen = leg.state === 'IN_SHORT' || leg.state === 'IN_LONG'
  const mtm = isOpen ? (leg.mtm_pnl ?? 0) : 0
  const net = leg.realized_pnl - leg.charges_total + mtm
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm">
      <div className="min-w-0">
        <div className="truncate font-medium">{leg.symbol}</div>
        <div className="text-xs text-muted-foreground">
          {leg.state === 'WATCHING' ? (
            <>
              ref ₹{leg.ref_premium.toFixed(2)}
              {(leg.ltp ?? 0) > 0 && <> · LTP ₹{(leg.ltp ?? 0).toFixed(2)}</>}
            </>
          ) : leg.state === 'PAPER_SHORT' ? (
            <>
              paper short ₹{(leg.v_entry ?? 0).toFixed(2)} · buys at ₹
              {(leg.v_sl ?? 0).toFixed(2)}
              {(leg.ltp ?? 0) > 0 && <> · LTP ₹{(leg.ltp ?? 0).toFixed(2)}</>}
            </>
          ) : (
            <>
              entry ₹{leg.entry_price.toFixed(2)} · SL ₹{leg.sl_price.toFixed(2)}
              {isOpen && (leg.ltp ?? 0) > 0 && <> · LTP ₹{(leg.ltp ?? 0).toFixed(2)}</>}
              {leg.be_armed && <> · BE armed</>}
              {leg.reentries !== undefined && <> · re-entries {leg.reentries}</>}
            </>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <div className="text-right">
          <div className={pnlClass(net)}>
            {net >= 0 ? '+' : ''}
            {net.toFixed(2)}
          </div>
          {isOpen && (
            <div className="text-[10px] text-muted-foreground">
              MTM {mtm >= 0 ? '+' : ''}
              {mtm.toFixed(2)}
            </div>
          )}
        </div>
        <Badge variant="outline" className={LEG_STATE_STYLES[leg.state]}>
          {leg.state}
        </Badge>
      </div>
    </div>
  )
}

function LivePanel({ status }: { status: StbtStatusResponse }) {
  const live = status.live
  if (!live) {
    return (
      <p className="text-sm text-muted-foreground">
        No session data yet — the live panel appears after the engine starts.
      </p>
    )
  }
  const totalNet = live.total_net_pnl ?? live.net_pnl
  const mtm = live.mtm_pnl ?? 0
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Badge variant={['KILLED', 'ERROR'].includes(live.phase) ? 'destructive' : 'secondary'}>
          {PHASE_LABELS[live.phase] || live.phase}
        </Badge>
        <div className="text-right">
          <span className={`text-sm font-semibold ${pnlClass(totalNet)}`}>
            Net {totalNet >= 0 ? '+' : ''}
            {totalNet.toFixed(2)}
          </span>
          {mtm !== 0 && (
            <div className="text-[10px] text-muted-foreground">
              realized {live.net_pnl >= 0 ? '+' : ''}
              {live.net_pnl.toFixed(2)} · MTM {mtm >= 0 ? '+' : ''}
              {mtm.toFixed(2)}
            </div>
          )}
        </div>
      </div>
      {live.message && <p className="text-xs text-muted-foreground">{live.message}</p>}
      <div className="space-y-1.5">
        {live.main_legs.map((leg) => (
          <LegRow key={`${leg.symbol}-${leg.opt_type}`} leg={leg} />
        ))}
        {live.hedge && (
          <div className="flex items-center justify-between gap-2 rounded-md border border-dashed p-2 text-sm">
            <div className="min-w-0">
              <div className="truncate font-medium">HEDGE · {live.hedge.symbol}</div>
              <div className="text-xs text-muted-foreground">
                bought ₹{live.hedge.buy_price.toFixed(2)}
                {live.hedge.state === 'OPEN' && (live.hedge.ltp ?? 0) > 0 && (
                  <> · LTP ₹{(live.hedge.ltp ?? 0).toFixed(2)}</>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {live.hedge.state === 'OPEN' && (
                <span className={`text-xs ${pnlClass(live.hedge.mtm_pnl ?? 0)}`}>
                  {(live.hedge.mtm_pnl ?? 0) >= 0 ? '+' : ''}
                  {(live.hedge.mtm_pnl ?? 0).toFixed(2)}
                </span>
              )}
              <Badge variant="outline">{live.hedge.state}</Badge>
            </div>
          </div>
        )}
      </div>
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>
          Expiry {live.expiry} · qty {live.quantity}
          {(live.max_loss ?? 0) > 0 && <> · max loss ₹{live.max_loss}</>}
        </span>
        <span>updated {live.last_update?.slice(11, 19)}</span>
      </div>
    </div>
  )
}

function HistorySection({ strategyId }: { strategyId: string }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [records, setRecords] = useState<StbtHistoryRecord[] | null>(null)
  const [totalNet, setTotalNet] = useState(0)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && records === null) {
      try {
        setLoading(true)
        const data = await stbtApi.getHistory(strategyId)
        setRecords(data.records || [])
        setTotalNet(data.total_net || 0)
      } catch {
        showToast.error('Failed to load history', 'stbt')
        setRecords([])
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="border-t pt-2">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center justify-between text-xs text-muted-foreground hover:text-foreground"
      >
        <span className="flex items-center gap-1">
          <History className="h-3.5 w-3.5" /> Session history
          {records !== null && <>&nbsp;· {records.length} sessions</>}
        </span>
        <span className="flex items-center gap-2">
          {records !== null && records.length > 0 && (
            <span className={pnlClass(totalNet)}>
              Total {totalNet >= 0 ? '+' : ''}
              {totalNet.toFixed(2)}
            </span>
          )}
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>
      {open && (
        <div className="mt-2">
          {loading ? (
            <Skeleton className="h-16" />
          ) : !records || records.length === 0 ? (
            <p className="py-2 text-xs text-muted-foreground">
              No completed sessions yet — a record is added when a session finishes.
            </p>
          ) : (
            <div className="max-h-56 space-y-1 overflow-y-auto">
              {records.map((record) => (
                <div
                  key={record.ended_at}
                  className="flex items-center justify-between gap-2 rounded-md bg-muted/40 px-2 py-1.5 text-xs"
                >
                  <div className="min-w-0">
                    <span className="font-medium">{record.trade_date}</span>
                    <span className="ml-2 text-muted-foreground">
                      {record.final_phase === 'KILLED' ? '⛔ killed' : record.final_phase.toLowerCase()}
                      {' · '}
                      {record.legs?.reduce((sum, leg) => sum + (leg.cycles || 0), 0)} cycles
                      {' · charges '}
                      {record.charges.toFixed(0)}
                    </span>
                  </div>
                  <span className={`shrink-0 font-medium ${pnlClass(record.net_pnl)}`}>
                    {record.net_pnl >= 0 ? '+' : ''}
                    {record.net_pnl.toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Stbt() {
  const [configs, setConfigs] = useState<StbtConfig[]>([])
  const [statuses, setStatuses] = useState<Record<string, StbtStatusResponse>>({})
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<StbtConfig | null>(null)
  const [form, setForm] = useState<FormState>(DEFAULT_FORM)
  const [saving, setSaving] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<StbtConfig | null>(null)
  const [panicTarget, setPanicTarget] = useState<StbtConfig | null>(null)
  const [panicking, setPanicking] = useState(false)

  const fetchConfigs = async (silent = false) => {
    try {
      if (!silent) setLoading(true)
      const data = await stbtApi.getConfigs()
      setConfigs(data)
      const statusEntries = await Promise.all(
        data.map(async (config) => {
          try {
            return [config.strategy_id, await stbtApi.getStatus(config.strategy_id)] as const
          } catch {
            return null
          }
        })
      )
      setStatuses(Object.fromEntries(statusEntries.filter(Boolean) as [string, StbtStatusResponse][]))
    } catch {
      if (!silent) showToast.error('Failed to load STBT configs', 'stbt')
    } finally {
      if (!silent) setLoading(false)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only init of the poll timer and SSE subscription
  useEffect(() => {
    fetchConfigs()
    const timer = setInterval(() => fetchConfigs(true), 5000)
    // Run-state changes (start/stop/crash) arrive via the python host's SSE.
    const eventSource = new EventSource('/python/api/events')
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.strategy_id && data.status) fetchConfigs(true)
      } catch {
        // heartbeat / non-JSON events
      }
    }
    eventSource.onerror = () => {}
    return () => {
      clearInterval(timer)
      eventSource.close()
    }
  }, [])

  const openCreate = () => {
    setEditing(null)
    setForm(DEFAULT_FORM)
    setFormOpen(true)
  }

  const openEdit = (config: StbtConfig) => {
    setEditing(config)
    setForm(formFromConfig(config))
    setFormOpen(true)
  }

  const handleSave = async () => {
    try {
      setSaving(true)
      const payload = payloadFromForm(form)
      const response = editing
        ? await stbtApi.updateConfig(editing.strategy_id, payload)
        : await stbtApi.createConfig(payload)
      if (response.status === 'success') {
        showToast.success(response.message || 'Saved', 'stbt')
        setFormOpen(false)
        fetchConfigs()
      } else {
        showToast.error(response.message || 'Save failed', 'stbt')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Save failed', 'stbt')
    } finally {
      setSaving(false)
    }
  }

  const handleStart = async (config: StbtConfig) => {
    try {
      setActionLoading(config.strategy_id)
      const response = await stbtApi.startConfig(config.strategy_id)
      if (response.status === 'success') {
        showToast.success(response.message || `${config.name} started`, 'stbt')
        fetchConfigs(true)
      } else {
        showToast.error(response.message || 'Failed to start', 'stbt')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Failed to start', 'stbt')
    } finally {
      setActionLoading(null)
    }
  }

  const handleStop = async (config: StbtConfig) => {
    try {
      setActionLoading(config.strategy_id)
      const response = await stbtApi.stopConfig(config.strategy_id)
      if (response.status === 'success') {
        showToast.success(response.message || `${config.name} stopped`, 'stbt')
        fetchConfigs(true)
      } else {
        showToast.error(response.message || 'Failed to stop', 'stbt')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Failed to stop', 'stbt')
    } finally {
      setActionLoading(null)
    }
  }

  const handlePanic = async () => {
    if (!panicTarget) return
    const id = panicTarget.strategy_id
    try {
      setPanicking(true)
      const res = await stbtApi.panic(id)
      if (res.status === 'success') {
        showToast.success(
          `Closed ${res.closed.length}, ${res.already_flat.length} already flat`,
          'stbt',
        )
      } else if (res.status === 'partial') {
        showToast.error(
          `${res.failed.length} FAILED to close (${res.failed.join(', ')}) — check broker now`,
          'stbt',
        )
      } else {
        showToast.error(res.message || 'Panic close failed', 'stbt')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Panic close failed', 'stbt')
    } finally {
      setPanicking(false)
      setPanicTarget(null)
      fetchConfigs(true)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    try {
      setActionLoading(deleteTarget.strategy_id)
      const response = await stbtApi.deleteConfig(deleteTarget.strategy_id)
      if (response.status === 'success') {
        showToast.success('STBT config deleted', 'stbt')
      } else {
        showToast.error(response.message || 'Delete failed', 'stbt')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      showToast.error(axiosError.response?.data?.message || 'Delete failed', 'stbt')
    } finally {
      setActionLoading(null)
      setDeleteTarget(null)
      fetchConfigs(true)
    }
  }

  const toggleDay = (day: string) => {
    setForm((prev) => ({
      ...prev,
      schedule_days: prev.schedule_days.includes(day)
        ? prev.schedule_days.filter((d) => d !== day)
        : [...prev.schedule_days, day],
    }))
  }

  const numberField = (
    label: string,
    key: keyof FormState,
    props: { step?: string; min?: string; max?: string } = {}
  ) => (
    <div className="space-y-1.5">
      <Label htmlFor={`stbt-${key}`}>{label}</Label>
      <Input
        id={`stbt-${key}`}
        type="number"
        value={form[key] as string}
        onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
        {...props}
      />
    </div>
  )

  const timeField = (label: string, key: keyof FormState) => (
    <div className="space-y-1.5">
      <Label htmlFor={`stbt-${key}`}>{label}</Label>
      <Input
        id={`stbt-${key}`}
        type="time"
        value={form[key] as string}
        onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
      />
    </div>
  )

  return (
    <div className="container mx-auto space-y-6 p-4 md:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Moon className="h-6 w-6" /> STBT
          </h1>
          <p className="text-sm text-muted-foreground">
            Overnight strategies — STBT short strangle and the BTST paper-short flip
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="mr-1 h-4 w-4" /> New Config
        </Button>
      </div>

      <Tabs defaultValue="configs" className="space-y-4">
        <TabsList>
          <TabsTrigger value="configs">Configs</TabsTrigger>
          <TabsTrigger value="analytics">Analytics</TabsTrigger>
        </TabsList>

        <TabsContent value="analytics">
          <StbtAnalytics />
        </TabsContent>

        <TabsContent value="configs" className="space-y-4">
          {loading ? (
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-52" />
          <Skeleton className="h-52" />
        </div>
      ) : configs.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <Moon className="h-10 w-10 text-muted-foreground" />
            <p className="text-muted-foreground">
              No STBT configs yet. Create one to run the overnight short-premium strategy without
              editing code.
            </p>
            <Button onClick={openCreate}>
              <Plus className="mr-1 h-4 w-4" /> Create your first config
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {configs.map((config) => {
            const status = statuses[config.strategy_id]
            const busy = actionLoading === config.strategy_id
            return (
              <Card key={config.strategy_id}>
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <CardTitle className="flex flex-wrap items-center gap-2 text-lg">
                        <span className="truncate">{config.name}</span>
                        <Badge variant="outline">{config.underlying}</Badge>
                        <Badge
                          variant="outline"
                          className={
                            config.strategy_type === 'btst'
                              ? 'bg-purple-500/10 text-purple-600 dark:text-purple-400'
                              : 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
                          }
                        >
                          {STRATEGY_TYPE_LABELS[config.strategy_type ?? 'stbt']}
                        </Badge>
                        {config.is_running ? (
                          <Badge className="bg-green-500/15 text-green-600 dark:text-green-400">
                            Running
                          </Badge>
                        ) : config.is_error ? (
                          <Badge variant="destructive">Error</Badge>
                        ) : config.manually_stopped ? (
                          <Badge variant="secondary">Stopped</Badge>
                        ) : config.is_scheduled ? (
                          <Badge variant="secondary">Scheduled</Badge>
                        ) : null}
                      </CardTitle>
                      <CardDescription className="mt-1 flex flex-wrap items-center gap-3 text-xs">
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          {config.schedule_start}–{config.schedule_stop}
                        </span>
                        <span className="flex items-center gap-1">
                          <Calendar className="h-3 w-3" />
                          {(config.schedule_days || []).join(', ')}
                        </span>
                      </CardDescription>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {config.is_running ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => handleStop(config)}
                        >
                          {busy ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Square className="h-4 w-4" />
                          )}
                          <span className="ml-1 hidden sm:inline">Stop</span>
                        </Button>
                      ) : (
                        <Button size="sm" disabled={busy} onClick={() => handleStart(config)}>
                          {busy ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Play className="h-4 w-4" />
                          )}
                          <span className="ml-1 hidden sm:inline">Start</span>
                        </Button>
                      )}
                      {(() => {
                        const live = status?.live
                        const hasPosition =
                          !!live &&
                          ((live.main_legs || []).some(
                            (l) => l.state === 'IN_SHORT' || l.state === 'IN_LONG'
                          ) ||
                            live.hedge?.state === 'OPEN')
                        if (!config.is_running && !hasPosition) return null
                        return (
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={busy}
                            onClick={() => setPanicTarget(config)}
                            title="Close every position for this config at market now"
                          >
                            <AlertTriangle className="h-4 w-4" />
                            <span className="ml-1 hidden sm:inline">Close all</span>
                          </Button>
                        )
                      })()}
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button size="sm" variant="ghost">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem
                            disabled={config.is_running}
                            onClick={() => openEdit(config)}
                          >
                            <Pencil className="mr-2 h-4 w-4" /> Edit parameters
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            className="text-destructive"
                            onClick={() => setDeleteTarget(config)}
                          >
                            <Trash2 className="mr-2 h-4 w-4" /> Delete
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {config.is_error && config.error_message && (
                    <Alert variant="destructive">
                      <AlertTriangle className="h-4 w-4" />
                      <AlertDescription className="text-xs">
                        {config.error_message}
                      </AlertDescription>
                    </Alert>
                  )}
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                    {config.strategy_type === 'btst' ? (
                      <>
                        <span>ITM-{config.params?.moneyness ?? 2} CE</span>
                        <span>drop {config.params?.drop_pct ?? 5}%</span>
                        <span>flip +{config.params?.vsl_pct ?? 20}%</span>
                        <span>SL {config.params?.real_sl_pct ?? 30}%</span>
                        {(config.params?.be_trigger_pct ?? 0) > 0 && (
                          <span>BE +{config.params?.be_trigger_pct ?? 30}% (D2)</span>
                        )}
                        {(config.params?.vix_max ?? 0) > 0 && (
                          <span>VIX ≤ {config.params?.vix_max ?? 18}</span>
                        )}
                        <span>
                          DTE {config.params?.dte_min ?? 1}–{config.params?.dte_max ?? 3}
                        </span>
                        <span>
                          {config.params?.trigger_mode === 'CANDLE_CLOSE'
                            ? '1m-close triggers'
                            : 'tick triggers'}
                        </span>
                        <span>lots ×{config.params?.lot_multiplier ?? 1}</span>
                      </>
                    ) : (
                      <>
                        <span>entry drop {config.params?.entry_drop_pct ?? 5}%</span>
                        <span>SL {config.params?.sl_pct ?? 20}%</span>
                        <span>re-entries {config.params?.max_reentries ?? 1}</span>
                        <span>hedge ≈₹{config.params?.hedge_target_premium ?? 20}</span>
                        <span>lots ×{config.params?.lot_multiplier ?? 1}</span>
                        {(config.params?.take_profit_pct ?? 0) > 0 && (
                          <span className="text-green-600/80">
                            target {config.params?.take_profit_pct}%
                          </span>
                        )}
                      </>
                    )}
                    {(config.params?.max_loss ?? 0) > 0 && (
                      <span className="text-red-500/80">max loss ₹{config.params?.max_loss}</span>
                    )}
                  </div>
                  {status && <LivePanel status={status} />}
                  <HistorySection strategyId={config.strategy_id} />
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}
        </TabsContent>
      </Tabs>

      {/* Create / edit dialog */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {editing
                ? `Edit ${editing.name}`
                : form.strategy_type === 'btst'
                  ? 'New BTST Flip Config'
                  : 'New STBT Config'}
            </DialogTitle>
            <DialogDescription>
              {editing
                ? 'Strategy type and underlying cannot be changed after creation.'
                : form.strategy_type === 'btst'
                  ? 'Paper-short flip: 5% drop opens a virtual short; its +20% stop-out buys the CE. Defaults match the filtered SENSEX backtest.'
                  : 'Parameters default to the classic SENSEX STBT setup.'}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Strategy type</Label>
              <Select
                value={form.strategy_type}
                disabled={!!editing}
                onValueChange={(value) =>
                  setForm((prev) => ({ ...prev, strategy_type: value as StbtStrategyType }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="stbt">STBT — short strangle overnight</SelectItem>
                  <SelectItem value="btst">BTST — paper-short flip (long CE)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="stbt-name">Name</Label>
              <Input
                id="stbt-name"
                placeholder={`${form.underlying} ${form.strategy_type === 'btst' ? 'BTST Flip' : 'STBT'}`}
                value={form.name}
                onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Underlying</Label>
              <Select
                value={form.underlying}
                disabled={!!editing}
                onValueChange={(value) => setForm((prev) => ({ ...prev, underlying: value }))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SUPPORTED_UNDERLYINGS.map((u) => (
                    <SelectItem key={u} value={u}>
                      {u}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {numberField('Lot multiplier', 'lot_multiplier', { step: '1', min: '1', max: '100' })}

            {form.strategy_type === 'btst' ? (
              <>
                {numberField('ITM level (CE strike)', 'moneyness', {
                  step: '1',
                  min: '1',
                  max: '5',
                })}
                {numberField('Drop % (paper short)', 'drop_pct', {
                  step: '0.5',
                  min: '0',
                  max: '50',
                })}
                {numberField('Flip trigger % (virtual SL)', 'vsl_pct', {
                  step: '1',
                  min: '1',
                  max: '200',
                })}
                {numberField('Stop-loss % (below buy)', 'real_sl_pct', {
                  step: '1',
                  min: '1',
                  max: '99',
                })}
                {numberField('Breakeven arm % (0 = off)', 'be_trigger_pct', {
                  step: '5',
                  min: '0',
                  max: '200',
                })}
                {numberField('Max India VIX (0 = off)', 'vix_max', {
                  step: '0.5',
                  min: '0',
                  max: '100',
                })}
                {numberField('Min DTE', 'dte_min', { step: '1', min: '1', max: '30' })}
                {numberField('Max DTE', 'dte_max', { step: '1', min: '1', max: '30' })}
                {numberField('Max loss ₹ (0 = off)', 'max_loss', { step: '500', min: '0' })}

                <div className="space-y-1.5">
                  <Label>Signal trigger</Label>
                  <Select
                    value={form.trigger_mode}
                    onValueChange={(value) =>
                      setForm((prev) => ({
                        ...prev,
                        trigger_mode: value as FormState['trigger_mode'],
                      }))
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="TICK">Live ticks — act the instant a level is hit</SelectItem>
                      <SelectItem value="CANDLE_CLOSE">
                        1-min candle close — backtest parity
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {form.trigger_mode === 'CANDLE_CLOSE' && (
                  <div className="space-y-1.5">
                    <Label>Signal candle source</Label>
                    <Select
                      value={form.candle_source}
                      onValueChange={(value) =>
                        setForm((prev) => ({
                          ...prev,
                          candle_source: value as FormState['candle_source'],
                        }))
                      }
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="WS">WebSocket ticks (live feed)</SelectItem>
                        <SelectItem value="HISTORY">History API (official 1-min close)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                )}

                <div className="space-y-1.5 sm:col-span-2">
                  <Label>Entry days (Friday off = weekend theta filter)</Label>
                  <div className="flex flex-wrap gap-1.5">
                    {ENTRY_WEEKDAY_OPTIONS.map((day) => (
                      <Button
                        key={day.value}
                        type="button"
                        size="sm"
                        variant={form.entry_weekdays.includes(day.value) ? 'default' : 'outline'}
                        onClick={() =>
                          setForm((prev) => ({
                            ...prev,
                            entry_weekdays: prev.entry_weekdays.includes(day.value)
                              ? prev.entry_weekdays.filter((d) => d !== day.value)
                              : [...prev.entry_weekdays, day.value],
                          }))
                        }
                      >
                        {day.label}
                      </Button>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <>
                {numberField('Entry drop %', 'entry_drop_pct', {
                  step: '0.5',
                  min: '0',
                  max: '50',
                })}
                {numberField('Stop-loss %', 'sl_pct', { step: '1', min: '1', max: '200' })}
                {numberField('Max re-entries', 'max_reentries', { step: '1', min: '0', max: '5' })}
                {numberField('Hedge target ₹', 'hedge_target_premium', {
                  step: '5',
                  min: '1',
                  max: '1000',
                })}
                {numberField('Max loss ₹ (0 = off)', 'max_loss', { step: '500', min: '0' })}
                {numberField('Take profit % (0 = off)', 'take_profit_pct', {
                  step: '5',
                  min: '0',
                  max: '99',
                })}

                <div className="space-y-1.5">
                  <Label>Re-entry method</Label>
                  <Select
                    value={form.reentry_method}
                    onValueChange={(value) =>
                      setForm((prev) => ({
                        ...prev,
                        reentry_method: value as FormState['reentry_method'],
                      }))
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="CANDLE_CLOSE">1-min candle close (StockMock)</SelectItem>
                      <SelectItem value="LTP">Instant tick (LTP)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex items-center justify-between rounded-md border p-3 sm:col-span-2">
                  <div>
                    <Label>Allow Day-2 re-entry</Label>
                    <p className="text-xs text-muted-foreground">
                      Continue SL re-entry checks on Day-2 (StockMock positional style)
                    </p>
                  </div>
                  <Switch
                    checked={form.allow_day2_reentry}
                    onCheckedChange={(checked) =>
                      setForm((prev) => ({ ...prev, allow_day2_reentry: checked }))
                    }
                  />
                </div>
              </>
            )}

            <div className="flex items-center justify-between rounded-md border p-3 sm:col-span-2">
              <div>
                <Label>Telegram alerts</Label>
                <p className="text-xs text-muted-foreground">
                  Entries, SL hits, kill switch, and session summary to your linked Telegram
                  (needs the Telegram bot set up)
                </p>
              </div>
              <Switch
                checked={form.telegram_alerts}
                onCheckedChange={(checked) =>
                  setForm((prev) => ({ ...prev, telegram_alerts: checked }))
                }
              />
            </div>

            {form.strategy_type === 'btst' ? (
              <>
                {timeField('Reference snapshot', 'ref_time')}
                {timeField('Entry window start', 'entry_start_time')}
                {timeField('Entry window end', 'entry_end_time')}
              </>
            ) : (
              <>
                {timeField('Day-1 entry', 'entry_time')}
                {timeField('Hedge placement', 'hedge_time')}
              </>
            )}
            {timeField('WS close', 'ws_close_time')}
            {timeField('Day-2 open', 'day2_open_time')}
            {timeField('Day-2 force exit', 'force_exit_time')}

            <div className="space-y-1.5 sm:col-span-2">
              <Label>Strategy Manager schedule (process start/stop)</Label>
              <div className="grid grid-cols-2 gap-3">
                <Input
                  type="time"
                  value={form.schedule_start}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, schedule_start: e.target.value }))
                  }
                />
                <Input
                  type="time"
                  value={form.schedule_stop}
                  onChange={(e) => setForm((prev) => ({ ...prev, schedule_stop: e.target.value }))}
                />
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {SCHEDULE_DAY_OPTIONS.map((day) => (
                  <Button
                    key={day.value}
                    type="button"
                    size="sm"
                    variant={form.schedule_days.includes(day.value) ? 'default' : 'outline'}
                    onClick={() => toggleDay(day.value)}
                  >
                    {day.label}
                  </Button>
                ))}
              </div>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setFormOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSave}
              disabled={
                saving ||
                form.schedule_days.length === 0 ||
                (form.strategy_type === 'btst' && form.entry_weekdays.length === 0)
              }
            >
              {saving && <RefreshCw className="mr-1 h-4 w-4 animate-spin" />}
              {editing ? 'Save changes' : 'Create config'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation dialog */}
      <Dialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleteTarget?.name}?</DialogTitle>
            <DialogDescription>
              This stops the strategy if running and removes its config, launcher, and state
              files. Broker positions are NOT touched — close any open positions manually first.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={actionLoading === deleteTarget?.strategy_id}
              onClick={handleDelete}
            >
              <Trash2 className="mr-1 h-4 w-4" /> Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Panic close-all confirmation */}
      <Dialog open={!!panicTarget} onOpenChange={(open) => !open && setPanicTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Close all positions for {panicTarget?.name}?</DialogTitle>
            <DialogDescription>
              This stops the strategy and immediately force-closes every leg and hedge it holds
              at market, then clears its state. Use this only in an emergency — it acts on your
              real broker account.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPanicTarget(null)} disabled={panicking}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={panicking} onClick={handlePanic}>
              {panicking ? (
                <RefreshCw className="mr-1 h-4 w-4 animate-spin" />
              ) : (
                <AlertTriangle className="mr-1 h-4 w-4" />
              )}
              Close all now
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
