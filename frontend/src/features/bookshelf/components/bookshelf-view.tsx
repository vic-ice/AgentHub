import { useCallback, useEffect, useMemo, useState } from "react"
import {
  BookOpen,
  Check,
  CircleAlert,
  Loader2,
  MessageSquareText,
  PencilLine,
  Plus,
  RefreshCw,
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
import { getErrorDetails, type ErrorDetails } from "@/lib/errors"
import type { BookEvaluation, ReadingStatus, ShelfBook } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { useToast } from "@/hooks/use-toast"
import { cn } from "@/lib/utils"

type BookshelfViewProps = { userId: string | null }
type StatusFilter = "all" | ReadingStatus

const STATUS_TABS: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "want_to_read", label: "想读" },
  { value: "reading", label: "在读" },
  { value: "read", label: "已读" },
  { value: "dropped", label: "弃读" },
]

const STATUS_META: Record<ReadingStatus, { label: string }> = {
  want_to_read: { label: "想读" },
  reading: { label: "在读" },
  read: { label: "已读" },
  dropped: { label: "弃读" },
}

const STATUS_DOT: Record<ReadingStatus, string> = {
  want_to_read: "bg-amber-500",
  reading: "bg-sky-500",
  read: "bg-emerald-500",
  dropped: "bg-slate-400",
}

const EVALUATION_OPTIONS: Array<{ value: BookEvaluation; label: string }> = [
  { value: "liked", label: "喜欢" },
  { value: "neutral", label: "一般" },
  { value: "disliked", label: "不喜欢" },
  { value: "not_interested", label: "没兴趣" },
]

function segmentedButtonClass(active: boolean): string {
  return cn(
    "h-10 rounded-md px-2.5 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50",
    active
      ? "bg-background text-foreground shadow-sm ring-1 ring-border"
      : "text-muted-foreground hover:bg-background/70 hover:text-foreground",
  )
}

function BookshelfSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-6 p-6 sm:grid-cols-[repeat(auto-fill,minmax(180px,1fr))]" aria-label="正在加载书架">
      {[0, 1, 2, 3, 4, 5, 6, 7].map(index => (
        <Card key={index} className="gap-0 overflow-hidden p-0">
          <Skeleton className="aspect-[2/3] w-full rounded-none" />
          <div className="space-y-2 p-3">
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
        </Card>
      ))}
    </div>
  )
}

