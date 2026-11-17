import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { editMemory, forgetMemory, getMemoryCurrent, getMemoryHistory } from "@/lib/api"
import { formatErrorForDisplay } from "@/lib/errors"
import type { MemoryAdminFact } from "@/types"

type MemoryManagementDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  userId: string | null
}

type EditField = {
  key: string
  label: string
  value: string
}

const POLARITIES = [
  { value: "like", label: "喜欢" },
  { value: "dislike", label: "不喜欢" },
  { value: "want", label: "想要" },
  { value: "avoid", label: "避免" },
  { value: "neutral", label: "中性" },
]

const CATEGORY_ORDER = [
  "identity",
  "preference",
  "possession",
  "relationship",
  "agreement",
  "feedback",
  "background",
  "state",
  "fact",
  "reading",
]

function formatDate(value: string | null): string {
  if (!value) return "时间未知"
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
}

function categoryLabel(categoryKey: string): string {
  const labels: Record<string, string> = {
    identity: "身份与称呼",
    preference: "偏好",
    possession: "物品与宠物",
    relationship: "关系与名称",
    agreement: "约定",
    feedback: "反馈",
    background: "能力与背景",
    state: "状态",
    fact: "其他事实",
    reading: "阅读记忆",
  }
  return labels[categoryKey] ?? "其他事实"
}

const READING_STATUS_LABELS: Record<string, string> = {
  want_to_read: "想读",
  reading: "在读",
  read: "已读",
  dropped: "已放弃",
}

const READING_EVALUATION_LABELS: Record<string, string> = {
  liked: "喜欢",
  neutral: "一般",
  disliked: "不喜欢",
  not_interested: "不感兴趣",
}

function mergeReadingFacts(facts: MemoryAdminFact[]): MemoryAdminFact[] {
  const ordinary: MemoryAdminFact[] = []
  const readingGroups = new Map<string, MemoryAdminFact[]>()

  facts.forEach((fact) => {
    if (fact.category_key !== "reading") {
      ordinary.push(fact)
      return
    }
    const title = String(fact.value.book_title ?? "").trim()
    const entityId = String(fact.value.entity_id ?? "").trim()
    const groupKey = entityId || title.toLocaleLowerCase() || fact.memory_key
    readingGroups.set(groupKey, [...(readingGroups.get(groupKey) ?? []), fact])
  })

  const reading = [...readingGroups.entries()].map(([groupKey, group]) => {
    const ordered = [...group].sort(
      (left, right) => new Date(right.valid_from).getTime() - new Date(left.valid_from).getTime(),
    )
    const newest = ordered[0]
    const state = ordered.find((fact) => fact.schema_key === "reading.state")
    const evaluation = ordered.find((fact) => fact.schema_key === "reading.feedback")
    const title = String(
      state?.value.book_title ?? evaluation?.value.book_title ?? newest.value.book_title ?? "",
    ).trim()
    const status = String(state?.value.reading_status ?? "").trim()
    const evaluationValue = String(evaluation?.value.evaluation ?? "").trim()
    const details = [
      status ? (READING_STATUS_LABELS[status] ?? status) : "",
      evaluationValue ? (READING_EVALUATION_LABELS[evaluationValue] ?? evaluationValue) : "",
    ].filter(Boolean)

    return {
      ...newest,
      schema_key: "reading.summary",
      memory_key: `reading.summary:${groupKey}`,
      presentation_key: "reading.summary",
      value: {
        ...newest.value,
        book_title: title,
        reading_status: status || null,
        evaluation: evaluationValue || null,
      },
      evidence_quote: "",
      version_no: Math.max(...ordered.map((fact) => fact.version_no)),
      display_value: `${title ? `《${title}》` : "阅读记录"}${details.length ? ` · ${details.join(" · ")}` : ""}`,
      can_edit: false,
      can_forget: false,
      show_evidence: false,
    }
  })

  return [...ordinary, ...reading].sort(
    (left, right) => new Date(right.valid_from).getTime() - new Date(left.valid_from).getTime(),
  )
}

