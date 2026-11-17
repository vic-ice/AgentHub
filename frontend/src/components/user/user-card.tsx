import type { UserInfo } from "@/types"
import { ArrowRight } from "lucide-react"

type UserCardProps = {
  user: UserInfo
  onClick: (user: UserInfo) => void
}

export function UserCard({ user, onClick }: UserCardProps) {
  return (
    <button
      type="button"
      onClick={() => onClick(user)}
      className="group flex min-h-[72px] w-full items-center gap-4 border-b border-border bg-transparent px-1 py-3 text-left transition-[background-color,color] duration-150 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <div className="flex size-11 shrink-0 items-center justify-center rounded-full border border-border bg-card text-sm font-semibold text-foreground">
        {user.name.slice(0, 1).toUpperCase()}
      </div>
      <span className="min-w-0 flex-1">
        <strong className="block truncate text-[15px] font-semibold text-foreground">{user.name}</strong>
        <small className="mt-0.5 block text-xs text-muted-foreground">进入个人工作区</small>
      </span>
      <ArrowRight className="size-4 shrink-0 text-muted-foreground transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-foreground" aria-hidden="true" />
    </button>
  )
}
