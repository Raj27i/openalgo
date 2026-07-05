// STBT Analytics — AlgoTest-style calendar heatmap + cumulative equity curve,
// seeded from the per-cycle journal the engine writes (GET /stbt/api/analytics).

import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import { RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
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
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { makeFormatCurrency } from '@/lib/utils'
import { stbtApi } from '@/api/stbt'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'
import type { StbtAnalyticsResponse, StbtDailyPnl } from '@/types/stbt'
import { showToast } from '@/utils/toast'

const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S']
const MONTH_NAMES = [
  'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
  'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
]

function cellColor(value: number, maxAbs: number): string {
  if (value === 0 || maxAbs === 0) return 'rgba(148,163,184,0.15)'
  const intensity = Math.min(1, Math.abs(value) / maxAbs)
  const alpha = 0.2 + 0.7 * intensity
  return value > 0 ? `rgba(34,197,94,${alpha})` : `rgba(239,68,68,${alpha})`
}

interface MonthBlock {
  key: string
  year: number
  month: number
  weeks: (StbtDailyPnl | null)[][] // 7 rows (weekday) × N week-columns
}

/** Build month blocks (weekday-row × week-column grids) spanning all dated data. */
function buildMonths(byDate: Map<string, StbtDailyPnl>, dates: string[]): MonthBlock[] {
  if (dates.length === 0) return []
  const first = new Date(`${dates[0]}T00:00:00`)
  const last = new Date(`${dates[dates.length - 1]}T00:00:00`)
  const blocks: MonthBlock[] = []
  const cursor = new Date(first.getFullYear(), first.getMonth(), 1)
  while (cursor <= last) {
    const year = cursor.getFullYear()
    const month = cursor.getMonth()
    const daysInMonth = new Date(year, month + 1, 0).getDate()
    // columns = weeks; each column has 7 rows indexed by weekday (0=Sun)
    const weeks: (StbtDailyPnl | null)[][] = []
    let col: (StbtDailyPnl | null)[] = new Array(7).fill(null)
    for (let day = 1; day <= daysInMonth; day++) {
      const d = new Date(year, month, day)
      const wd = d.getDay()
      const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
      col[wd] = byDate.get(iso) ?? { date: iso, gross: 0, charges: 0, net: 0, cycles: 0, wins: 0, losses: 0 }
      if (wd === 6) {
        weeks.push(col)
        col = new Array(7).fill(null)
      }
    }
    if (col.some((c) => c !== null)) weeks.push(col)
    blocks.push({ key: `${year}-${month}`, year, month, weeks })
    cursor.setMonth(cursor.getMonth() + 1)
  }
  return blocks
}

export default function StbtAnalytics() {
  const { user } = useAuthStore()
  const { mode } = useThemeStore()
  const isDark = mode === 'dark'
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])

  const [loading, setLoading] = useState(true)
  const [data, setData] = useState<StbtAnalyticsResponse | null>(null)
  const [includeCharges, setIncludeCharges] = useState(true)
  const [configFilter, setConfigFilter] = useState<string>('all')

  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = configFilter === 'all' ? undefined : { config: configFilter }
      const resp = await stbtApi.getAnalytics(params)
      setData(resp)
    } catch {
      showToast.error('Failed to load analytics', 'stbt')
    } finally {
      setLoading(false)
    }
  }, [configFilter])

  useEffect(() => {
    load()
  }, [load])

  // value picker: net (charges included) vs gross
  const dayValue = useCallback(
    (d: StbtDailyPnl) => (includeCharges ? d.net : d.gross),
    [includeCharges],
  )

  // Sum of a month block's traded cells for the monthly-total row.
  const monthTotal = useCallback(
    (block: MonthBlock) =>
      block.weeks.reduce(
        (sum, week) =>
          sum + week.reduce((s, cell) => s + (cell && cell.cycles > 0 ? dayValue(cell) : 0), 0),
        0,
      ),
    [dayValue],
  )

  const { byDate, dates, maxAbs } = useMemo(() => {
    const map = new Map<string, StbtDailyPnl>()
    let mx = 0
    for (const d of data?.daily ?? []) {
      map.set(d.date, d)
      mx = Math.max(mx, Math.abs(includeCharges ? d.net : d.gross))
    }
    return { byDate: map, dates: (data?.daily ?? []).map((d) => d.date), maxAbs: mx }
  }, [data, includeCharges])

  const months = useMemo(() => buildMonths(byDate, dates), [byDate, dates])

  // Equity curve chart
  useEffect(() => {
    const container = chartContainerRef.current
    if (!container) return
    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 320,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDark ? '#a6adbb' : '#334155',
      },
      grid: {
        vertLines: { color: isDark ? 'rgba(166,173,187,0.1)' : 'rgba(0,0,0,0.06)' },
        horzLines: { color: isDark ? 'rgba(166,173,187,0.1)' : 'rgba(0,0,0,0.06)' },
      },
      rightPriceScale: { borderColor: isDark ? 'rgba(166,173,187,0.2)' : 'rgba(0,0,0,0.15)' },
      timeScale: { borderColor: isDark ? 'rgba(166,173,187,0.2)' : 'rgba(0,0,0,0.15)' },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#22c55e',
      topColor: 'rgba(34,197,94,0.35)',
      bottomColor: 'rgba(34,197,94,0.0)',
      lineWidth: 2,
      priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
    })
    chartRef.current = chart
    seriesRef.current = series
    const onResize = () => chart.applyOptions({ width: container.offsetWidth })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [isDark, formatCurrency])

  useEffect(() => {
    if (!seriesRef.current || !data) return
    let cum = 0
    const points = data.curve.map((c) => {
      cum = includeCharges ? c.cumulative_net : c.cumulative_gross
      return { time: c.date as Time, value: cum }
    })
    seriesRef.current.setData(points)
    chartRef.current?.timeScale().fitContent()
  }, [data, includeCharges])

  const totals = data?.totals
  const headlineNet = totals ? (includeCharges ? totals.net : totals.gross) : 0
  const winRate =
    totals && totals.trading_days > 0
      ? Math.round((totals.win_days / totals.trading_days) * 100)
      : 0

  if (loading) return <Skeleton className="h-96" />

  const hasData = (data?.daily.length ?? 0) > 0

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-4">
          {(data?.configs.length ?? 0) > 1 && (
            <Select value={configFilter} onValueChange={setConfigFilter}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All configs</SelectItem>
                {data?.configs.map((c) => (
                  <SelectItem key={c.strategy_id} value={c.strategy_id}>
                    {c.name} ({c.underlying})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <div className="flex items-center gap-2">
            <Switch id="charges" checked={includeCharges} onCheckedChange={setIncludeCharges} />
            <Label htmlFor="charges" className="text-sm">
              Include charges
            </Label>
          </div>
        </div>
        <button
          type="button"
          onClick={load}
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {/* Headline stats */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Net P&amp;L{includeCharges ? '' : ' (gross)'}</p>
            <p className={`text-xl font-bold ${headlineNet >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {formatCurrency(headlineNet)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Total charges</p>
            <p className="text-xl font-bold">{formatCurrency(totals?.charges ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Trading days</p>
            <p className="text-xl font-bold">{totals?.trading_days ?? 0}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Win rate</p>
            <p className="text-xl font-bold">
              {winRate}% <span className="text-xs font-normal text-muted-foreground">({totals?.win_days ?? 0}W / {totals?.loss_days ?? 0}L)</span>
            </p>
          </CardContent>
        </Card>
      </div>

      {!hasData ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            No completed cycles yet. The calendar fills in as STBT trades close.
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Calendar heatmap */}
          <Card>
            <CardContent className="overflow-x-auto p-4">
              <TooltipProvider delayDuration={100}>
                <div className="flex gap-6">
                  {months.map((block) => (
                    <div key={block.key} className="shrink-0">
                      <p className="mb-2 text-center text-xs font-medium text-muted-foreground">
                        {MONTH_NAMES[block.month]} {String(block.year).slice(2)}
                      </p>
                      <div className="flex gap-1">
                        {/* weekday labels */}
                        <div className="flex flex-col gap-1">
                          {WEEKDAYS.map((w, i) => (
                            <div
                              key={`${block.key}-wd-${i}`}
                              className="flex h-4 w-4 items-center justify-center text-[9px] text-muted-foreground"
                            >
                              {w}
                            </div>
                          ))}
                        </div>
                        {block.weeks.map((week, wi) => (
                          <div key={`${block.key}-w-${wi}`} className="flex flex-col gap-1">
                            {week.map((cell, di) => {
                              if (!cell)
                                return <div key={`${block.key}-${wi}-${di}`} className="h-4 w-4" />
                              const v = dayValue(cell)
                              const traded = cell.cycles > 0
                              return (
                                <Tooltip key={`${block.key}-${wi}-${di}`}>
                                  <TooltipTrigger asChild>
                                    <div
                                      className="h-4 w-4 rounded-sm"
                                      style={{
                                        backgroundColor: traded
                                          ? cellColor(v, maxAbs)
                                          : 'rgba(148,163,184,0.08)',
                                      }}
                                    />
                                  </TooltipTrigger>
                                  {traded && (
                                    <TooltipContent>
                                      <div className="text-xs">
                                        <div className="font-medium">{cell.date}</div>
                                        <div className={v >= 0 ? 'text-green-500' : 'text-red-500'}>
                                          {formatCurrency(v)}
                                        </div>
                                        <div className="text-muted-foreground">
                                          {cell.cycles} cycles · {cell.wins}W/{cell.losses}L
                                        </div>
                                      </div>
                                    </TooltipContent>
                                  )}
                                </Tooltip>
                              )
                            })}
                          </div>
                        ))}
                      </div>
                      {(() => {
                        const mt = monthTotal(block)
                        return (
                          <div className="mt-2 border-t pt-1.5 text-center text-xs">
                            <span className="text-muted-foreground">Total </span>
                            <span
                              className={`font-medium ${mt >= 0 ? 'text-green-500' : 'text-red-500'}`}
                            >
                              {mt >= 0 ? '+' : ''}
                              {formatCurrency(mt)}
                            </span>
                          </div>
                        )
                      })()}
                    </div>
                  ))}
                </div>
              </TooltipProvider>
              <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: 'rgba(239,68,68,0.8)' }} />
                Loss
                <span className="ml-2 inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: 'rgba(148,163,184,0.15)' }} />
                Breakeven
                <span className="ml-2 inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: 'rgba(34,197,94,0.8)' }} />
                Profit
              </div>
            </CardContent>
          </Card>

          {/* Equity curve */}
          <Card>
            <CardContent className="p-4">
              <p className="mb-2 text-sm font-medium">Cumulative P&amp;L</p>
              <div ref={chartContainerRef} className="w-full" />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