function presentationKey(fact: MemoryAdminFact): string {
  return fact.presentation_key || fact.schema_key
}

function editPredicate(fact: MemoryAdminFact): string {
  const predicates: Record<string, string> = {
    "identity.self_reported_name": "name",
    "identity.preferred_address": "call_me",
    "identity.alias": "alias",
    "possession.entity": "has",
    "preference.entity": "prefers",
    "relationship.entity": "relationship",
    "instruction.behavior": "instruction",
    "feedback.outcome": "feedback",
    "temporary.state": "state",
    "entity.name": "entity_name",
  }
  return predicates[presentationKey(fact)] ?? fact.predicate ?? fact.schema_key
}

function factValueLabel(fact: MemoryAdminFact): string {
  if (fact.display_value?.trim()) return fact.display_value.trim()
  const value = fact.value
  const key = presentationKey(fact)
  if (key === "identity.self_reported_name") return String(value.name ?? "")
  if (key === "identity.preferred_address") return String(value.address ?? "")
  if (key === "identity.alias") return String(value.alias ?? "")

  const entity = String(value.entity ?? "")
  if (key === "possession.entity") return entity
  if (key === "preference.entity") {
    const verbs: Record<string, string> = {
      like: "喜欢",
      dislike: "不喜欢",
      want: "想要",
      avoid: "避免",
      neutral: "关注",
    }
    return entity ? `${verbs[String(value.polarity ?? "neutral")] ?? "关注"} ${entity}` : ""
  }
  if (key === "relationship.entity") {
    return entity && value.relation ? `${entity}（${String(value.relation)}）` : entity
  }
  if (key === "entity.name") {
    return entity && value.name ? `${entity}叫${String(value.name)}` : entity
  }
  if (key === "instruction.behavior") return String(value.instruction ?? "")
  if (key === "feedback.outcome") {
    return value.target && value.outcome
      ? `${String(value.target)}：${String(value.outcome)}`
      : String(value.target ?? value.outcome ?? "")
  }
  if (key === "temporary.state") {
    return value.state && value.value
      ? `${String(value.state)} = ${String(value.value)}`
      : String(value.state ?? value.value ?? "")
  }
  return fact.evidence_quote || fact.memory_key
}

function editFieldsFor(fact: MemoryAdminFact): EditField[] {
  const value = fact.value
  const fields: Record<string, EditField[]> = {
    "identity.self_reported_name": [
      { key: "name", label: "名字", value: String(value.name ?? "") },
    ],
    "identity.preferred_address": [
      { key: "address", label: "称呼", value: String(value.address ?? "") },
    ],
    "identity.alias": [
      { key: "alias", label: "别名", value: String(value.alias ?? "") },
    ],
    "possession.entity": [
      { key: "entity", label: "物品或宠物", value: String(value.entity ?? "") },
    ],
    "preference.entity": [
      { key: "entity", label: "对象", value: String(value.entity ?? "") },
      { key: "polarity", label: "态度", value: String(value.polarity ?? "like") },
    ],
    "relationship.entity": [
      { key: "entity", label: "对象", value: String(value.entity ?? "") },
      { key: "relation", label: "关系", value: String(value.relation ?? "") },
    ],
    "entity.name": [
      { key: "entity", label: "对象", value: String(value.entity ?? "") },
      { key: "name", label: "名称", value: String(value.name ?? "") },
    ],
    "instruction.behavior": [
      { key: "instruction", label: "约定", value: String(value.instruction ?? "") },
    ],
    "feedback.outcome": [
      { key: "target", label: "目标", value: String(value.target ?? "") },
      { key: "outcome", label: "结果", value: String(value.outcome ?? "") },
    ],
    "temporary.state": [
      {
        key: "value",
        label: String(value.state ?? "") === "skill" ? "技能" : "状态内容",
        value: String(value.value ?? ""),
      },
    ],
  }
  return fields[presentationKey(fact)] ?? []
}

