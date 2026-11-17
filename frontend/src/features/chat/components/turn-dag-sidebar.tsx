import { useEffect, useMemo, useState } from "react"
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Clock3,
  Maximize2,
  TerminalSquare,
  UserRound,
  Wrench,
} from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import CSSTurnDAG from "@/features/kanban/components/dag/CSSTurnDAG"
import type { ExecutionDagRaw, MessageStepRaw } from "@/features/kanban/types/dag"
import { getCurrentUserId, requestJson } from "@/lib/api"
import { formatErrorForDisplay } from "@/lib/errors"
import type { CompletedExecutionStep } from "@/types"
import {
  formatDuration,
  formatElapsed,
  friendlyStepDetail,
  friendlyStepTitle,
  operationLabel,
  summarizeToolInput,
  summarizeToolOutput,
} from "@/features/chat/execution-display"

interface TurnDAGSidebarProps {
  threadId: string | null
  isStreaming: boolean
  requestId?: string | null
  liveSteps: CompletedExecutionStep[]
}

type TimelineStep = {
  id: string
  order: number
  type: "human" | "ai" | "tool"
  title: string
  status: string
  detail: string
  metadata: Array<{ label: string; value: string }>
  error?: string | null
}

function useDagByRequestId(threadId: string | null, requestId: string | null | undefined) {
  const [dag, setDag] = useState<ExecutionDagRaw | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    async function fetchDag() {
      try {
        const userId = getCurrentUserId()
        if (!userId) throw new Error("尚未选择用户")
        const response = await requestJson<ExecutionDagRaw>(
          `/traces/${threadId}/dag/${requestId}?user_id=${encodeURIComponent(userId)}`,
          { signal: controller.signal },
        )
        setDag(response)
      } catch (fetchError) {
        if (fetchError instanceof Error && fetchError.name === "AbortError") return
        setError(formatErrorForDisplay(fetchError, "执行记录读取失败"))
      } finally {
        setLoading(false)
      }
    }

    const timer = window.setTimeout(() => {
      if (!threadId || !requestId) {
        setDag(null)
        setError(null)
        setLoading(false)
        return
      }
      setLoading(true)
      setError(null)
      void fetchDag()
    }, 0)

    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [threadId, requestId])

  return { dag, loading, error }
}

function textPreview(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value.trim()
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return fallback
    }
  }
  return fallback
}

function toolStatusLabel(status?: string | null, error?: string | null): string {
  if (error || status === "failed" || status === "error") return "失败"
  if (status === "running" || status === "pending") return "执行中"
  if (status === "skipped") return "已跳过"
  return "已完成"
}

function mapLegacyStep(step: MessageStepRaw, index: number): TimelineStep {
  if (step.message_type === "human") {
    return {
      id: `human-${step.step_number}-${index}`,
      order: step.step_number,
      type: "human",
      title: "接收用户请求",
      status: "已完成",
      detail: textPreview(step.content, "已接收本轮输入"),
      metadata: [],
    }
  }

  if (step.message_type === "tool") {
    const hasOutput = Boolean(step.tool_output?.trim())
    return {
      id: step.tool_call_id || `tool-${step.step_number}-${index}`,
      order: step.step_number,
      type: "tool",
      title: operationLabel(step.tool_name),
      status: toolStatusLabel(step.tool_status, step.tool_error),
      detail: hasOutput
        ? summarizeToolOutput(step.tool_output)
        : `处理内容：${summarizeToolInput(step.tool_args)}`,
      error: step.tool_error,
      metadata: [
        ...(step.latency_ms != null ? [{ label: "耗时", value: `${step.latency_ms} ms` }] : []),
        ...(step.tool_name ? [{ label: "技术操作", value: step.tool_name }] : []),
      ],
    }
  }

  const isThinking = Boolean(step.thinking)
  const hasTools = Boolean(step.tool_calls?.length)
  return {
    id: `ai-${step.step_number}-${index}`,
    order: step.step_number,
    type: "ai",
    title: isThinking ? "模型分析与推理" : hasTools ? "模型规划工具" : "生成回复",
    status: step.thinking_status === "running" ? "执行中" : "已完成",
    detail: textPreview(step.thinking || step.content, hasTools ? `已确认需要执行 ${step.tool_calls?.length ?? 0} 个步骤` : "回答内容已经整理完成"),
    metadata: [
      ...(step.model_name ? [{ label: "模型", value: step.model_name }] : []),
      ...(step.latency_ms != null ? [{ label: "耗时", value: `${step.latency_ms} ms` }] : []),
      ...(hasTools ? [{ label: "工具", value: `${step.tool_calls?.length ?? 0} 个` }] : []),
    ],
  }
}

