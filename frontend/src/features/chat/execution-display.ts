import type { CompletedExecutionStep, ToolCallInfo } from "@/types"

const OPERATION_LABELS: Record<string, string> = {
  answer_question: "整理回答",
  book_search_v1: "查找图书信息",
  bookshelf_read_v1: "读取我的书架",
  build_research_report: "整理研究报告",
  cancel_active_task_v1: "停止当前任务",
  collect_research_sources: "汇总研究来源",
  conversation_read: "回顾近期对话",
  fetch_research_source: "阅读来源内容",
  forget_memory_v2: "更新长期记忆",
  get_recommendation_history: "检查历史推荐",
  plan_recommendation_research_workflow: "规划推荐研究",
  process_memory_write_request: "保存用户信息",
  read_research_state: "读取研究进度",
  record_book_feedback: "更新阅读记录",
  remember_memory_v2: "保存长期记忆",
  research_read_v1: "读取研究结果",
  research_report_v1: "生成研究报告",
  research_state_tool: "读取研究进度",
  search_books: "查找图书",
  search_memory: "查找长期记忆",
  search_memory_v2: "查找长期记忆",
  search_research_sources: "检索研究来源",
  start_research: "创建研究任务",
  weather_get_v1: "查询天气",
  web_search: "搜索公开信息",
  web_search_v2: "搜索公开信息",
}

const PRIVATE_KEYS = new Set([
  "user_id",
  "thread_id",
  "request_id",
  "message_id",
  "conversation_id",
  "source_event_id",
  "idempotency_key",
])

const PREFERRED_INPUT_KEYS = [
  "query",
  "q",
  "title",
  "book_title",
  "objective",
  "question",
  "content",
  "status",
  "evaluation",
  "keywords",
]

const PREFERRED_OUTPUT_KEYS = [
  "message",
  "summary",
  "conclusion",
  "answer",
  "reason",
  "next_action_hint",
]

function cleanText(value: unknown): string {
  if (typeof value !== "string") return ""
  return value.replace(/\s+/g, " ").trim()
}

function truncate(value: string, limit = 180): string {
  return value.length > limit ? `${value.slice(0, limit - 1).trimEnd()}…` : value
}

function humanizeIdentifier(value: string): string {
  const normalized = value
    .replace(/_v\d+$/i, "")
    .replace(/[-_]+/g, " ")
    .trim()
  if (!normalized) return "执行任务"
  return normalized
    .split(" ")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ")
}

export function operationLabel(value?: string | null): string {
  const operation = cleanText(value)
  if (!operation) return "执行任务"
  const normalized = operation.toLowerCase()
  return OPERATION_LABELS[normalized] ?? humanizeIdentifier(operation)
}

export function formatDuration(durationMs?: number | null): string | null {
  if (durationMs == null || !Number.isFinite(durationMs)) return null
  if (durationMs < 1000) return `${Math.max(0, Math.round(durationMs))} 毫秒`
  const seconds = durationMs / 1000
  return seconds < 10 ? `${seconds.toFixed(1)} 秒` : `${Math.round(seconds)} 秒`
}

