import { ArrowRight, BookOpen, Brain, MessageSquare, SearchCheck } from "lucide-react"

type ReadingHomeProps = {
  userName?: string
  onOpenChat: () => void
  onOpenBookshelf: () => void
  onOpenMemory: () => void
  onOpenResearch: () => void
}

export function ReadingHome({ userName, onOpenChat, onOpenBookshelf, onOpenMemory, onOpenResearch }: ReadingHomeProps) {
  const entries = [
    { title: "智能对话", icon: MessageSquare, description: "用自然语言更新阅读状态、评价和备注，也可以查找图书、获取个性化推荐。", action: "开始对话", onClick: onOpenChat },
    { title: "我的书架", icon: BookOpen, description: "查看想读、在读与已读图书。点击图书封面，核对阅读状态、评价和备注。", action: "进入我的书架", onClick: onOpenBookshelf },
    { title: "长期记忆", icon: Brain, description: "查看当前有效的个人偏好与阅读事实，按需编辑、查看历史版本或遗忘。", action: "查看当前记忆", onClick: onOpenMemory },
    { title: "深度研究", icon: SearchCheck, description: "在对话中显式开启深度研究；在这里回顾研究任务、最终报告与研究详情。", action: "查看研究记录", onClick: onOpenResearch },
  ]

  return (
    <section aria-label="个人阅读工作台" className="min-h-0 flex-1 overflow-y-auto bg-muted/20">
      <div className="mx-auto max-w-6xl space-y-8 px-6 py-9 sm:px-10 sm:py-12">
        <section className="space-y-4">
          <p className="text-sm font-medium tracking-widest text-primary">个人阅读工作台</p>
          <h1 className="text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">让阅读有记录，让推荐更懂你</h1>
          <p className="max-w-3xl text-base leading-8 text-muted-foreground">
            {userName ? `${userName}，欢迎回来。` : "欢迎来到你的阅读空间。"}通过对话表达阅读情况，在书架核对保存结果，再用持续积累的偏好与反馈发现新书。
          </p>
        </section>

        <section aria-label="系统功能入口" className="grid gap-5 md:grid-cols-2">
          {entries.map(({ title, icon: Icon, description, action, onClick }) => (
            <article key={title} className="flex flex-col items-start rounded-xl border border-border bg-background p-6 shadow-sm sm:p-7">
              <div className="mb-4 flex items-center gap-3">
                <span className="rounded-lg bg-primary/10 p-2.5 text-primary"><Icon className="size-5" /></span>
                <h2 className="text-xl font-semibold">{title}</h2>
              </div>
              <p className="mb-5 max-w-xl flex-1 text-sm leading-7 text-muted-foreground">{description}</p>
              <button type="button" onClick={onClick} className="inline-flex items-center gap-2 rounded-md text-sm font-semibold text-primary underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-4">
                {action}<ArrowRight className="size-4" />
              </button>
            </article>
          ))}
        </section>

        <section className="rounded-xl border border-border bg-background px-6 py-5">
          <h2 className="mb-3 text-base font-semibold">第一次使用，从这里开始</h2>
          <ol className="grid gap-4 text-sm leading-7 text-muted-foreground lg:grid-cols-3">
            <li><span className="font-semibold text-foreground">01 · 表达阅读情况</span><br />在智能对话中说明书名、阅读状态、评价和备注。</li>
            <li><span className="font-semibold text-foreground">02 · 核对保存结果</span><br />进入我的书架，点击封面查看这本书的阅读详情。</li>
            <li><span className="font-semibold text-foreground">03 · 继续发现新书</span><br />在对话中提出“根据我的书架推荐不重复的新书”。</li>
          </ol>
        </section>
      </div>
    </section>
  )
}
