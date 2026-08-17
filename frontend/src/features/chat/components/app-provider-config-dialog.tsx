import { useCallback, useEffect, useMemo, useState } from "react"
import {
  CheckCircle2,
  ChevronDown,
  KeyRound,
  PlugZap,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
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
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import {
  checkAppProviderHealth,
  getAppProviderConfigs,
  updateAppProviderConfig,
} from "@/lib/api"
import type {
  AppProviderCapability,
  AppProviderConfig,
  AppProviderScope,
  AppProviderType,
} from "@/types"

type AppProviderConfigDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
}

const CAPABILITIES: AppProviderCapability[] = [
  "memory_recall",
  "semantic_search",
  "research_observation",
  "web_search",
  "source_visit",
]

const SCOPES: AppProviderScope[] = ["global", "workspace", "user"]

const CAPABILITY_LABELS: Record<AppProviderCapability, string> = {
  memory_recall: "记忆召回",
  semantic_search: "语义检索",
  research_observation: "研究过程观察",
  web_search: "网页搜索",
  source_visit: "来源访问",
}

const SCOPE_LABELS: Record<AppProviderScope, string> = {
  global: "全局",
  workspace: "工作区",
  user: "当前用户",
}

const TYPE_LABELS: Record<AppProviderType, string> = {
  memory: "记忆服务",
  research_observation: "研究观察",
  web_search: "网页搜索",
}

const TYPE_DESCRIPTIONS: Record<AppProviderType, string> = {
  memory: "负责长期事实的召回与语义检索。",
  research_observation: "记录 Deep Research 的过程观察与证据。",
  web_search: "为研究与对话提供实时网页搜索和来源访问。",
}

function formatJson(value: Record<string, unknown>): string {
  return JSON.stringify(value ?? {}, null, 2)
}

function parseSettings(text: string): Record<string, unknown> {
  const trimmed = text.trim()
  if (!trimmed) return {}
  const parsed = JSON.parse(trimmed) as unknown
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("高级设置必须是 JSON 对象")
  }
  return parsed as Record<string, unknown>
}

function healthLabel(status: string): string {
  const labels: Record<string, string> = {
    ok: "连接正常",
    disabled: "已停用",
    unknown: "未检查",
    missing_credentials: "缺少凭据",
    failed: "连接失败",
    timeout: "连接超时",
  }
  return labels[status] ?? status
}

function credentialLabel(status: string): string {
  if (status === "configured") return "凭据已配置"
  if (status === "missing") return "缺少凭据"
  return "无需凭据"
}

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "ok") return "default"
  if (["missing_credentials", "failed", "timeout"].includes(status)) return "destructive"
  if (["disabled", "unknown"].includes(status)) return "secondary"
  return "outline"
}

function formatCheckedAt(value: string | null): string {
  if (!value) return "尚未检查"
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
}

function ProviderIcon({ type }: { type: AppProviderType }) {
  if (type === "memory") return <ShieldCheck className="size-4" aria-hidden="true" />
  if (type === "web_search") return <Search className="size-4" aria-hidden="true" />
  return <PlugZap className="size-4" aria-hidden="true" />
}