function buildTimeline(dag: ExecutionDagRaw | null): TimelineStep[] {
  if (!dag) return []
  if (dag.progress_steps?.length) {
    return dag.progress_steps
      .map(mapLiveStep)
      .sort((left, right) => left.order - right.order)
  }
  if (dag.steps?.length) {
    return dag.steps
      .map(mapLegacyStep)
      .sort((left, right) => left.order - right.order)
  }

  return (dag.execution_graph?.nodes ?? [])
    .slice()
    .sort((left, right) => left.order - right.order)
    .map((node, index) => ({
      id: node.node_id,
      order: node.step_number || index + 1,
      type: node.kind === "user" ? "human" : node.kind === "action" ? "tool" : "ai",
      title: node.kind === "action"
        ? operationLabel(node.operation || node.label)
        : node.label || (node.kind === "response" ? "生成回复" : "接收请求"),
      status: toolStatusLabel(node.status),
      detail: node.kind === "action"
        ? "该步骤已完成，展开可查看耗时和技术操作"
        : node.capability || "该步骤已写入执行记录",
      metadata: [
        ...(node.capability ? [{ label: "能力", value: operationLabel(node.capability) }] : []),
        ...(node.operation ? [{ label: "技术操作", value: node.operation }] : []),
      ],
    }))
}

function mapLiveStep(step: CompletedExecutionStep): TimelineStep {
  const status = step.status === "completed"
    ? "\u5df2\u5b8c\u6210"
    : step.status === "skipped"
      ? "\u5df2\u8df3\u8fc7"
      : step.status === "waiting"
        ? "\u6267\u884c\u4e2d"
        : "\u5931\u8d25"
  return {
    id: step.step_id,
    order: step.order,
    type: step.kind === "action" ? "tool" : "ai",
    title: friendlyStepTitle(step),
    status,
    detail: friendlyStepDetail(step),
    error: step.error,
    metadata: [
      ...(step.duration_ms != null ? [{ label: "\u8017\u65f6", value: formatDuration(step.duration_ms) ?? "-" }] : []),
    ],
  }
}

function StepIcon({ type, status }: Pick<TimelineStep, "type" | "status">) {
  if (status === "失败") return <AlertCircle className="size-4 text-destructive" aria-hidden="true" />
  if (status === "执行中") return <CircleDashed className="size-4 animate-spin text-primary" aria-hidden="true" />
  if (type === "human") return <UserRound className="size-4" aria-hidden="true" />
  if (type === "tool") return <Wrench className="size-4" aria-hidden="true" />
  return <Bot className="size-4" aria-hidden="true" />
}