export function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`
}

function formatScalar(value: unknown): string {
  if (typeof value === "string") return truncate(cleanText(value), 100)
  if (typeof value === "number") return String(value)
  if (typeof value === "boolean") return value ? "是" : "否"
  if (Array.isArray(value)) {
    const textItems = value
      .map((item) => cleanText(item))
      .filter(Boolean)
      .slice(0, 3)
    return textItems.length ? textItems.join("、") : `${value.length} 项`
  }
  return ""
}

export function summarizeToolInput(args?: Record<string, unknown> | null): string {
  if (!args || Object.keys(args).length === 0) return "已准备执行所需信息"

  for (const key of PREFERRED_INPUT_KEYS) {
    const formatted = formatScalar(args[key])
    if (formatted) return formatted
  }

  const visibleEntries = Object.entries(args)
    .filter(([key]) => !PRIVATE_KEYS.has(key))
    .map(([, value]) => formatScalar(value))
    .filter(Boolean)
    .slice(0, 2)
  return visibleEntries.length ? visibleEntries.join(" · ") : "已准备执行所需信息"
}

function parseOutput(output?: string | null): unknown {
  const text = cleanText(output)
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return text
  }
}

function countLabel(record: Record<string, unknown>): string {
  const candidates: Array<[string, string]> = [
    ["result_count", "项结果"],
    ["evidence_count", "条证据"],
    ["source_count", "个来源"],
    ["count", "项结果"],
  ]
  for (const [key, suffix] of candidates) {
    const value = Number(record[key])
    if (Number.isFinite(value)) return `${value} ${suffix}`
  }
  return ""
}

function titleList(record: Record<string, unknown>): string {
  const collections = [record.books, record.results, record.items, record.records]
  for (const collection of collections) {
    if (!Array.isArray(collection) || collection.length === 0) continue
    const titles = collection
      .map((item) => {
        if (typeof item === "string") return cleanText(item)
        if (!item || typeof item !== "object") return ""
        const value = item as Record<string, unknown>
        return cleanText(value.book_title) || cleanText(value.title) || cleanText(value.name)
      })
      .filter(Boolean)
      .slice(0, 3)
    if (titles.length) {
      const remaining = collection.length - titles.length
      return `${titles.join("、")}${remaining > 0 ? ` 等 ${collection.length} 项` : ""}`
    }
    return `${collection.length} 项结果`
  }
  return ""
}

function summarizeBookEvidence(record: Record<string, unknown>): string {
  if (record.result_mode !== "book_evidence") return ""
  const items = Array.isArray(record.items) ? record.items : []
  const candidateCount = Number.isFinite(Number(record.candidate_count))
    ? Number(record.candidate_count)
    : items.length
  if (candidateCount <= 0) return "没有找到通过书架排除校验的新候选"

  const publicUrls = new Set<string>()
  items.forEach((raw) => {
    if (!raw || typeof raw !== "object") return
    const sources = (raw as Record<string, unknown>).evidence_sources
    if (!Array.isArray(sources)) return
    sources.forEach((source) => {
      if (!source || typeof source !== "object") return
      const url = cleanText((source as Record<string, unknown>).url)
      if (url) publicUrls.add(url)
    })
  })

  const themeParts = (Array.isArray(record.coverage) ? record.coverage : [])
    .map((raw) => {
      if (!raw || typeof raw !== "object") return ""
      const entry = raw as Record<string, unknown>
      const theme = cleanText(entry.theme)
      const count = Number(entry.candidate_count)
      if (!theme) return ""
      if (Number.isFinite(count) && count > 0) return `${theme} ${count} 本`
      return cleanText(entry.status) === "missing" ? `${theme} 暂无合适候选` : ""
    })
    .filter(Boolean)

  const parts = [`已筛选 ${candidateCount} 本候选`]
  if (publicUrls.size > 0) parts.push(`核对 ${publicUrls.size} 个公开页面`)
  if (themeParts.length > 0) parts.push(`主题覆盖：${themeParts.join("、")}`)
  return parts.join("；")
}

export function summarizeToolOutput(output?: string | null): string {
  const parsed = parseOutput(output)
  if (parsed == null) return "等待返回结果"
  if (typeof parsed === "string") return truncate(parsed)
  if (Array.isArray(parsed)) return `已返回 ${parsed.length} 项结果`
  if (typeof parsed !== "object") return truncate(String(parsed))

  const record = parsed as Record<string, unknown>
  const bookSummary = summarizeBookEvidence(record)
  if (bookSummary) return truncate(bookSummary)
  for (const key of PREFERRED_OUTPUT_KEYS) {
    const value = cleanText(record[key])
    if (value) return truncate(value)
  }

  const titles = titleList(record)
  if (titles) return `已找到：${titles}`
  const count = countLabel(record)
  if (count) return `已返回 ${count}`

  const status = cleanText(record.status)
  if (status) {
    const statusLabels: Record<string, string> = {
      completed: "已完成",
      success: "已完成",
      ok: "已完成",
      empty_result: "未找到匹配结果",
      failed: "执行失败",
      blocked: "因安全检查暂停",
    }
    return statusLabels[status.toLowerCase()] ?? `状态：${status}`
  }
  return `已返回 ${Object.keys(record).length} 类信息`
}

export function friendlyStepTitle(step: CompletedExecutionStep): string {
  if (
    step.operation === "remember_memory_v2"
    && /书架|阅读记录/.test(cleanText(step.detail))
  ) {
    return "同步阅读记录"
  }
  if (step.operation) return operationLabel(step.operation)
  const title = cleanText(step.title)
  if (title === "模型调用完成" && /规划|执行动作/.test(step.detail)) {
    return "理解任务并制定步骤"
  }
  if (title === "模型调用完成") return "理解并处理问题"
  if (/^执行\s+[-\w.]+$/i.test(title)) {
    return operationLabel(title.replace(/^执行\s+/i, ""))
  }
  return title || (step.kind === "research" ? "推进研究任务" : "完成处理步骤")
}

export function friendlyStepDetail(step: CompletedExecutionStep): string {
  const detail = cleanText(step.detail)
  if (!detail) return step.status === "completed" ? "这一步已经完成" : "正在处理"
  const statusMatch = detail.match(/^status:\s*([^;]+)(?:;\s*)?(.*)$/i)
  if (statusMatch) {
    const rest = statusMatch[2]?.trim()
    if (rest) return truncate(rest.replace(/\b(message|summary|conclusion|count):\s*/gi, ""))
    return summarizeToolOutput(JSON.stringify({ status: statusMatch[1] }))
  }
  return truncate(detail.replace(/\b(message|summary|conclusion|count):\s*/gi, ""))
}

export function toolDisplay(tool: ToolCallInfo): {
  title: string
  input: string
  result: string
} {
  return {
    title: operationLabel(tool.name),
    input: summarizeToolInput(tool.args),
    result: tool.output
      ? summarizeToolOutput(tool.output)
      : tool.status === "completed"
        ? "执行完成，结果已用于回答"
        : "等待返回结果",
  }
}

export function technicalToolDetails(tool: ToolCallInfo): string {
  return JSON.stringify(
    {
      operation: tool.name,
      input: tool.args,
      output: parseOutput(tool.output),
    },
    null,
    2,
  )
}
