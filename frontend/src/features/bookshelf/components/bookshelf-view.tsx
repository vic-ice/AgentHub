import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  BookOpen,
  Check,
  Loader2,
  PencilLine,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react"

import {
  getBookshelf,
  removeShelfBook,
  updateShelfBook,
  upsertShelfBook,
} from "@/lib/api"
import type { BookEvaluation, ReadingStatus, ShelfBook } from "@/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

type BookshelfViewProps = {
  userId: string | null
}

type StatusFilter = "all" | ReadingStatus

const STATUS_TABS: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "want_to_read", label: "想读" },
  { value: "reading", label: "在读" },
  { value: "read", label: "已读" },
  { value: "dropped", label: "弃读" },
]

const STATUS_LABELS: Record<ReadingStatus, string> = {
  want_to_read: "想读",
  reading: "在读",
  read: "已读",
  dropped: "弃读",
}

const EVALUATION_OPTIONS: Array<{ value: BookEvaluation; label: string }> = [
  { value: "liked", label: "喜欢" },
  { value: "neutral", label: "一般" },
  { value: "disliked", label: "不喜欢" },
  { value: "not_interested", label: "不感兴趣" },
]

function statusButtonClass(active: boolean): string {
  return cn(
    "h-8 rounded-md px-3 text-[13px] font-medium transition-colors duration-150",
    active
      ? "bg-[var(--primary)] text-[var(--primary-foreground)]"
      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
  )
}

function evaluationButtonClass(active: boolean): string {
  return cn(
    "h-8 rounded-md px-2.5 text-[13px] font-medium transition-colors duration-150",
    active
      ? "bg-[var(--primary)]/10 text-[var(--primary)]"
      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
  )
}

