import { useCallback, useEffect, useMemo, useState } from "react"
import { Brain, Check, ChevronDown, ChevronRight, Pencil, RefreshCw, Trash2, X } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { editMemory, forgetMemory, getMemoryCurrent, getMemoryHistory } from "@/lib/api"
import type { MemoryAdminFact } from "@/types"
import { useI18n } from "@/i18n"

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

const POLARITIES: { value: string; label: string }[] = [
  { value: "like", label: "喜欢" },
  { value: "dislike", label: "不喜欢" },
  { value: "want", label: "想要" },
  { value: "avoid", label: "避免" },
  { value: "neutral", label: "中性" },
]

function formatDate(value: string | null): string {
  if (!value) return ""
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString()
}

function categoryLabel(schemaKey: string): string {
  if (schemaKey === "identity.self_reported_name") return "名字"
  if (schemaKey === "identity.preferred_address") return "称呼"
  if (schemaKey === "identity.alias") return "别名"
  if (schemaKey === "possession.entity") return "物品"
  if (schemaKey === "preference.entity") return "喜好"
  if (schemaKey === "relationship.entity") return "关系"
  if (schemaKey === "instruction.behavior") return "约定"
  if (schemaKey === "feedback.outcome") return "反馈"
  if (schemaKey === "temporary.state") return "状态"
  return schemaKey
}

function editPredicate(fact: MemoryAdminFact): string {
  if (fact.schema_key === "identity.self_reported_name") return "name"
  if (fact.schema_key === "identity.preferred_address") return "call_me"
  if (fact.schema_key === "identity.alias") return "alias"
  if (fact.schema_key === "possession.entity") return "has"
  if (fact.schema_key === "preference.entity") return "prefers"
  if (fact.schema_key === "relationship.entity") return "relationship"
  if (fact.schema_key === "instruction.behavior") return "instruction"
  if (fact.schema_key === "feedback.outcome") return "feedback"
  if (fact.schema_key === "temporary.state") return "state"
  return fact.predicate || fact.schema_key
}

function factValueLabel(fact: MemoryAdminFact): string {
  const value = fact.value
  if (fact.schema_key === "identity.self_reported_name") return String(value.name ?? "")
  if (fact.schema_key === "identity.preferred_address") return String(value.address ?? "")
  if (fact.schema_key === "identity.alias") return String(value.alias ?? "")
  const entity = String(value.entity ?? "")
  if (fact.schema_key === "possession.entity") return entity
  if (fact.schema_key === "preference.entity") {
    const polarity = String(value.polarity ?? "")
    const verb =
      polarity === "like" ? "喜欢" :
      polarity === "dislike" ? "不喜欢" :
      polarity === "want" ? "想要" :
      polarity === "avoid" ? "想避免" : "偏好"
    return entity ? `${verb} ${entity}` : ""
  }
  if (fact.schema_key === "relationship.entity") {
    return entity && value.relation ? `${entity}（${String(value.relation)}）` : entity
  }
  if (fact.schema_key === "instruction.behavior") return String(value.instruction ?? "")
  if (fact.schema_key === "feedback.outcome") {
    return value.target && value.outcome
      ? `${String(value.target)}：${String(value.outcome)}`
      : String(value.target ?? value.outcome ?? "")
  }
  if (fact.schema_key === "temporary.state") {
    return value.state && value.value
      ? `${String(value.state)} = ${String(value.value)}`
      : String(value.state ?? value.value ?? "")
  }
  return fact.evidence_quote || ""
}

function editFieldsFor(fact: MemoryAdminFact): EditField[] {
  const value = fact.value
  if (fact.schema_key === "identity.self_reported_name") {
    return [{ key: "name", label: "名字", value: String(value.name ?? "") }]
  }
  if (fact.schema_key === "identity.preferred_address") {
    return [{ key: "address", label: "称呼", value: String(value.address ?? "") }]
  }
  if (fact.schema_key === "identity.alias") {
    return [{ key: "alias", label: "别名", value: String(value.alias ?? "") }]
  }
  if (fact.schema_key === "possession.entity") {
    return [{ key: "entity", label: "物品/宠物", value: String(value.entity ?? "") }]
  }
  if (fact.schema_key === "preference.entity") {
    return [
      { key: "entity", label: "对象", value: String(value.entity ?? "") },
      { key: "polarity", label: "态度", value: String(value.polarity ?? "like") },
    ]
  }
  if (fact.schema_key === "relationship.entity") {
    return [
      { key: "entity", label: "对象", value: String(value.entity ?? "") },
      { key: "relation", label: "关系", value: String(value.relation ?? "") },
    ]
  }
  if (fact.schema_key === "instruction.behavior") {
    return [{ key: "instruction", label: "约定", value: String(value.instruction ?? "") }]
  }
  if (fact.schema_key === "feedback.outcome") {
    return [
      { key: "target", label: "目标", value: String(value.target ?? "") },
      { key: "outcome", label: "结果", value: String(value.outcome ?? "") },
    ]
  }
  if (fact.schema_key === "temporary.state") {
    return [
      { key: "state", label: "状态键", value: String(value.state ?? "") },
      { key: "value", label: "状态值", value: String(value.value ?? "") },
    ]
  }
  return []
}