export function AppProviderConfigDialog({ open, onOpenChange }: AppProviderConfigDialogProps) {
  const [providers, setProviders] = useState<AppProviderConfig[]>([])
  const [selectedKey, setSelectedKey] = useState("")
  const [enabled, setEnabled] = useState(false)
  const [scope, setScope] = useState<AppProviderScope>("global")
  const [apiKey, setApiKey] = useState("")
  const [settingsText, setSettingsText] = useState("{}")
  const [selectedCapabilities, setSelectedCapabilities] = useState<AppProviderCapability[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [busyAction, setBusyAction] = useState<"save" | "health" | null>(null)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")

  const selected = useMemo(
    () => providers.find((provider) => provider.provider_key === selectedKey) ?? null,
    [providers, selectedKey],
  )

  const applyProvider = useCallback((provider: AppProviderConfig) => {
    setSelectedKey(provider.provider_key)
    setEnabled(provider.enabled)
    setScope(provider.scope)
    setSettingsText(formatJson(provider.settings))
    setSelectedCapabilities(provider.capabilities)
    setApiKey("")
    setError("")
    setNotice("")
  }, [])

  const loadProviders = useCallback(async () => {
    setIsLoading(true)
    setError("")
    try {
      const result = await getAppProviderConfigs()
      setProviders(result.providers)
      const first = result.providers[0]
      if (first) applyProvider(first)
      else setSelectedKey("")
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "无法读取应用服务")
    } finally {
      setIsLoading(false)
    }
  }, [applyProvider])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => void loadProviders(), 0)
    return () => window.clearTimeout(timer)
  }, [loadProviders, open])

  const toggleCapability = (capability: AppProviderCapability) => {
    setSelectedCapabilities((current) => current.includes(capability)
      ? current.filter((item) => item !== capability)
      : [...current, capability])
  }

  const saveSelected = async () => {
    if (!selected) return
    setBusyAction("save")
    setError("")
    setNotice("")
    try {
      const updated = await updateAppProviderConfig(selected.provider_key, {
        enabled,
        scope,
        capabilities: selectedCapabilities,
        settings: parseSettings(settingsText),
        api_key: apiKey.trim() || null,
      })
      setProviders((current) => current.map((provider) => provider.provider_key === updated.provider_key ? updated : provider))
      setApiKey("")
      setNotice("服务配置已保存。")
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存服务配置失败")
    } finally {
      setBusyAction(null)
    }
  }

  const refreshHealth = async () => {
    if (!selected) return
    setBusyAction("health")
    setError("")
    setNotice("")
    try {
      const updated = await checkAppProviderHealth(selected.provider_key)
      setProviders((current) => current.map((provider) => provider.provider_key === updated.provider_key ? updated : provider))
      setNotice(updated.health.status === "ok" ? "连接检查通过。" : `检查完成：${healthLabel(updated.health.status)}`)
    } catch (healthError) {
      setError(healthError instanceof Error ? healthError.message : "连接检查失败")
    } finally {
      setBusyAction(null)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-hidden p-0 sm:max-w-5xl" data-od-id="app-services-dialog">
        <DialogHeader className="border-b border-border px-6 pb-5 pt-6">
          <DialogTitle className="text-2xl">应用服务</DialogTitle>
          <DialogDescription className="mt-2 max-w-2xl leading-6">
            管理记忆、研究观察与网页搜索。这里只配置应用能力；模型和 Provider 连接仍在“模型配置”中管理。
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-h-0 grid-cols-1 md:grid-cols-[230px_minmax(0,1fr)]">
          <aside className="border-b border-border bg-muted/20 p-3 md:border-b-0 md:border-r" aria-label="应用服务目录">
            {isLoading ? (
              <div className="p-3 text-sm text-muted-foreground">正在加载服务…</div>
            ) : providers.length === 0 ? (
              <div className="p-3 text-sm text-muted-foreground">当前没有可配置的应用服务。</div>
            ) : providers.map((provider) => (
              <button
                key={provider.provider_key}
                type="button"
                onClick={() => applyProvider(provider)}
                className={`mb-1 flex min-h-14 w-full items-center gap-3 px-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${provider.provider_key === selectedKey ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:bg-background hover:text-foreground"}`}
              >
                <span className="grid size-8 shrink-0 place-items-center border border-border bg-background"><ProviderIcon type={provider.provider_type} /></span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{provider.display_name || TYPE_LABELS[provider.provider_type]}</span>
                  <span className="mt-0.5 block truncate text-xs">{healthLabel(provider.health.status)}</span>
                </span>
                <span className={`size-2 rounded-full ${provider.enabled && provider.health.status === "ok" ? "bg-emerald-500" : provider.health.status === "failed" ? "bg-destructive" : "bg-muted-foreground/40"}`} aria-hidden="true" />
              </button>
            ))}
          </aside>

          <section className="min-w-0 overflow-y-auto p-5 sm:p-6" style={{ maxHeight: "72vh" }} data-od-id="app-service-config">
            {selected ? (
              <div className="mx-auto max-w-3xl space-y-6">
                <div className="flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <p className="text-xs font-medium tracking-[0.08em] text-muted-foreground">{TYPE_LABELS[selected.provider_type]}</p>
                    <h3 className="mt-1 text-2xl font-semibold leading-8">{selected.display_name || selected.provider_key}</h3>
                    <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{TYPE_DESCRIPTIONS[selected.provider_type]}</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Badge variant={statusVariant(selected.health.status)}>{healthLabel(selected.health.status)}</Badge>
                      <Badge variant="outline">{credentialLabel(selected.credential_status)}</Badge>
                      <code className="self-center text-xs text-muted-foreground">{selected.provider_key}</code>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button type="button" variant="outline" onClick={() => void refreshHealth()} disabled={busyAction !== null}>
                      <RefreshCw className={`size-4 ${busyAction === "health" ? "animate-spin" : ""}`} />检查连接
                    </Button>
                    <Button type="button" onClick={() => void saveSelected()} disabled={busyAction !== null}>
                      <Save className="size-4" />保存
                    </Button>
                  </div>
                </div>

                {error && <div className="border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" role="alert">{error}</div>}
                {notice && <div className="border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800" role="status">{notice}</div>}

                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <label className="text-sm font-medium" htmlFor="app-provider-enabled">服务状态</label>
                    <div className="flex min-h-11 items-center justify-between border border-input bg-background px-3">
                      <span className="text-sm text-muted-foreground">{enabled ? "已启用" : "已停用"}</span>
                      <Switch id="app-provider-enabled" checked={enabled} onCheckedChange={setEnabled} aria-label="启用应用服务" />
                    </div>
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium">作用范围</label>
                    <Select value={scope} onValueChange={(value) => setScope(value as AppProviderScope)}>
                      <SelectTrigger className="min-h-11"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {SCOPES.map((item) => <SelectItem key={item} value={item}>{SCOPE_LABELS[item]}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="app-provider-api-key">访问凭据</label>
                  <div className="relative">
                    <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                    <Input
                      id="app-provider-api-key"
                      className="min-h-11 pl-10"
                      type="password"
                      value={apiKey}
                      onChange={(event) => setApiKey(event.target.value)}
                      placeholder={selected.credential_status === "configured" ? "已配置；输入新密钥可替换" : "输入 API Key"}
                      autoComplete="new-password"
                    />
                  </div>
                  <p className="text-xs leading-5 text-muted-foreground">密钥由后端加密保存，浏览器不会读取已保存的明文。</p>
                </div>

                <fieldset className="space-y-3">
                  <legend className="text-sm font-medium">开放能力</legend>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {CAPABILITIES.map((capability) => {
                      const active = selectedCapabilities.includes(capability)
                      return (
                        <label key={capability} className="flex min-h-11 cursor-pointer items-center gap-3 border border-border px-3 text-sm hover:bg-muted/40">
                          <input type="checkbox" checked={active} onChange={() => toggleCapability(capability)} className="size-4 accent-[var(--primary)]" />
                          <span className="flex-1">{CAPABILITY_LABELS[capability]}</span>
                          {active && <CheckCircle2 className="size-4 text-emerald-600" aria-hidden="true" />}
                        </label>
                      )
                    })}
                  </div>
                </fieldset>

                <div className="border-y border-border py-4 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="font-medium">最近连接检查</p>
                      <p className="mt-1 text-xs text-muted-foreground">{formatCheckedAt(selected.health.checked_at)}{selected.health.duration_ms > 0 ? ` · ${selected.health.duration_ms} ms` : ""}</p>
                    </div>
                    <Badge variant={statusVariant(selected.health.status)}>{healthLabel(selected.health.status)}</Badge>
                  </div>
                  {selected.health.error && <p className="mt-3 text-sm leading-6 text-destructive">{selected.health.error}</p>}
                </div>

                <details className="group border-b border-border pb-4">
                  <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    高级设置
                    <ChevronDown className="size-4 transition-transform group-open:rotate-180" aria-hidden="true" />
                  </summary>
                  <p className="mb-2 text-xs leading-5 text-muted-foreground">仅在服务文档要求额外参数时修改。内容必须为 JSON 对象。</p>
                  <Textarea
                    value={settingsText}
                    onChange={(event) => setSettingsText(event.target.value)}
                    className="min-h-40 font-mono text-xs"
                    spellCheck={false}
                    aria-label="高级设置 JSON"
                  />
                </details>
              </div>
            ) : (
              <div className="flex min-h-80 items-center justify-center text-sm text-muted-foreground">请选择一个应用服务。</div>
            )}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  )
}