export function BookshelfView({ userId }: BookshelfViewProps) {
  const [items, setItems] = useState<ShelfBook[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [activeStatus, setActiveStatus] = useState<StatusFilter>("all")
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const [addTitle, setAddTitle] = useState("")
  const [adding, setAdding] = useState(false)
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set())
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null)
  const [noteDraft, setNoteDraft] = useState("")
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async () => {
    if (!userId) {
      setItems([])
      setTotal(0)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const response = await getBookshelf({
        userId,
        status: activeStatus === "all" ? undefined : activeStatus,
        q: debouncedQuery || undefined,
      })
      setItems(response.items)
      setTotal(response.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载书架失败")
    } finally {
      setLoading(false)
    }
  }, [userId, activeStatus, debouncedQuery])

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      if (!userId) {
        return
      }
      try {
        const response = await getBookshelf({
          userId,
          status: activeStatus === "all" ? undefined : activeStatus,
          q: debouncedQuery || undefined,
          })
        if (!cancelled) {
          setItems(response.items)
          setTotal(response.total)
          setError(null)
          setLoading(false)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "加载书架失败")
          setLoading(false)
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [userId, activeStatus, debouncedQuery])

  useEffect(() => {
    if (searchTimer.current) {
      clearTimeout(searchTimer.current)
    }
    searchTimer.current = setTimeout(() => {
      setDebouncedQuery(query.trim())
    }, 300)
    return () => {
      if (searchTimer.current) {
        clearTimeout(searchTimer.current)
      }
    }
  }, [query])

  const counts = useMemo(() => {
    const map: Record<StatusFilter, number> = {
      all: total,
      want_to_read: 0,
      reading: 0,
      read: 0,
      dropped: 0,
    }
    for (const item of items) {
      map[item.reading_status] += 1
    }
    return map
  }, [items, total])

  const withSaving = useCallback(
    async (entryId: string, fn: () => Promise<unknown>) => {
      setSavingIds(prev => new Set(prev).add(entryId))
      try {
        await fn()
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : "操作失败")
      } finally {
        setSavingIds(prev => {
          const next = new Set(prev)
          next.delete(entryId)
          return next
        })
      }
    },
    [load],
  )

  const handleAdd = useCallback(async () => {
    const title = addTitle.trim()
    if (!title || !userId || adding) {
      return
    }
    setAdding(true)
    setError(null)
    try {
      await upsertShelfBook({
        user_id: userId,
        title,
        reading_status: "want_to_read",
      })
      setAddTitle("")
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加书籍失败")
    } finally {
      setAdding(false)
    }
  }, [addTitle, adding, load, userId])

  const handleStatusChange = useCallback(
    (entry: ShelfBook, status: ReadingStatus) => {
      if (status === entry.reading_status) {
        return
      }
      void withSaving(entry.id, () =>
        updateShelfBook(entry.id, { reading_status: status }),
      )
    },
    [withSaving],
  )

  const handleEvaluationChange = useCallback(
    (entry: ShelfBook, evaluation: BookEvaluation | null) => {
      if (evaluation === entry.evaluation) {
        return
      }
      void withSaving(entry.id, () =>
        updateShelfBook(entry.id, { evaluation }),
      )
    },
    [withSaving],
  )

  const handleRemove = useCallback(
    (entry: ShelfBook) => {
      void withSaving(entry.id, () => removeShelfBook(entry.id))
    },
    [withSaving],
  )

  const handleNoteSave = useCallback(
    (entry: ShelfBook) => {
      void withSaving(entry.id, () =>
        updateShelfBook(entry.id, { note: noteDraft.trim() }),
      )
      setEditingNoteId(null)
    },
    [noteDraft, withSaving],
  )

  return (
    <section className="flex h-full min-h-0 flex-col" data-od-id="bookshelf-view">
      {/* Header */}
      <header className="border-b border-border px-6 pb-4 pt-5">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-md bg-[var(--primary)]/10 text-[var(--primary)]">
            <BookOpen className="size-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-[22px] font-semibold leading-7 text-foreground">
              我的书架
            </h1>
            <p className="text-[13px] text-muted-foreground">
              共 {total} 本 · 按最近事件排序
            </p>
          </div>
        </div>
      </header>

      {/* Add + Search */}
      <div className="flex flex-col gap-3 border-b border-border px-6 py-4 sm:flex-row sm:items-center">
        <form
          className="flex w-full items-center gap-2 sm:max-w-md"
          onSubmit={event => {
            event.preventDefault()
            void handleAdd()
          }}
        >
          <Input
            value={addTitle}
            onChange={event => setAddTitle(event.target.value)}
            placeholder="输入书名，添加到想读"
            aria-label="输入书名添加书籍"
            className="h-11 flex-1"
            disabled={!userId}
          />
          <Button
            type="submit"
            size="default"
            disabled={!userId || !addTitle.trim() || adding}
          >
            {adding ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Plus className="size-4" />
            )}
            添加
          </Button>
        </form>
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索标题或作者"
            aria-label="搜索书架"
            className="h-11 pl-9"
          />
        </div>
      </div>

      {/* Status tabs */}
      <div
        className="flex flex-wrap items-center gap-1 border-b border-border px-6 py-2"
        role="tablist"
        aria-label="阅读状态筛选"
      >
        {STATUS_TABS.map(tab => (
          <button
            key={tab.value}
            type="button"
            role="tab"
            aria-selected={activeStatus === tab.value}
            className={statusButtonClass(activeStatus === tab.value)}
            onClick={() => setActiveStatus(tab.value)}
          >
            {tab.label}
            <span className="ml-1.5 text-xs opacity-70">{counts[tab.value]}</span>
          </button>
        ))}
      </div>

      {/* List */}
      <ScrollArea className="min-h-0 flex-1">
        {error && (
          <div className="m-4 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {loading && items.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            加载书架中…
          </div>
        )}

        {!loading && !error && items.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-3 py-20 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-accent text-muted-foreground">
              <BookOpen className="size-6" />
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">
                {query || activeStatus !== "all" ? "没有符合条件的书籍" : "书架还是空的"}
              </p>
              <p className="mt-1 text-[13px] text-muted-foreground">
                {query || activeStatus !== "all"
                  ? "换个关键词或筛选条件试试"
                  : "在上方输入书名，把第一本书加入想读清单"}
              </p>
            </div>
          </div>
        )}

        <ul className="divide-y divide-border">
          {items.map(entry => {
            const saving = savingIds.has(entry.id)
            const editingNote = editingNoteId === entry.id
            return (
              <li
                key={entry.id}
                className="flex flex-col gap-3 px-6 py-4 transition-colors duration-150 hover:bg-accent/30 lg:flex-row lg:items-center"
              >
                {/* Cover */}
                <div className="flex items-center gap-3 lg:w-80 lg:shrink-0">
                  <div className="flex size-12 shrink-0 items-center justify-center overflow-hidden rounded-md border border-border bg-card text-muted-foreground">
                    {entry.cover_url ? (
                      <img
                        src={entry.cover_url}
                        alt={entry.title}
                        className="size-full object-cover"
                        loading="lazy"
                      />
                    ) : (
                      <BookOpen className="size-5" />
                    )}
                  </div>
                  <div className="min-w-0">
                    <p className="truncate text-[15px] font-semibold leading-6 text-foreground">
                      {entry.title}
                    </p>
                    <p className="truncate text-[13px] text-muted-foreground">
                      {entry.authors.length > 0
                        ? entry.authors.join(" / ")
                        : "作者未知"}
                    </p>
                    {entry.rating !== null && entry.rating !== undefined && (
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {"★".repeat(entry.rating)}
                      </p>
                    )}
                  </div>
                </div>

                {/* Status */}
                <div
                  className="flex flex-wrap items-center gap-1"
                  role="group"
                  aria-label="阅读状态"
                >
                  {(Object.keys(STATUS_LABELS) as ReadingStatus[]).map(status => (
                    <button
                      key={status}
                      type="button"
                      className={statusButtonClass(entry.reading_status === status)}
                      onClick={() => handleStatusChange(entry, status)}
                      disabled={saving}
                    >
                      {STATUS_LABELS[status]}
                    </button>
                  ))}
                </div>

                {/* Evaluation */}
                <div
                  className="flex flex-wrap items-center gap-1"
                  role="group"
                  aria-label="书籍评价"
                >
                  {EVALUATION_OPTIONS.map(option => (
                    <button
                      key={option.value}
                      type="button"
                      className={evaluationButtonClass(
                        entry.evaluation === option.value,
                      )}
                      onClick={() =>
                        handleEvaluationChange(entry, option.value)
                      }
                      disabled={saving}
                    >
                      {option.label}
                    </button>
                  ))}
                  {entry.evaluation !== null && (
                    <button
                      type="button"
                      className="h-8 rounded-md px-2 text-[13px] text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                      onClick={() => handleEvaluationChange(entry, null)}
                      disabled={saving}
                      title="清除评价"
                    >
                      清除
                    </button>
                  )}
                </div>

                {/* Note */}
                <div className="flex min-w-0 flex-1 items-center gap-2 lg:max-w-xs">
                  {editingNote ? (
                    <>
                      <Input
                        value={noteDraft}
                        onChange={event => setNoteDraft(event.target.value)}
                        placeholder="写点备注"
                        aria-label="编辑备注"
                        className="h-9 flex-1 text-[13px]"
                        autoFocus
                      />
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        onClick={() => handleNoteSave(entry)}
                        disabled={saving}
                        aria-label="保存备注"
                      >
                        <Check className="size-4" />
                      </Button>
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        onClick={() => setEditingNoteId(null)}
                        aria-label="取消编辑备注"
                      >
                        <X className="size-4" />
                      </Button>
                    </>
                  ) : (
                    <>
                      <p
                        className={cn(
                          "min-w-0 flex-1 truncate text-[13px] leading-6",
                          entry.note ? "text-muted-foreground" : "text-muted-foreground/50",
                        )}
                      >
                        {entry.note || "无备注"}
                      </p>
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        onClick={() => {
                          setNoteDraft(entry.note)
                          setEditingNoteId(entry.id)
                        }}
                        aria-label="编辑备注"
                        title="编辑备注"
                      >
                        <PencilLine className="size-4" />
                      </Button>
                    </>
                  )}
                </div>

                {/* Remove */}
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  className="shrink-0 text-muted-foreground hover:text-destructive"
                  onClick={() => handleRemove(entry)}
                  disabled={saving}
                  aria-label={`从书架移除《${entry.title}》`}
                  title="从书架移除"
                >
                  <Trash2 className="size-4" />
                </Button>
              </li>
            )
          })}
        </ul>
      </ScrollArea>
    </section>
  )
}