function buildEditValue(fact: MemoryAdminFact, fields: EditField[]): Record<string, unknown> {
  const byKey = Object.fromEntries(fields.map((field) => [field.key, field.value]))
  if (fact.schema_key === "identity.self_reported_name") return { name: byKey.name }
  if (fact.schema_key === "identity.preferred_address") return { address: byKey.address }
  if (fact.schema_key === "identity.alias") return { alias: byKey.alias }
  if (fact.schema_key === "possession.entity") return { entity: byKey.entity }
  if (fact.schema_key === "preference.entity") {
    return { entity: byKey.entity, polarity: byKey.polarity }
  }
  if (fact.schema_key === "relationship.entity") {
    return { entity: byKey.entity, relation: byKey.relation }
  }
  if (fact.schema_key === "instruction.behavior") return { instruction: byKey.instruction }
  if (fact.schema_key === "feedback.outcome") {
    return { target: byKey.target, outcome: byKey.outcome }
  }
  if (fact.schema_key === "temporary.state") {
    return { state: byKey.state, value: byKey.value }
  }
  return {}
}

function forgetIdentity(fact: MemoryAdminFact): Record<string, unknown> {
  const value = fact.value
  if (fact.schema_key === "possession.entity" || fact.schema_key === "preference.entity" || fact.schema_key === "relationship.entity") {
    return { entity: String(value.entity ?? "") }
  }
  if (fact.schema_key === "instruction.behavior") return { instruction: String(value.instruction ?? "") }
  if (fact.schema_key === "feedback.outcome") return { target: String(value.target ?? "") }
  if (fact.schema_key === "temporary.state") return { state: String(value.state ?? "") }
  if (fact.schema_key === "identity.alias") return { alias: String(value.alias ?? "") }
  return {}
}