function buildEditValue(fact: MemoryAdminFact, values: Record<string, string>): Record<string, unknown> {
  const key = presentationKey(fact)
  if (key === "identity.self_reported_name") return { name: values.name }
  if (key === "identity.preferred_address") return { address: values.address }
  if (key === "identity.alias") return { alias: values.alias }
  if (key === "possession.entity") return { entity: values.entity }
  if (key === "preference.entity") {
    return { entity: values.entity, polarity: values.polarity }
  }
  if (key === "relationship.entity") {
    return { entity: values.entity, relation: values.relation }
  }
  if (key === "entity.name") return { entity: values.entity, name: values.name }
  if (key === "instruction.behavior") return { instruction: values.instruction }
  if (key === "feedback.outcome") {
    return { target: values.target, outcome: values.outcome }
  }
  if (key === "temporary.state") {
    return { state: fact.value.state, value: values.value }
  }
  return fact.value
}

function forgetIdentity(fact: MemoryAdminFact): Record<string, unknown> {
  const value = fact.value
  const key = presentationKey(fact)
  if (["possession.entity", "preference.entity", "relationship.entity", "entity.name"].includes(key)) {
    return { entity: String(value.entity ?? "") }
  }
  if (key === "instruction.behavior") return { instruction: String(value.instruction ?? "") }
  if (key === "feedback.outcome") return { target: String(value.target ?? "") }
  if (key === "temporary.state") return { state: String(value.state ?? "") }
  if (key === "identity.alias") return { alias: String(value.alias ?? "") }
  return {}
}

