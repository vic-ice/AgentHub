import type { UserInfo } from "@/types"
import { AgentHubLogo } from "@/components/agenthub-logo"
import { UserCard } from "@/components/user/user-card"
import { WeixinQRCode } from "@/channels/weixin/WeixinQRCode"

const USERS: UserInfo[] = [
  { id: "00000000-0000-0000-0000-000000000001", name: "Jack", gender: "male" },
  { id: "00000000-0000-0000-0000-000000000002", name: "Rose", gender: "female" },
]

type HomePageProps = {
  onSelectUser: (user: UserInfo) => void
}

export function HomePage({ onSelectUser }: HomePageProps) {
  return (
    <main className="min-h-full overflow-y-auto bg-[var(--background-elevated)] p-3 sm:p-6 lg:p-8">
      <div className="mx-auto grid min-h-[calc(100vh-4rem)] max-w-[1280px] overflow-hidden border border-border bg-background lg:grid-cols-[minmax(0,1.3fr)_minmax(380px,0.7fr)]">
        <section className="flex min-h-[420px] flex-col justify-between p-7 sm:p-10 lg:p-14" aria-labelledby="home-title">
          <AgentHubLogo size="md" />
          <div className="max-w-2xl py-14 lg:py-20">
            <p className="mb-5 font-mono text-xs font-semibold tracking-[0.1em] text-primary">AGENTHUB / LOCAL WORKSPACE</p>
            <h1 id="home-title" className="max-w-[13ch] text-[42px] font-semibold leading-[1.35] sm:text-[56px]">
              让研究、对话与执行过程保持在同一上下文。
            </h1>
            <p className="mt-7 max-w-[58ch] text-[17px] leading-8 text-muted-foreground">
              选择工作身份后进入。模型路由、研究记录与记忆配置将沿用对应用户空间。
            </p>
          </div>
          <p className="font-mono text-xs text-muted-foreground">对话 · 深度研究 · 模型路由 · 执行追踪</p>
        </section>

        <section className="flex flex-col justify-center border-t border-border bg-card p-7 sm:p-10 lg:border-l lg:border-t-0 lg:p-12" aria-labelledby="identity-title">
          <div className="mx-auto w-full max-w-md">
            <p className="font-mono text-xs font-medium tracking-[0.08em] text-muted-foreground">01 / 访问工作区</p>
            <h2 id="identity-title" className="mt-3 text-3xl font-semibold">选择身份</h2>
            <p className="mt-3 text-[15px] leading-7 text-muted-foreground">身份决定会话、记忆与研究记录的归属。</p>
            <div className="mt-7 grid border-t border-border" aria-label="选择用户">
              {USERS.map((user) => (
                <UserCard key={user.id} user={user} onClick={() => onSelectUser(user)} />
              ))}
            </div>

            <div className="my-7 flex items-center gap-3 text-xs text-muted-foreground" aria-hidden="true">
              <span className="h-px flex-1 bg-border" />
              <span>或</span>
              <span className="h-px flex-1 bg-border" />
            </div>

            <div className="border border-border bg-background p-5" aria-label="微信登录">
              <p className="mb-4 text-center text-[15px] font-semibold text-foreground">使用微信扫码登录</p>
              <div className="flex justify-center">
                <WeixinQRCode />
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  )
}