export function MemoryManagementDialog({
  open,
  onOpenChange,
  userId,
}: MemoryManagementDialogProps) {
  const { t } = useI18n()
  const [facts, setFacts] = useState<MemoryAdminFact[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editValues, setEditValues] = useState<Record<string, string>>({})
  const [openHistory, setOpenHistory] = useState<Set<string>>(new Set())
  const [historyData, setHistoryData] = useState<Record<string, MemoryAdminFact[]>>({})
  const [busyKey, setBusyKey] = useState<string | null>(null)

  const sortedFacts = useMemo(
    () =>
      [...facts].sort((left, right) =>
        new Date(right.valid_from).getTime() - new Date(left.valid_from).getTime(),
      ),
    [facts],
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
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : t("error.unexpected"))
    } finally {
      setIsLoading(false)
    }
  }, [t, userId])

  useEffect(() => {
    if (open) {
      void loadFacts()
    }
  }, [loadFacts, open])

  const startEdit = (fact: MemoryAdminFact) => {
    setEditingKey(fact.memory_key)
    setEditValues(Object.fromEntries(editFieldsFor(fact).map((field) => [field.key, field.value])))
    setNotice(null)
  }

  const saveEdit = async (fact: MemoryAdminFact) => {
    if (!userId) return
    const fields = editFieldsFor(fact).map((field) => ({
      ...field,
      value: editValues[field.key] ?? "",
    }))
    setBusyKey(fact.memory_key)
    setError(null)
    setNotice(null)
    try {
      const receipt = await editMemory({
        user_id: userId,
        predicate: editPredicate(fact),
        value: buildEditValue(fact, fields),
        qualifiers: fact.qualifiers,
      })
      const status = receipt.mutations?.[0]?.status
      setNotice(status === "revised" ? "已更新记忆（旧值保留在历史中）" : status === "noop_duplicate" ? "值与当前记忆相同，未产生新版本" : "已记录")
      setEditingKey(null)
      await loadFacts()
    } catch (editError) {
      const message = editError instanceof Error ? editError.message : String(editError)
      setError(message)
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
      try {
        const result = await getMemoryHistory(userId, fact.memory_key)
        setHistoryData((current) => ({ ...current, [fact.memory_key]: result.versions }))
      } catch (historyError) {
        setError(historyError instanceof Error ? historyError.message : t("error.unexpected"))
      }
    }
  }

  const handleForget = async (fact: MemoryAdminFact) => {
    if (!userId) return
    setBusyKey(fact.memory_key)
    setError(null)
    try {
      await forgetMemory({
        user_id: userId,
        predicate: editPredicate(fact),
        identity: forgetIdentity(fact),
        qualifiers: fact.qualifiers,
      })
      setNotice("已按你的要求将该条记忆标记为遗忘")
      await loadFacts()
    } catch (forgetError) {
      setError(forgetError instanceof Error ? forgetError.message : t("memory.forgetFailed"))
    } finally {
      setBusyKey(null)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Brain className="size-5 text-[var(--primary)]" />
            {t("memory.currentTitle")}
          </DialogTitle>
          <DialogDescription>{t("memory.currentDescription")}</DialogDescription>
        </DialogHeader>

        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-muted-foreground">
            {t("memory.total", { count: sortedFacts.length })}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void loadFacts()}
            disabled={isLoading || !userId}
          >
            <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
            {t("common.refresh")}
          </Button>
        </div>

        {error && (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-300">
            {error}
          </div>
        )}
        {notice && (
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
            {notice}
          </div>
        )}

        <div className="max-h-[55vh] overflow-y-auto pr-1">
          {isLoading && sortedFacts.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">{t("common.loading")}</div>
          ) : sortedFacts.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">{t("memory.currentEmpty")}</div>
          ) : (
            <div className="space-y-2">
              {sortedFacts.map((fact) => {
                const editing = editingKey === fact.memory_key
                const historyOpen = openHistory.has(fact.memory_key)
                const versions = historyData[fact.memory_key] ?? []
                return (
                  <div
                    key={fact.memory_key}
                    className="rounded-lg border border-border bg-background px-3 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1 space-y-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="outline">{categoryLabel(fact.schema_key)}</Badge>
                          <Badge variant="secondary">{fact.schema_key}</Badge>
                        </div>
                        {editing ? (
                          <div className="space-y-2">
                            {editFieldsFor(fact).map((field) => (
                              <div key={field.key} className="flex items-center gap-2">
                                <label className="w-16 shrink-0 text-xs text-muted-foreground">
                                  {field.label}
                                </label>
                                {field.key === "polarity" ? (
                                  <select
                                    className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-sm"
                                    value={editValues[field.key] ?? field.value}
                                    onChange={(event) =>
                                      setEditValues((current) => ({
                                        ...current,
                                        [field.key]: event.target.value,
                                      }))
                                    }
                                  >
                                    {POLARITIES.map((option) => (
                                      <option key={option.value} value={option.value}>
                                        {option.label}
                                      </option>
                                    ))}
                                  </select>
                                ) : (
                                  <input
                                    className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-sm"
                                    value={editValues[field.key] ?? field.value}
                                    onChange={(event) =>
                                      setEditValues((current) => ({
                                        ...current,
                                        [field.key]: event.target.value,
                                      }))
                                    }
                                  />
                                )}
                              </div>
                            ))}
                            <div className="flex justify-end gap-2">
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                disabled={busyKey === fact.memory_key}
                                onClick={() => setEditingKey(null)}
                              >
                                <X className="size-4" />
                                取消
                              </Button>
                              <Button
                                type="button"
                                size="sm"
                                disabled={busyKey === fact.memory_key}
                                onClick={() => void saveEdit(fact)}
                              >
                                <Check className="size-4" />
                                保存
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <p className="break-words text-sm font-medium leading-6">
                            {factValueLabel(fact) || fact.evidence_quote}
                          </p>
                        )}
                        {fact.evidence_quote && !editing && (
                          <p className="break-words text-xs leading-5 text-muted-foreground">
                            来源：{fact.evidence_quote}
                          </p>
                        )}
                        <div className="text-xs text-muted-foreground">
                          {t("memory.updatedAt", {
                            time: formatDate(fact.valid_from) || "-",
                          })}
                        </div>
                        {historyOpen && versions.length > 0 && (
                          <div className="space-y-1 rounded-md border border-border bg-muted/30 p-2">
                            <p className="text-xs font-medium text-muted-foreground">版本历史</p>
                            {versions.map((version) => (
                              <div
                                key={`${version.memory_key}-${version.version_no}`}
                                className="flex items-center justify-between gap-2 text-xs"
                              >
                                <span>
                                  v{version.version_no}：{factValueLabel(version) || version.evidence_quote}
                                  {version.valid_to === null && !version.is_tombstone ? "（当前）" : ""}
                                  {version.is_tombstone ? "（已遗忘）" : ""}
                                </span>
                                <span className="shrink-0 text-muted-foreground">
                                  {formatDate(version.valid_from)}
                                  {version.valid_to ? ` → ${formatDate(version.valid_to)}` : ""}
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          className="size-8"
                          onClick={() => void toggleHistory(fact)}
                          title="历史"
                          aria-label="历史"
                        >
                          {historyOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                        </Button>
                        {!editing && (
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            className="size-8"
                            onClick={() => startEdit(fact)}
                            title="编辑"
                            aria-label="编辑"
                          >
                            <Pencil className="size-4" />
                          </Button>
                        )}
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          className="size-8 text-red-600 hover:bg-red-500/10 hover:text-red-700 dark:text-red-300"
                          disabled={busyKey === fact.memory_key}
                          onClick={() => void handleForget(fact)}
                          title={t("memory.forget")}
                          aria-label={t("memory.forget")}
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