export function BookshelfView({ userId }: BookshelfViewProps) {
  const { toast } = useToast()
  const [items, setItems] = useState<ShelfBook[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ErrorDetails | null>(null)
  const [activeStatus, setActiveStatus] = useState<StatusFilter>("all")
  const [query, setQuery] = useState("")
  const [addTitle, setAddTitle] = useState("")
  const [adding, setAdding] = useState(false)
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set())
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null)
  const [noteDraft, setNoteDraft] = useState("")
  const [pendingRemoval, setPendingRemoval] = useState<ShelfBook | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!userId) {
      setItems([])
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const response = await getBookshelf({ userId, limit: 5000 })
      setItems(response.items)
    } catch (err) {
      setError(getErrorDetails(err, "加载书架失败，请稍后重试"))
    } finally {
      setLoading(false)
    }
  }, [userId])

  useEffect(() => {
    void load()
  }, [load])

  const counts = useMemo(() => {
    const result: Record<StatusFilter, number> = {
      all: items.length,
      want_to_read: 0,
      reading: 0,
      read: 0,
      dropped: 0,
    }
    for (const item of items) result[item.reading_status] += 1
    return result
  }, [items])

  const visibleItems = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase()
    return items.filter(item => {
      if (activeStatus !== "all" && item.reading_status !== activeStatus) return false
      if (!keyword) return true
      return [item.title, ...item.authors]
        .join(" ")
        .toLocaleLowerCase()
        .includes(keyword)
    })
  }, [activeStatus, items, query])

  const selectedEntry = useMemo(
    () => items.find(item => item.id === expandedId) ?? null,
    [expandedId, items],
  )

  const withSaving = useCallback(
    async (entryId: string, action: () => Promise<unknown>, successMessage?: string) => {
      setSavingIds(previous => new Set(previous).add(entryId))
      setError(null)
      try {
        await action()
        await load()
        if (successMessage) toast({ title: successMessage })
        return true
      } catch (err) {
        setError(getErrorDetails(err, "操作失败，请稍后重试"))
        return false
      } finally {
        setSavingIds(previous => {
          const next = new Set(previous)
          next.delete(entryId)
          return next
        })
      }
    },
    [load, toast],
  )

  const handleAdd = useCallback(async () => {
    const title = addTitle.trim()
    if (!title || !userId || adding) return
    setAdding(true)
    setError(null)
    try {
      await upsertShelfBook({ user_id: userId, title, reading_status: "want_to_read" })
      setAddTitle("")
      await load()
      toast({ title: `已将《${title}》加入想读` })
    } catch (err) {
      setError(getErrorDetails(err, "添加书籍失败，请稍后重试"))
    } finally {
      setAdding(false)
    }
  }, [addTitle, adding, load, toast, userId])

  const handleNoteSave = useCallback(async (entry: ShelfBook) => {
    const saved = await withSaving(
      entry.id,
      () => updateShelfBook(entry.id, { note: noteDraft.trim() }),
      "备注已保存",
    )
    if (saved) setEditingNoteId(null)
  }, [noteDraft, withSaving])

  const confirmRemove = useCallback(async () => {
    const entry = pendingRemoval
    if (!entry) return
    setPendingRemoval(null)
    setExpandedId(null)
    await withSaving(
      entry.id,
      () => removeShelfBook(entry.id),
      `已从书架移除《${entry.title}》`,
    )
  }, [pendingRemoval, withSaving])

  return (
    <section className="bookshelf-view flex h-full min-h-0 flex-col bg-muted/20" data-od-id="bookshelf-view">
      <header className="border-b border-border bg-background px-5 py-6 sm:px-8 sm:py-7">
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <p className="text-[11px] font-semibold tracking-[0.18em] text-primary">PERSONAL LIBRARY</p>
            <h1 className="bookshelf-display-title mt-1 text-[32px] font-semibold leading-10 text-foreground">我的书架</h1>
            <p className="mt-1.5 text-[15px] leading-6 text-muted-foreground">
              收藏阅读轨迹，也收藏每一次真实感受
            </p>
          </div>
          <div className="grid grid-cols-3 divide-x divide-border text-center">
            <div className="min-w-20 px-4 first:pl-0">
              <strong className="bookshelf-display-title block text-xl font-semibold leading-6 text-foreground">{items.length}</strong>
              <span className="mt-1 block text-xs text-muted-foreground">本藏书</span>
            </div>
            <div className="min-w-20 px-4">
              <strong className="bookshelf-display-title block text-xl font-semibold leading-6 text-foreground">{counts.reading}</strong>
              <span className="mt-1 block text-xs text-muted-foreground">本在读</span>
            </div>
            <div className="min-w-20 px-4 last:pr-0">
              <strong className="bookshelf-display-title block text-xl font-semibold leading-6 text-foreground">{counts.read}</strong>
              <span className="mt-1 block text-xs text-muted-foreground">本已读</span>
            </div>
          </div>
        </div>
      </header>

      <div className="border-b border-border bg-background px-5 py-4 sm:px-7">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <form
            className="flex min-w-0 flex-1 items-center gap-2 xl:max-w-xl"
            onSubmit={event => { event.preventDefault(); void handleAdd() }}
          >
            <div className="relative min-w-0 flex-1">
              <Plus className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={addTitle}
                onChange={event => setAddTitle(event.target.value)}
                placeholder="输入书名，添加到想读"
                aria-label="输入书名添加书籍"
                className="h-11 pl-9 text-[15px]"
                disabled={!userId}
              />
            </div>
            <Button type="submit" size="sm" className="h-11 px-4 text-sm" disabled={!userId || !addTitle.trim() || adding}>
              {adding ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
              添加
            </Button>
          </form>
          <div className="relative w-full xl:w-64">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="搜索书名或作者"
              aria-label="搜索书架"
              className="h-11 px-9 text-[15px]"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 flex size-6 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                aria-label="清空搜索"
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
        </div>

        <div className="mt-4 flex gap-1 overflow-x-auto rounded-lg bg-muted/70 p-1" role="tablist" aria-label="阅读状态筛选">
          {STATUS_TABS.map(tab => (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={activeStatus === tab.value}
              className={cn(
                "flex h-10 min-w-fit flex-1 items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium transition-all",
                activeStatus === tab.value
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => setActiveStatus(tab.value)}
            >
              {tab.label}
              <span className={cn("text-xs tabular-nums", activeStatus === tab.value ? "text-primary" : "text-muted-foreground/70")}>{counts[tab.value]}</span>
            </button>
          ))}
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        {error && (
          <Alert variant="destructive" className="mx-5 mt-5 w-auto bg-background sm:mx-7">
            <CircleAlert />
            <AlertTitle>操作未完成</AlertTitle>
            <AlertDescription>
              <p>{error.message}</p>
              {(error.code || error.requestId) && (
                <p className="font-mono text-xs opacity-80">
                  {[error.code && `错误码 ${error.code}`, error.requestId && `请求 ID ${error.requestId}`].filter(Boolean).join(" · ")}
                </p>
              )}
              {error.retryable && (
                <Button type="button" size="xs" variant="outline" className="mt-1" onClick={() => void load()}>
                  <RefreshCw className="size-3" />重新加载
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        {loading && items.length === 0 ? (
          <BookshelfSkeleton />
        ) : visibleItems.length === 0 ? (
          <div className="flex flex-col items-center justify-center px-6 py-20 text-center">
            <div className="flex size-14 items-center justify-center rounded-2xl bg-background text-muted-foreground shadow-sm ring-1 ring-border">
              <BookOpen className="size-6" />
            </div>
            <p className="mt-4 text-sm font-semibold text-foreground">
              {query || activeStatus !== "all" ? "没有找到符合条件的书" : "书架还是空的"}
            </p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              {query || activeStatus !== "all" ? "试试清空搜索或切换阅读状态" : "在上方输入书名，建立你的第一条阅读记录"}
            </p>
            {(query || activeStatus !== "all") && (
              <Button type="button" size="sm" variant="outline" className="mt-4" onClick={() => { setQuery(""); setActiveStatus("all") }}>
                查看全部书籍
              </Button>
            )}
          </div>
        ) : (
          <div className="p-5 sm:p-8">
            <div className="flex items-end justify-between gap-4 px-1">
              <div>
                <h2 className="bookshelf-display-title text-xl font-semibold leading-7 text-foreground">藏书陈列</h2>
                <p className="mt-1 text-[13px] text-muted-foreground">共 {visibleItems.length} 本 · 点击封面查看详情</p>
              </div>
              {loading && <span className="flex items-center gap-1.5 text-[13px] text-muted-foreground"><Loader2 className="size-3 animate-spin" />正在同步</span>}
            </div>
            <div className="mt-6 grid grid-cols-2 gap-x-6 gap-y-10 sm:grid-cols-[repeat(auto-fill,minmax(180px,1fr))]">
              {visibleItems.map(entry => {
                const saving = savingIds.has(entry.id)
                const statusMeta = STATUS_META[entry.reading_status]
                const selected = expandedId === entry.id
                const evaluationLabel = EVALUATION_OPTIONS.find(option => option.value === entry.evaluation)?.label
                return (
                  <button
                    key={entry.id}
                    type="button"
                    className="group flex min-w-0 flex-col text-left focus-visible:outline-none"
                    onClick={() => setExpandedId(entry.id)}
                    aria-label={`查看《${entry.title}》详情`}
                    aria-haspopup="dialog"
                  >
                    <Card className={cn(
                      "relative aspect-[2/3] w-full gap-0 overflow-hidden rounded-md border-border/70 p-0 shadow-[0_10px_28px_rgba(32,24,16,0.10)] transition-all duration-300 group-hover:-translate-y-1 group-hover:border-primary/25 group-hover:shadow-[0_16px_36px_rgba(32,24,16,0.16)] group-focus-visible:ring-2 group-focus-visible:ring-ring",
                      selected && "border-primary/40 shadow-lg ring-2 ring-primary/15",
                    )}>
                      {saving && <div className="absolute inset-x-0 top-0 z-20 h-0.5 overflow-hidden bg-primary/15"><div className="h-full w-1/2 animate-pulse bg-primary" /></div>}
                      <div className="relative size-full overflow-hidden bg-muted">
                        {entry.cover_url ? (
                          <img src={entry.cover_url} alt={`《${entry.title}》封面`} className="size-full object-cover transition-transform duration-300 group-hover:scale-[1.025]" loading="lazy" />
                        ) : (
                          <div className="flex size-full flex-col items-center justify-center gap-2 bg-gradient-to-br from-primary/5 to-primary/15 px-4 text-center text-muted-foreground">
                            <BookOpen className="size-8" />
                            <span className="line-clamp-2 text-xs font-medium">{entry.title}</span>
                          </div>
                        )}
                      </div>
                    </Card>
                    <div className="w-full px-0.5 pt-4">
                      <h2 className="bookshelf-book-title line-clamp-2 min-h-14 text-[19px] font-semibold leading-7 text-foreground" title={entry.title}>{entry.title}</h2>
                      <p className="mt-1 line-clamp-1 text-sm leading-6 text-muted-foreground" title={entry.authors.join(" / ")}>{entry.authors.length ? entry.authors.join(" / ") : "作者信息待补充"}</p>
                      <div className="mt-2.5 flex min-w-0 items-center gap-2 text-[13px] leading-5 text-muted-foreground">
                        <span className={cn("size-1.5 shrink-0 rounded-full", STATUS_DOT[entry.reading_status])} aria-hidden="true" />
                        <span>{statusMeta.label}</span>
                        {evaluationLabel && (
                          <>
                            <span aria-hidden="true" className="text-border">·</span>
                            <span className="truncate">{evaluationLabel}</span>
                          </>
                        )}
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>
          </div>
        )}
      </ScrollArea>

      <Sheet open={selectedEntry !== null} onOpenChange={open => { if (!open) { setExpandedId(null); setEditingNoteId(null) } }}>
        <SheetContent className="bookshelf-view !w-[min(92vw,430px)] gap-0 overflow-hidden p-0 sm:!max-w-[430px]" aria-describedby="bookshelf-book-description">
          {selectedEntry && (() => {
            const entry = selectedEntry
            const saving = savingIds.has(entry.id)
            const editingNote = editingNoteId === entry.id
            const statusMeta = STATUS_META[entry.reading_status]
            return (
              <>
                <SheetHeader className="border-b border-border p-5 pr-12">
                  <div className="flex gap-4">
                    <div className="flex h-36 w-24 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-border bg-muted text-muted-foreground shadow-sm">
                      {entry.cover_url ? (
                        <img src={entry.cover_url} alt={`《${entry.title}》封面`} className="size-full object-cover" />
                      ) : (
                        <BookOpen className="size-7" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1 self-center">
                      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
                        <span className={cn("size-2 rounded-full", STATUS_DOT[entry.reading_status])} aria-hidden="true" />
                        {statusMeta.label}
                      </div>
                      <SheetTitle className="bookshelf-book-title text-2xl leading-8 tracking-tight">{entry.title}</SheetTitle>
                      <SheetDescription id="bookshelf-book-description" className="mt-1.5 text-sm leading-6">
                        {entry.authors.length ? entry.authors.join(" / ") : "作者信息待补充"}
                      </SheetDescription>
                    </div>
                  </div>
                </SheetHeader>

                <ScrollArea className="min-h-0 flex-1">
                  <div className="space-y-6 p-5">
                    <section>
                      <p className="mb-2.5 text-[15px] font-semibold text-foreground">阅读进度</p>
                      <div className="grid grid-cols-4 gap-1 rounded-lg bg-muted/70 p-1" role="group" aria-label={`《${entry.title}》阅读状态`}>
                        {(Object.keys(STATUS_META) as ReadingStatus[]).map(status => (
                          <button key={status} type="button" className={segmentedButtonClass(entry.reading_status === status)} onClick={() => { if (status !== entry.reading_status) void withSaving(entry.id, () => updateShelfBook(entry.id, { reading_status: status })) }} disabled={saving} aria-pressed={entry.reading_status === status}>
                            {STATUS_META[status].label}
                          </button>
                        ))}
                      </div>
                    </section>

                    <section>
                      <p className="mb-2.5 text-[15px] font-semibold text-foreground">阅读感受</p>
                      <div className="grid grid-cols-4 gap-1 rounded-lg bg-muted/70 p-1" role="group" aria-label={`《${entry.title}》阅读评价`}>
                        {EVALUATION_OPTIONS.map(option => (
                          <button key={option.value} type="button" className={segmentedButtonClass(entry.evaluation === option.value)} onClick={() => { const value = entry.evaluation === option.value ? null : option.value; void withSaving(entry.id, () => updateShelfBook(entry.id, { evaluation: value })) }} disabled={saving} aria-pressed={entry.evaluation === option.value}>
                            {option.label}
                          </button>
                        ))}
                      </div>
                    </section>

                    <section>
                      <div className="mb-2 flex items-center justify-between">
                        <p className="text-[15px] font-semibold text-foreground">阅读备注</p>
                        {!editingNote && (
                          <Button type="button" size="xs" variant="ghost" onClick={() => { setNoteDraft(entry.note); setEditingNoteId(entry.id) }}>
                            <PencilLine className="size-3.5" />编辑
                          </Button>
                        )}
                      </div>
                      {editingNote ? (
                        <form className="rounded-xl border border-border bg-muted/20 p-3" onSubmit={event => { event.preventDefault(); void handleNoteSave(entry) }}>
                          <Input value={noteDraft} onChange={event => setNoteDraft(event.target.value)} placeholder="记录一句想法或摘记" aria-label={`编辑《${entry.title}》备注`} className="border-0 bg-transparent px-0 shadow-none focus-visible:ring-0" autoFocus disabled={saving} />
                          <div className="mt-3 flex justify-end gap-2 border-t border-border/60 pt-3">
                            <Button type="button" size="xs" variant="ghost" onClick={() => setEditingNoteId(null)}><X className="size-3.5" />取消</Button>
                            <Button type="submit" size="xs" disabled={saving}><Check className="size-3.5" />保存</Button>
                          </div>
                        </form>
                      ) : (
                        <button type="button" className="flex min-h-20 w-full items-start gap-3 rounded-xl border border-dashed border-border bg-muted/20 p-3 text-left transition-colors hover:border-primary/30 hover:bg-muted/40" onClick={() => { setNoteDraft(entry.note); setEditingNoteId(entry.id) }}>
                          <MessageSquareText className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                          <span className={cn("text-sm leading-6", entry.note ? "text-foreground" : "text-muted-foreground")}>{entry.note || "还没有阅读备注，点击记录你的想法…"}</span>
                        </button>
                      )}
                    </section>
                  </div>
                </ScrollArea>

                <div className="border-t border-border p-4">
                  <Button type="button" variant="ghost" className="w-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive" onClick={() => setPendingRemoval(entry)} disabled={saving}>
                    <Trash2 className="size-4" />移出书架
                  </Button>
                </div>
              </>
            )
          })()}
        </SheetContent>
      </Sheet>

      <AlertDialog open={pendingRemoval !== null} onOpenChange={open => { if (!open) setPendingRemoval(null) }}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>移出书架？</AlertDialogTitle>
            <AlertDialogDescription>将移除《{pendingRemoval?.title}》及其阅读状态和评价。此操作不可撤销。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => void confirmRemove()}>确认移除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