export function MemoryManagementDialog({
  open,
  onOpenChange,
  userId,
}: MemoryManagementDialogProps) {
  const [facts, setFacts] = useState<MemoryAdminFact[]>([])
  const [activeCategory, setActiveCategory] = useState("all")
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editValues, setEditValues] = useState<Record<string, string>>({})
  const [openHistory, setOpenHistory] = useState<Set<string>>(new Set())
  const [historyData, setHistoryData] = useState<Record<string, MemoryAdminFact[]>>({})
  const [historyLoading, setHistoryLoading] = useState<Set<string>>(new Set())
  const [historyErrors, setHistoryErrors] = useState<Record<string, string>>({})
  const [busyKey, setBusyKey] = useState<string | null>(null)

  const sortedFacts = useMemo(
    () => mergeReadingFacts(facts),
    [facts],
  )

  const categories = useMemo(() => {
    const counts = new Map<string, number>()
    sortedFacts.forEach((fact) => counts.set(fact.category_key, (counts.get(fact.category_key) ?? 0) + 1))
    return [...counts.entries()].sort(([left], [right]) => {
      const leftIndex = CATEGORY_ORDER.indexOf(left)
      const rightIndex = CATEGORY_ORDER.indexOf(right)
      return (leftIndex < 0 ? 999 : leftIndex) - (rightIndex < 0 ? 999 : rightIndex)
    })
  }, [sortedFacts])

  const categoryLabels = useMemo(
    () => new Map(sortedFacts.map((fact) => [fact.category_key, fact.category_label])),
    [sortedFacts],
  )

  const displayCategoryLabel = useCallback(
    (categoryKey: string) => categoryLabels.get(categoryKey) || categoryLabel(categoryKey),
    [categoryLabels],
  )

  const visibleFacts = useMemo(
    () => activeCategory === "all" ? sortedFacts : sortedFacts.filter((fact) => fact.category_key === activeCategory),
    [activeCategory, sortedFacts],
  )

  const loadFacts = useCallback(async () => {
    if (!userId) {
      setFacts([])
      return
    }
    setIsLoading(true)
    setError(null)
    try {
      const result = await getMemoryCurrent(userId)
      setFacts(result.facts)
      setOpenHistory(new Set())
      setHistoryData({})
      setHistoryLoading(new Set())
      setHistoryErrors({})
    } catch (loadError) {
      setError(formatErrorForDisplay(loadError, "无法读取当前记忆"))
    } finally {
      setIsLoading(false)
    }
  }, [userId])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => void loadFacts(), 0)
    return () => window.clearTimeout(timer)
  }, [loadFacts, open])

  useEffect(() => {
    if (activeCategory !== "all" && !categories.some(([key]) => key === activeCategory)) {
      setActiveCategory("all")
    }
  }, [activeCategory, categories])

  const startEdit = (fact: MemoryAdminFact) => {
    setEditingKey(fact.memory_key)
    setEditValues(Object.fromEntries(editFieldsFor(fact).map((field) => [field.key, field.value])))
    setNotice(null)
  }

  const saveEdit = async (fact: MemoryAdminFact) => {
    if (!userId) return
    setBusyKey(fact.memory_key)
    setError(null)
    setNotice(null)
    try {
      const receipt = await editMemory({
        user_id: userId,
        memory_key: fact.memory_key,
        predicate: editPredicate(fact),
        value: buildEditValue(fact, editValues),
        qualifiers: fact.qualifiers,
      })
      const status = receipt.mutations?.[0]?.status
      setNotice(
        status === "revised"
          ? "记忆已更新，旧值仍保留在版本历史中。"
          : status === "noop_duplicate"
            ? "内容与当前记忆相同，没有创建新版本。"
            : "记忆已保存。",
      )
      setEditingKey(null)
      await loadFacts()
    } catch (editError) {
      setError(formatErrorForDisplay(editError, "保存记忆失败"))
    } finally {
      setBusyKey(null)
    }
  }

  const toggleHistory = async (fact: MemoryAdminFact) => {
    if (!userId) return
    if (openHistory.has(fact.memory_key)) {
      setOpenHistory((current) => {
        const next = new Set(current)
        next.delete(fact.memory_key)
        return next
      })
      return
    }
    setOpenHistory((current) => new Set(current).add(fact.memory_key))
    if (!historyData[fact.memory_key]) {
      setHistoryLoading((current) => new Set(current).add(fact.memory_key))
      setHistoryErrors((current) => {
        const next = { ...current }
        delete next[fact.memory_key]
        return next
      })
      try {
        const result = await getMemoryHistory(userId, fact.memory_key)
        setHistoryData((current) => ({ ...current, [fact.memory_key]: result.versions }))
      } catch (historyError) {
        const message = formatErrorForDisplay(historyError, "无法读取版本历史")
        setError(message)
        setHistoryErrors((current) => ({ ...current, [fact.memory_key]: message }))
      } finally {
        setHistoryLoading((current) => {
          const next = new Set(current)
          next.delete(fact.memory_key)
          return next
        })
      }
    }
  }

  const handleForget = async (fact: MemoryAdminFact) => {
    if (!userId || !window.confirm("确定遗忘这条记忆吗？历史记录会保留遗忘标记。")) return
    setBusyKey(fact.memory_key)
    setError(null)
    try {
      await forgetMemory({
        user_id: userId,
        memory_key: fact.memory_key,
        predicate: editPredicate(fact),
        identity: forgetIdentity(fact),
        qualifiers: fact.qualifiers,
      })
      setNotice("该条记忆已标记为遗忘。")
      await loadFacts()
    } catch (forgetError) {
      setError(formatErrorForDisplay(forgetError, "遗忘记忆失败"))
    } finally {
      setBusyKey(null)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-hidden p-0 sm:max-w-5xl" data-od-id="memory-center-dialog">
        <DialogHeader className="border-b border-border px-6 pb-5 pt-6">
          <div className="flex items-start justify-between gap-5 pr-8">
            <div>
              <DialogTitle className="flex items-center gap-2 text-2xl">
                <Brain className="size-5 text-primary" aria-hidden="true" />
                记忆中心
              </DialogTitle>
              <DialogDescription className="mt-2 max-w-2xl text-sm leading-6">
                查看系统正在使用的事实，必要时编辑、回看版本或明确遗忘。修改会保留版本链，不会静默覆盖。
              </DialogDescription>
            </div>
            <Button type="button" variant="outline" onClick={() => void loadFacts()} disabled={isLoading || !userId}>
              <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} aria-hidden="true" />
              刷新
            </Button>
          </div>
        </DialogHeader>

        <div className="grid min-h-0 grid-cols-1 md:grid-cols-[190px_minmax(0,1fr)]">
          <aside className="border-b border-border bg-muted/20 p-3 md:border-b-0 md:border-r" aria-label="记忆分类">
            <button
              type="button"
              onClick={() => setActiveCategory("all")}
              className={`flex min-h-11 w-full items-center justify-between px-3 text-left text-sm transition-colors ${activeCategory === "all" ? "bg-background font-medium text-foreground shadow-sm" : "text-muted-foreground hover:bg-background hover:text-foreground"}`}
            >
              <span>全部记忆</span>
              <span className="font-mono text-xs">{sortedFacts.length}</span>
            </button>
            {categories.map(([schemaKey, count]) => (
              <button
                key={schemaKey}
                type="button"
                onClick={() => setActiveCategory(schemaKey)}
                className={`mt-1 flex min-h-11 w-full items-center justify-between px-3 text-left text-sm transition-colors ${activeCategory === schemaKey ? "bg-background font-medium text-foreground shadow-sm" : "text-muted-foreground hover:bg-background hover:text-foreground"}`}
              >
                <span>{displayCategoryLabel(schemaKey)}</span>
                <span className="font-mono text-xs">{count}</span>
              </button>
            ))}
          </aside>

          <section className="min-w-0 overflow-y-auto" style={{ maxHeight: "68vh" }} data-od-id="memory-fact-list">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-background/95 px-5 py-3 backdrop-blur">
              <p className="text-sm font-medium">{activeCategory === "all" ? "当前记忆" : displayCategoryLabel(activeCategory)}</p>
              <p className="font-mono text-xs text-muted-foreground">{visibleFacts.length} 条</p>
            </div>

            {error && <div className="m-4 border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" role="alert">{error}</div>}
            {notice && <div className="m-4 border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800" role="status">{notice}</div>}

            {isLoading && sortedFacts.length === 0 ? (
              <div className="px-6 py-16 text-center text-sm text-muted-foreground">正在读取记忆…</div>
            ) : visibleFacts.length === 0 ? (
              <div className="px-6 py-16 text-center">
                <Brain className="mx-auto size-7 text-muted-foreground" aria-hidden="true" />
                <p className="mt-3 text-sm font-medium">当前分类没有可用记忆</p>
                <p className="mt-1 text-sm text-muted-foreground">对话中确认过的长期事实会显示在这里。</p>
              </div>
            ) : (
              visibleFacts.map((fact) => {
                const editing = editingKey === fact.memory_key
                const historyOpen = openHistory.has(fact.memory_key)
                const versions = historyData[fact.memory_key] ?? []
                const mergedHistory = new Set(versions.map((version) => version.memory_key)).size > 1
                const loadingHistory = historyLoading.has(fact.memory_key)
                const historyError = historyErrors[fact.memory_key]
                const editInvalid = editFieldsFor(fact).some((field) =>
                  !(editValues[field.key] ?? field.value).trim(),
                )
                return (
                  <article key={fact.memory_key} className="border-b border-border px-5 py-5" data-od-id={`memory-fact-${fact.memory_key}`}>
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="outline">{fact.category_label || displayCategoryLabel(fact.category_key)}</Badge>
                        </div>

                        {editing ? (
                          <div className="mt-4 space-y-3 border-y border-border py-4">
                            {editFieldsFor(fact).map((field) => (
                              <label key={field.key} className="grid gap-2 text-sm sm:grid-cols-[96px_minmax(0,1fr)] sm:items-center">
                                <span className="font-medium">{field.label}</span>
                                {field.key === "polarity" ? (
                                  <select
                                    className="h-11 border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                    value={editValues[field.key] ?? field.value}
                                    onChange={(event) => setEditValues((current) => ({ ...current, [field.key]: event.target.value }))}
                                  >
                                    {POLARITIES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                                  </select>
                                ) : (
                                  <input
                                    className="h-11 border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                    value={editValues[field.key] ?? field.value}
                                    onChange={(event) => setEditValues((current) => ({ ...current, [field.key]: event.target.value }))}
                                  />
                                )}
                              </label>
                            ))}
                            <div className="flex justify-end gap-2 pt-1">
                              <Button type="button" variant="outline" onClick={() => setEditingKey(null)} disabled={busyKey === fact.memory_key}>
                                <X className="size-4" aria-hidden="true" />取消
                              </Button>
                              <Button type="button" onClick={() => void saveEdit(fact)} disabled={busyKey === fact.memory_key || editInvalid}>
                                <Check className="size-4" aria-hidden="true" />保存
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <p className="mt-3 break-words text-base font-medium leading-7 text-foreground">{factValueLabel(fact)}</p>
                        )}

                        {fact.show_evidence && fact.evidence_quote && fact.evidence_quote.trim() !== factValueLabel(fact).trim() && !editing && (
                          <details className="mt-3 max-w-3xl text-sm text-muted-foreground">
                            <summary className="cursor-pointer select-none hover:text-foreground">来源片段</summary>
                            <blockquote className="mt-2 border-l border-border pl-3 leading-6">
                              “{fact.evidence_quote}”
                            </blockquote>
                          </details>
                        )}

                        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                          <span className="inline-flex items-center gap-1"><Clock3 className="size-3.5" aria-hidden="true" />更新于 {formatDate(fact.valid_from)}</span>
                          {fact.category_key !== "reading" ? <span className="font-mono">v{fact.version_no}</span> : null}
                        </div>
                      </div>

                      {!editing && (
                        <div className="flex shrink-0 flex-wrap gap-1">
                          {fact.category_key !== "reading" ? (
                            <Button type="button" variant="ghost" onClick={() => void toggleHistory(fact)} aria-expanded={historyOpen}>
                              {historyOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                              版本
                            </Button>
                          ) : null}
                          {fact.can_edit ? (
                            <Button type="button" variant="ghost" onClick={() => startEdit(fact)}>
                              <Pencil className="size-4" aria-hidden="true" />编辑
                            </Button>
                          ) : null}
                          {fact.can_forget ? (
                            <Button type="button" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => void handleForget(fact)} disabled={busyKey === fact.memory_key}>
                              <Trash2 className="size-4" aria-hidden="true" />遗忘
                            </Button>
                          ) : fact.category_key === "reading" ? (
                            <span className="self-center px-2 text-xs text-muted-foreground">在书架中管理</span>
                          ) : null}
                        </div>
                      )}
                    </div>

                    {historyOpen && (
                      <div className="mt-4 border-t border-border pt-4">
                        <p className="mb-3 text-sm font-medium">版本时间线</p>
                        {loadingHistory ? (
                          <p className="text-sm text-muted-foreground">正在读取版本…</p>
                        ) : historyError ? (
                          <p className="text-sm text-destructive">{historyError}；收起后可重试。</p>
                        ) : versions.length === 0 ? (
                          <p className="text-sm text-muted-foreground">暂无版本记录。</p>
                        ) : (
                          <ol className="space-y-3 border-l border-border pl-4">
                            {versions.map((version, versionIndex) => (
                              <li key={`${version.memory_key}-${version.version_no}`} className="relative text-sm">
                                <span className="absolute -left-[19px] top-1.5 size-2 rounded-full bg-muted-foreground" aria-hidden="true" />
                                <div className="flex flex-wrap items-baseline justify-between gap-2">
                                  <span className="font-medium">
                                    {mergedHistory ? `记录 ${versionIndex + 1}` : `v${version.version_no}`} · {factValueLabel(version)}
                                  </span>
                                  <span className="font-mono text-xs text-muted-foreground">{formatDate(version.valid_from)}</span>
                                </div>
                                <p className="mt-1 text-xs text-muted-foreground">
                                  {version.is_tombstone ? "已遗忘" : version.valid_to === null ? "当前版本" : `有效至 ${formatDate(version.valid_to)}`}
                                </p>
                              </li>
                            ))}
                          </ol>
                        )}
                      </div>
                    )}
                  </article>
                )
              })
            )}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  )
}
