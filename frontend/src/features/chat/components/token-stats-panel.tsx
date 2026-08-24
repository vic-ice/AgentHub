import { useCallback, useEffect, useMemo, useState } from "react"
import { Activity, ChevronDown, Coins, RefreshCw } from "lucide-react"

import { getCurrentUserId, requestJson } from "@/lib/api"
import { formatErrorForDisplay } from "@/lib/errors"
import type { ConversationInDB } from "@/types"

interface TokenStatsPanelProps {
  currentConversation: ConversationInDB | null
}

type DailyStat = {
  date: string
  conversation_count: number
  total_tokens: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  reasoning_tokens: number
}

type ConversationUsage = {
  thread_id: string
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  reasoning_tokens: number
  total_tokens: number
}

type TokenSegment = {
  key: string
  label: string
  value: number
  color: string
  hint: string
}

function compactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return value.toLocaleString("zh-CN")
}

function shortDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getMonth() + 1}/${date.getDate()}`
}

function numberField(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0
}

function parseDailyStats(payload: unknown): DailyStat[] {
  if (!Array.isArray(payload)) return []
  return payload.flatMap((item) => {
    if (!item || typeof item !== "object") return []
    const record = item as Record<string, unknown>
    if (typeof record.date !== "string") return []
    return [{
      date: record.date,
      conversation_count: numberField(record.conversation_count),
      total_tokens: numberField(record.total_tokens),
      input_tokens: numberField(record.input_tokens),
      output_tokens: numberField(record.output_tokens),
      cached_tokens: numberField(record.cached_tokens),
      reasoning_tokens: numberField(record.reasoning_tokens),
    }]
  })
}

function pastSevenDays(): string[] {
  const result: string[] = []
  const today = new Date()
  for (let index = 6; index >= 0; index -= 1) {
    const date = new Date(today)
    date.setDate(date.getDate() - index)
    result.push(date.toISOString().split("T")[0])
  }
  return result
}

export function TokenStatsPanel({ currentConversation }: TokenStatsPanelProps) {
  const [showTrend, setShowTrend] = useState(false)
  const [dailyStats, setDailyStats] = useState<DailyStat[]>([])
  const [isLoadingStats, setIsLoadingStats] = useState(false)
  const [statsError, setStatsError] = useState("")
  const [conversationUsage, setConversationUsage] = useState<ConversationUsage | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const threadId = currentConversation?.thread_id
    const userId = getCurrentUserId()
    if (!threadId || !userId) {
      return () => controller.abort()
    }
    void requestJson<ConversationUsage>(
      `/chat/conversations/${threadId}/stats?user_id=${encodeURIComponent(userId)}`,
      { signal: controller.signal },
    )
      .then((payload: ConversationUsage) => setConversationUsage(payload))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setConversationUsage(null)
        }
      })
    return () => controller.abort()
  }, [currentConversation?.thread_id, currentConversation?.total_tokens])

  const activeUsage = conversationUsage?.thread_id === currentConversation?.thread_id
    ? conversationUsage
    : null
  const tokens = useMemo(() => ({
    input: activeUsage?.input_tokens ?? currentConversation?.input_tokens ?? 0,
    output: activeUsage?.output_tokens ?? currentConversation?.output_tokens ?? 0,
    cache: activeUsage?.cached_tokens ?? 0,
    reasoning: activeUsage?.reasoning_tokens ?? 0,
    total: activeUsage?.total_tokens ?? currentConversation?.total_tokens ?? 0,
  }), [activeUsage, currentConversation])

  const segments = useMemo<TokenSegment[]>(() => [
    { key: "input", label: "\u65b0\u8f93\u5165", value: Math.max(0, tokens.input - tokens.cache), color: "var(--token-input)", hint: "\u672a\u547d\u4e2d Provider Prompt Cache \u7684\u8f93\u5165" },
    { key: "output", label: "\u53ef\u89c1\u8f93\u51fa", value: Math.max(0, tokens.output - tokens.reasoning), color: "var(--token-output)", hint: "\u9664\u63a8\u7406\u5916\u7684\u6a21\u578b\u8f93\u51fa" },
    { key: "cache", label: "\u7f13\u5b58\u8bfb\u53d6", value: tokens.cache, color: "var(--muted-foreground)", hint: "Provider \u8fd4\u56de\u7684 cached tokens" },
    { key: "reasoning", label: "\u63a8\u7406", value: tokens.reasoning, color: "var(--border)", hint: "Provider \u8fd4\u56de\u7684 reasoning tokens" },
  ], [tokens])

  const denominator = Math.max(
    tokens.total,
    segments.reduce((sum, segment) => sum + segment.value, 0),
    1,
  )

  const fetchDailyStats = useCallback(async () => {
    setIsLoadingStats(true)
    setStatsError("")
    try {
      const params = new URLSearchParams({ days: "7" })
      const userId = getCurrentUserId()
      if (userId) params.set("user_id", userId)
      const payload = await requestJson<unknown>(`/chat/stats/daily?${params.toString()}`)
      const parsed = parseDailyStats(payload)
      const byDate = new Map(parsed.map((item) => [item.date, item]))
      setDailyStats(pastSevenDays().map((date) => byDate.get(date) ?? {
        date,
        conversation_count: 0,
        total_tokens: 0,
        input_tokens: 0,
        output_tokens: 0,
        cached_tokens: 0,
        reasoning_tokens: 0,
      }))
    } catch (error) {
      setStatsError(formatErrorForDisplay(error, "\u65e0\u6cd5\u8bfb\u53d6\u4e03\u65e5\u8d8b\u52bf"))
    } finally {
      setIsLoadingStats(false)
    }
  }, [])

  const toggleTrend = () => {
    const next = !showTrend
    setShowTrend(next)
    if (next && dailyStats.length === 0 && !isLoadingStats) void fetchDailyStats()
  }

  const maxDaily = Math.max(...dailyStats.map((item) => item.total_tokens), 1)

  return (
    <section className="overflow-hidden border border-border bg-card" data-od-id="token-usage-panel">
      <header className="flex min-h-12 items-center justify-between border-b border-border px-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Coins className="size-4" aria-hidden="true" />
            Token 用量
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">当前会话累计</p>
        </div>
        <strong className="font-mono text-base font-semibold tabular-nums">{compactNumber(tokens.total)}</strong>
      </header>

      <div className="p-3">
        <div className="flex h-2 w-full overflow-hidden bg-muted" aria-label="Token 构成">
          {segments.map((segment) => (
            <span
              key={segment.key}
              className="h-full min-w-0"
              style={{ width: `${(segment.value / denominator) * 100}%`, backgroundColor: segment.color }}
              title={`${segment.label} ${segment.value.toLocaleString("zh-CN")}`}
            />
          ))}
        </div>

        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3">
          {segments.map((segment) => (
            <div key={segment.key} className="min-w-0">
              <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className="size-2" style={{ backgroundColor: segment.color }} aria-hidden="true" />
                {segment.label}
              </dt>
              <dd className="mt-1 font-mono text-sm font-medium tabular-nums text-foreground">{compactNumber(segment.value)}</dd>
              <p className="mt-0.5 truncate text-[11px] text-muted-foreground" title={segment.hint}>{segment.hint}</p>
            </div>
          ))}
        </dl>

        <button
          type="button"
          onClick={toggleTrend}
          className="mt-3 flex min-h-10 w-full items-center justify-between border-t border-border pt-3 text-left text-xs font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={showTrend}
        >
          <span>最近 7 日趋势</span>
          <ChevronDown className={`size-4 transition-transform ${showTrend ? "rotate-180" : ""}`} aria-hidden="true" />
        </button>

        {showTrend && (
          <div className="mt-2 border-t border-border pt-3">
            {isLoadingStats ? (
              <div className="flex min-h-24 items-center justify-center gap-2 text-xs text-muted-foreground" role="status">
                <Activity className="size-4 animate-pulse" aria-hidden="true" />正在读取趋势…
              </div>
            ) : statsError ? (
              <div className="py-3 text-xs text-destructive" role="alert">
                <p>{statsError}</p>
                <button type="button" onClick={() => void fetchDailyStats()} className="mt-2 inline-flex min-h-9 items-center gap-1.5 font-medium underline underline-offset-4">
                  <RefreshCw className="size-3.5" />重试
                </button>
              </div>
            ) : (
              <div className="space-y-2">
                {dailyStats.map((item) => (
                  <div key={item.date} className="grid grid-cols-[34px_minmax(0,1fr)_44px] items-center gap-2 text-[11px]">
                    <span className="font-mono text-muted-foreground">{shortDate(item.date)}</span>
                    <div className="flex h-2 overflow-hidden bg-muted">
                      <span style={{ width: `${(item.input_tokens / maxDaily) * 100}%`, backgroundColor: "var(--token-input)" }} />
                      <span style={{ width: `${(item.output_tokens / maxDaily) * 100}%`, backgroundColor: "var(--token-output)" }} />
                    </div>
                    <span className="text-right font-mono text-muted-foreground">{compactNumber(item.total_tokens)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