export function TurnDAGSidebar({
  threadId,
  isStreaming,
  requestId,
  liveSteps,
}: TurnDAGSidebarProps) {
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set())
  const [streamStartedAt, setStreamStartedAt] = useState<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const { dag: fetchedDag, loading: fetchedLoading, error } = useDagByRequestId(threadId, requestId)
  const dag = isStreaming ? null : fetchedDag
  const loading = isStreaming ? liveSteps.length === 0 : fetchedLoading
  const steps = useMemo(
    () => isStreaming
      ? liveSteps.map(mapLiveStep).sort((left, right) => left.order - right.order)
      : buildTimeline(dag),
    [dag, isStreaming, liveSteps],
  )

  useEffect(() => {
    if (!isStreaming) {
      setStreamStartedAt(null)
      setElapsedSeconds(0)
      return
    }
    setStreamStartedAt((current) => current ?? Date.now())
  }, [isStreaming])

  useEffect(() => {
    if (!isStreaming || streamStartedAt == null) return
    const update = () => setElapsedSeconds(Math.floor((Date.now() - streamStartedAt) / 1000))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [isStreaming, streamStartedAt])

  useEffect(() => {
    if (!isStreaming || steps.length === 0) return
    const latestId = steps[steps.length - 1].id
    setExpandedSteps((current) => {
      if (current.size === 1 && current.has(latestId)) return current
      return new Set([latestId])
    })
  }, [isStreaming, steps])

  const toggleStep = (id: string) => {
    setExpandedSteps((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <>
      <section className="flex h-full min-h-0 flex-col overflow-hidden border border-border bg-card" data-od-id="turn-execution-timeline">
        <header className="flex min-h-12 items-center justify-between border-b border-border px-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <TerminalSquare className="size-4" aria-hidden="true" />
              执行步骤
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {isStreaming ? `实时更新 · 已用时 ${formatElapsed(elapsedSeconds)}` : "输入、处理与结果按实际顺序记录"}
            </p>
          </div>
          <div className="flex items-center gap-1">
            {steps.length > 0 && <span className="px-2 font-mono text-xs text-muted-foreground">{steps.length} 步</span>}
            <button
              type="button"
              onClick={() => setIsDialogOpen(true)}
              className="grid size-10 place-items-center text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title="打开完整执行图"
              aria-label="打开完整执行图"
              disabled={!dag}
            >
              <Maximize2 className="size-4" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {loading ? (
            <div className="space-y-3" role="status">
              <div className="flex items-center gap-2 text-sm font-medium"><CircleDashed className="size-4 animate-spin text-primary" />正在理解问题并准备执行…</div>
              {[0, 1, 2].map((item) => <div key={item} className="h-14 animate-pulse bg-muted/60" />)}
            </div>
          ) : error ? (
            <div className="border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
              <div className="flex items-center gap-2 font-medium"><AlertCircle className="size-4" />执行记录暂不可用</div>
              <p className="mt-1 text-xs leading-5">{error}</p>
            </div>
          ) : steps.length === 0 ? (
            <div className="py-10 text-center">
              <Clock3 className="mx-auto size-6 text-muted-foreground" aria-hidden="true" />
              <p className="mt-3 text-sm font-medium">本轮尚无执行记录</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">发送消息后，模型推理和工具调用会依次显示。</p>
            </div>
          ) : (
            <ol className="relative ml-3 border-l border-border">
              {steps.map((step, index) => {
                const expanded = expandedSteps.has(step.id)
                return (
                  <li key={step.id} className="relative pb-4 pl-5 last:pb-0">
                    <span className="absolute -left-[13px] top-1 grid size-6 place-items-center rounded-full border border-border bg-background text-foreground">
                      <StepIcon type={step.type} status={step.status} />
                    </span>
                    <button
                      type="button"
                      onClick={() => toggleStep(step.id)}
                      className="w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-expanded={expanded}
                    >
                      <div className="flex min-h-10 items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-[11px] text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                            <span className="truncate text-sm font-medium text-foreground">{step.title}</span>
                          </div>
                          <p className={`mt-1 text-xs leading-5 text-muted-foreground ${expanded ? "whitespace-pre-wrap break-words" : "line-clamp-2"}`}>{step.detail}</p>
                        </div>
                        <div className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
                          {step.status === "已完成" && <CheckCircle2 className="size-3.5 text-emerald-600" aria-hidden="true" />}
                          <span>{step.status}</span>
                          <ChevronDown className={`size-3.5 transition-transform ${expanded ? "rotate-180" : ""}`} aria-hidden="true" />
                        </div>
                      </div>
                    </button>
                    {expanded && (
                      <div className="mt-2 border-t border-border/70 pt-2">
                        {step.metadata.length > 0 && (
                          <dl className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
                            {step.metadata.map((item) => (
                              <div key={`${step.id}-${item.label}`} className="flex gap-2">
                                <dt className="text-muted-foreground">{item.label}</dt>
                                <dd className="min-w-0 break-all font-mono text-foreground">{item.value}</dd>
                              </div>
                            ))}
                          </dl>
                        )}
                        {step.error && <p className="mt-2 text-xs leading-5 text-destructive">{step.error}</p>}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          )}
        </div>
      </section>

      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="flex max-h-[88vh] w-[92vw] max-w-5xl flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle>本轮完整执行图</DialogTitle>
          </DialogHeader>
          <div className="min-h-[460px] flex-1 overflow-auto border border-border bg-muted/20">
            {dag && <CSSTurnDAG dag={dag} compact={false} className="min-h-[460px] w-full" />}
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
