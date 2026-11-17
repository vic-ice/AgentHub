import { useEffect, useMemo, useState } from "react"
import {
  CheckCircle2,
  CircleDashed,
  Clock3,
  Search,
  Sparkles,
  Square,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  formatDuration,
  formatElapsed,
  friendlyStepDetail,
  friendlyStepTitle,
  toolDisplay,
} from "@/features/chat/execution-display"
import { cn } from "@/lib/utils"
import type { CompletedExecutionStep, ToolCallInfo } from "@/types"

type ExecutionProgressCardProps = {
  isStreaming: boolean
  isProcessing: boolean
  isAgentThinking: boolean
  steps: CompletedExecutionStep[]
  tools: ToolCallInfo[]
  onStop: () => void
}

export function ExecutionProgressCard({
  isStreaming,
  isProcessing,
  isAgentThinking,
  steps,
  tools,
  onStop,
}: ExecutionProgressCardProps) {
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)

  useEffect(() => {
    if (!isStreaming) {
      setStartedAt(null)
      setElapsedSeconds(0)
      return
    }
    setStartedAt((current) => current ?? Date.now())
  }, [isStreaming])

  useEffect(() => {
    if (!isStreaming || startedAt == null) return
    const update = () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [isStreaming, startedAt])

  const visibleActivities = useMemo(() => {
    if (steps.length > 0) {
      return steps.slice(-4).map((step) => ({
        id: step.step_id,
        title: friendlyStepTitle(step),
        detail: friendlyStepDetail(step),
        duration: formatDuration(step.duration_ms),
        status: step.status,
      }))
    }
    return tools.slice(-4).map((tool) => {
      const display = toolDisplay(tool)
      return {
        id: tool.id,
        title: display.title,
        detail: tool.status === "completed" ? display.result : `正在处理：${display.input}`,
        duration: null,
        status: tool.status === "completed" ? "completed" : "waiting",
      }
    })
  }, [steps, tools])

  const currentLabel = (() => {
    if (isProcessing && visibleActivities.length === 0) return "正在理解你的问题"
    if (tools.some((tool) => tool.status === "calling")) return "正在获取所需信息"
    if (visibleActivities.some((activity) => activity.status === "waiting")) {
      return "正在获取并核对所需信息"
    }
    if (isAgentThinking) return "正在分析已有结果"
    if (visibleActivities.length > 0) return "正在组织最终回答"
    return "正在启动本轮任务"
  })()

  if (!isStreaming) return null

  return (
    <section
      className="execution-progress-card max-w-[92%] overflow-hidden rounded-2xl border border-primary/15 bg-card/95 shadow-sm"
      aria-live="polite"
      aria-label="本轮实时执行进度"
      data-od-id="live-execution-progress"
    >
      <div className="execution-progress-sheen h-1 w-full bg-primary/15" aria-hidden="true" />
      <div className="px-4 py-3.5 sm:px-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary">
              {tools.some((tool) => tool.status === "calling") ||
              visibleActivities.some((activity) => activity.status === "waiting") ? (
                <Search className="size-4 animate-pulse" />
              ) : visibleActivities.length > 0 ? (
                <Sparkles className="size-4 animate-pulse" />
              ) : (
                <CircleDashed className="size-4 animate-spin" />
              )}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-foreground">{currentLabel}</p>
              <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                <Clock3 className="size-3.5" />
                已用时 {formatElapsed(elapsedSeconds)}
                <span aria-hidden="true">·</span>
                结果会在完成后直接显示
              </p>
            </div>
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-8 shrink-0 gap-1.5 px-2 text-xs text-muted-foreground hover:text-foreground"
            onClick={onStop}
            aria-label="停止生成"
          >
            <Square className="size-3 fill-current" />
            停止
          </Button>
        </div>

        {visibleActivities.length > 0 ? (
          <ol className="mt-3 space-y-2 border-t border-border/60 pt-3">
            {visibleActivities.map((activity, index) => {
              const isLast = index === visibleActivities.length - 1
              const completed = activity.status === "completed"
              return (
                <li key={activity.id} className="flex gap-2.5 text-sm animate-in fade-in slide-in-from-bottom-1 duration-300">
                  <span className="mt-0.5 grid size-5 shrink-0 place-items-center">
                    {completed ? (
                      <CheckCircle2 className="size-4 text-emerald-600" />
                    ) : (
                      <CircleDashed className="size-4 animate-spin text-primary" />
                    )}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className={cn("font-medium", isLast ? "text-foreground" : "text-foreground/80")}>
                        {activity.title}
                      </span>
                      {activity.duration ? (
                        <span className="shrink-0 text-[11px] text-muted-foreground">{activity.duration}</span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-xs leading-5 text-muted-foreground">{activity.detail}</p>
                  </div>
                </li>
              )
            })}
          </ol>
        ) : (
          <div className="mt-3 flex gap-1.5 border-t border-border/60 pt-3" aria-hidden="true">
            <span className="h-1.5 w-16 animate-pulse rounded-full bg-primary/25" />
            <span className="h-1.5 w-10 animate-pulse rounded-full bg-primary/15 [animation-delay:120ms]" />
            <span className="h-1.5 w-20 animate-pulse rounded-full bg-primary/10 [animation-delay:240ms]" />
          </div>
        )}
      </div>
    </section>
  )
}
