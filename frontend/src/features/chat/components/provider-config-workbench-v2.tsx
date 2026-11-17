import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  Check,
  Eye,
  EyeOff,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react"

import {
  createModel,
  createProviderConnection,
  deleteModel,
  deleteProviderConnection,
  getAllModels,
  getProviderConnections,
  getProviders,
  setDefaultModel,
  updateModel,
  updateProvider,
  updateProviderConnection,
  validateModel,
} from "@/lib/api"
import { formatErrorForDisplay } from "@/lib/errors"
import type {
  ModelCreate,
  ModelInfo,
  ModelType,
  ProviderConnectionInfo,
  ProviderInfo,
} from "@/types"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"

import "./provider-config-workbench-v2.css"

type ProviderConfigDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfigChanged?: () => void | Promise<void>
}

type StatusTone = "neutral" | "success" | "error"

type ModelDraft = {
  model_id: string
  model_type: ModelType
  connection_id: string | null
  thinking: boolean
  is_active: boolean
  is_default: boolean
}

type ConnectionDraft = {
  name: string
  baseUrl: string
  apiKey: string
  enabled: boolean
  presetType: string
  extraHeaders: string
  clearApiKey: boolean
}

type StatusMessage = {
  tone: StatusTone
  text: string
} | null

const MODEL_TYPES: ModelType[] = ["llm", "vlm", "embedding"]

function providerKey(provider: ProviderInfo): string {
  return provider.provider_key || provider.provider
}

function modelProviderKey(model: ModelInfo): string {
  return model.provider_key || model.provider
}

function modelName(model: ModelInfo): string {
  return model.display_name || model.provider_model_id || model.model_id
}

function modelTypeLabel(type: ModelType): string {
  if (type === "embedding") return "向量模型"
  if (type === "vlm") return "视觉模型"
  return "对话模型"
}

function capabilityLabel(model: ModelInfo): { text: string; tone: StatusTone } {
  const capability = model.capability
  if (!capability) return { text: "待验证", tone: "neutral" }
  const ok = capability.probe_ok ?? capability.chat_ok ?? false
  if (!ok) return { text: "验证失败", tone: "error" }
  if (model.model_type === "embedding") return { text: "向量可用", tone: "success" }
  if (capability.thinking_request_ok && capability.reasoning_text_ok) {
    return { text: "推理可用", tone: "success" }
  }
  return { text: "对话可用", tone: "success" }
}

function formatUpdatedAt(value: string | undefined | null): string {
  if (!value) return "—"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date)
}

function toModelDraft(model: ModelInfo): ModelDraft {
  return {
    model_id: model.provider_model_id || model.model_id,
    model_type: model.model_type,
    connection_id: model.connection_id || null,
    thinking: model.thinking,
    is_active: model.is_active,
    is_default: model.is_default,
  }
}

function toConnectionDraft(
  connection: ProviderConnectionInfo | undefined,
  provider: ProviderInfo | undefined,
): ConnectionDraft {
  return {
    name: connection?.name || provider?.display_name || (provider ? providerKey(provider) : "Provider"),
    baseUrl: connection?.base_url || provider?.base_url || "",
    apiKey: "",
    enabled: connection?.enabled ?? provider?.enabled ?? true,
    presetType: connection?.preset_type || "custom",
    extraHeaders: JSON.stringify(connection?.extra_headers_json || {}, null, 2),
    clearApiKey: false,
  }
}

export function ProviderConfigDialog({
  open,
  onOpenChange,
  onConfigChanged,
}: ProviderConfigDialogProps) {
  const [models, setModels] = useState<ModelInfo[]>([])
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [connections, setConnections] = useState<ProviderConnectionInfo[]>([])
  const [selectedProviderKey, setSelectedProviderKey] = useState<string | null>(null)
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  const [connectionDraft, setConnectionDraft] = useState<ConnectionDraft | null>(null)
  const [modelDraft, setModelDraft] = useState<ModelDraft | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [status, setStatus] = useState<StatusMessage>(null)
  const [showApiKey, setShowApiKey] = useState(false)
  const [query, setQuery] = useState("")
  const [isCreatingConnection, setIsCreatingConnection] = useState(false)
  const [isCreatingModel, setIsCreatingModel] = useState(false)
  const [newConnection, setNewConnection] = useState<ConnectionDraft>({
    name: "",
    baseUrl: "",
    apiKey: "",
    enabled: true,
    presetType: "custom",
    extraHeaders: "{}",
    clearApiKey: false,
  })
  const [newModel, setNewModel] = useState<ModelCreate>({
    model_id: "",
    model_type: "llm",
    thinking: false,
    is_active: true,
    is_default: false,
  })

  const notifyChanged = useCallback(async () => {
    await onConfigChanged?.()
  }, [onConfigChanged])

  const loadSnapshot = useCallback(async (
    preferredModelId?: string | null,
    preferredProviderKey?: string | null,
    preferredConnectionId?: string | null,
  ) => {
    setIsLoading(true)
    setStatus(null)
    try {
      const [modelsResult, providersResult, connectionsResult] = await Promise.all([
        getAllModels(),
        getProviders(),
        getProviderConnections(),
      ])
      const nextModels = modelsResult.models
      const nextProviders = providersResult.providers
      const nextConnections = connectionsResult.connections
      const preferredModel = nextModels.find(model => model.id === preferredModelId)
      const nextProvider = nextProviders.find(provider => (
        providerKey(provider) === (preferredProviderKey || (preferredModel && modelProviderKey(preferredModel)))
      )) || nextProviders[0]
      const nextProviderKey = nextProvider ? providerKey(nextProvider) : null
      const nextConnection = nextConnections.find(connection => (
        connection.provider_key === nextProviderKey
        && connection.connection_id === (preferredConnectionId || preferredModel?.connection_id)
      )) || nextConnections.find(connection => connection.provider_key === nextProviderKey)
      const nextConnectionId = nextConnection?.connection_id ?? null
      const nextModel = nextModels.find(model => (
        model.id === preferredModelId
        && modelProviderKey(model) === nextProviderKey
      )) || nextModels.find(model => (
        modelProviderKey(model) === nextProviderKey
        && (nextConnectionId ? model.connection_id === nextConnectionId : !model.connection_id)
      )) || nextModels.find(model => modelProviderKey(model) === nextProviderKey)

      setModels(nextModels)
      setProviders(nextProviders)
      setConnections(nextConnections)
      setSelectedProviderKey(nextProviderKey)
      setSelectedConnectionId(nextConnectionId)
      setSelectedModelId(nextModel?.id ?? null)
      setConnectionDraft(nextProvider ? toConnectionDraft(nextConnection, nextProvider) : null)
      setModelDraft(nextModel ? toModelDraft(nextModel) : null)
    } catch (error) {
      const details = formatErrorForDisplay(error)
      setStatus({ tone: "error", text: `加载配置失败：${details}` })
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => void loadSnapshot(), 0)
    return () => window.clearTimeout(timer)
  }, [open, loadSnapshot])

  const selectedProvider = useMemo(
    () => providers.find(provider => providerKey(provider) === selectedProviderKey),
    [providers, selectedProviderKey],
  )
  const providerConnections = useMemo(
    () => connections.filter(connection => connection.provider_key === selectedProviderKey),
    [connections, selectedProviderKey],
  )
  const selectedConnection = useMemo(
    () => connections.find(connection => connection.connection_id === selectedConnectionId),
    [connections, selectedConnectionId],
  )
  const selectedProviderModels = useMemo(
    () => models.filter(model => modelProviderKey(model) === selectedProviderKey),
    [models, selectedProviderKey],
  )
  const selectedConnectionModels = useMemo(
    () => selectedProviderModels.filter(model => (
      selectedConnectionId ? model.connection_id === selectedConnectionId : !model.connection_id
    )),
    [selectedConnectionId, selectedProviderModels],
  )
  const visibleModels = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return selectedConnectionModels
    return selectedConnectionModels.filter(model => (
      modelName(model).toLowerCase().includes(normalized)
      || model.model_type.toLowerCase().includes(normalized)
    ))
  }, [query, selectedConnectionModels])
  const selectedModel = useMemo(
    () => models.find(model => model.id === selectedModelId) ?? null,
    [models, selectedModelId],
  )
  const defaultModel = models.find(model => model.is_default)
  const activeModelCount = models.filter(model => model.is_active).length
  const defaultConnectionModelCount = selectedProviderModels.filter(model => !model.connection_id).length
  const showDefaultConnection = Boolean(selectedProvider) && (
    providerConnections.length === 0
    || defaultConnectionModelCount > 0
    || Boolean(selectedProvider?.base_url)
    || Boolean(selectedProvider?.has_api_key)
  )

  const chooseProvider = (provider: ProviderInfo) => {
    const key = providerKey(provider)
    const connection = connections.find(item => item.provider_key === key)
    const model = models.find(item => (
      modelProviderKey(item) === key
      && (connection ? item.connection_id === connection.connection_id : !item.connection_id)
    )) || models.find(item => modelProviderKey(item) === key)
    setSelectedProviderKey(key)
    setSelectedConnectionId(connection?.connection_id ?? null)
    setSelectedModelId(model?.id ?? null)
    setConnectionDraft(toConnectionDraft(connection, provider))
    setModelDraft(model ? toModelDraft(model) : null)
    setIsCreatingConnection(false)
    setIsCreatingModel(false)
    setQuery("")
    setStatus(null)
  }

  const chooseConnection = (connection: ProviderConnectionInfo) => {
    const model = models.find(item => (
      modelProviderKey(item) === connection.provider_key
      && item.connection_id === connection.connection_id
    ))
    setSelectedConnectionId(connection.connection_id)
    setSelectedModelId(model?.id ?? null)
    setConnectionDraft(toConnectionDraft(connection, selectedProvider))
    setModelDraft(model ? toModelDraft(model) : null)
    setIsCreatingConnection(false)
    setIsCreatingModel(false)
    setQuery("")
    setStatus(null)
  }

  const chooseDefaultConnection = () => {
    if (!selectedProvider) return
    const key = providerKey(selectedProvider)
    const model = models.find(item => modelProviderKey(item) === key && !item.connection_id)
    setSelectedConnectionId(null)
    setSelectedModelId(model?.id ?? null)
    setConnectionDraft(toConnectionDraft(undefined, selectedProvider))
    setModelDraft(model ? toModelDraft(model) : null)
    setIsCreatingConnection(false)
    setIsCreatingModel(false)
    setQuery("")
    setStatus(null)
  }

  const chooseModel = (model: ModelInfo) => {
    setSelectedModelId(model.id)
    setModelDraft(toModelDraft(model))
    setStatus(null)
  }

  const runAction = async (key: string, action: () => Promise<void>, success: string) => {
    setBusyKey(key)
    setStatus(null)
    try {
      await action()
      setStatus({ tone: "success", text: success })
      await notifyChanged()
    } catch (error) {
      setStatus({ tone: "error", text: formatErrorForDisplay(error) })
    } finally {
      setBusyKey(null)
    }
  }

  const saveModel = async () => {
    if (!selectedModel || !modelDraft || !modelDraft.model_id.trim()) return
    await runAction(`save-model-${selectedModel.id}`, async () => {
      if (modelDraft.is_default && !selectedModel.is_default) await setDefaultModel(selectedModel.id)
      await updateModel(selectedModel.id, {
        model_id: modelDraft.model_id.trim(),
        model_type: modelDraft.model_type,
        connection_id: modelDraft.connection_id,
        thinking: modelDraft.thinking,
        is_active: modelDraft.is_active,
        is_default: modelDraft.is_default,
      })
      await loadSnapshot(selectedModel.id, selectedProviderKey, modelDraft.connection_id)
    }, "模型配置已保存")
  }

  const startModelCreation = () => {
    setNewModel({
      model_id: "",
      model_type: "llm",
      provider: selectedProviderKey || "",
      connection_id: selectedConnectionId,
      thinking: false,
      is_active: true,
      is_default: false,
    })
    setIsCreatingModel(true)
    setStatus(null)
  }

  const createNewModel = async () => {
    if (!newModel.model_id.trim()) {
      setStatus({ tone: "error", text: "请输入模型名称 model_id" })
      return
    }
    await runAction("create-model", async () => {
      const created = await createModel({
        ...newModel,
        model_id: newModel.model_id.trim(),
        provider: newModel.provider || selectedProviderKey || "",
        connection_id: newModel.connection_id || null,
      })
      setIsCreatingModel(false)
      await loadSnapshot(created.id, selectedProviderKey, newModel.connection_id)
    }, "模型已登记")
  }

  const probeModel = async (model: ModelInfo) => {
    await runAction(`validate-model-${model.id}`, async () => {
      await validateModel(model.id, model.model_type !== "embedding")
      await loadSnapshot(model.id, selectedProviderKey, selectedConnectionId)
    }, "模型验证已完成")
  }

  const removeModel = async (model: ModelInfo) => {
    if (!window.confirm(`确认删除模型“${modelName(model)}”？`)) return
    await runAction(`delete-model-${model.id}`, async () => {
      await deleteModel(model.id)
      await loadSnapshot(null, selectedProviderKey, selectedConnectionId)
    }, "模型已删除")
  }

  const parseHeaders = (value: string): Record<string, unknown> | null => {
    try {
      const parsed = JSON.parse(value || "{}") as unknown
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("请求头必须是对象")
      }
      return parsed as Record<string, unknown>
    } catch {
      setStatus({ tone: "error", text: "自定义请求头必须是有效的 JSON 对象" })
      return null
    }
  }

  const saveProviderConfiguration = async () => {
    if (!selectedProvider || !connectionDraft) return
    const headers = parseHeaders(connectionDraft.extraHeaders)
    if (!headers) return
    const key = providerKey(selectedProvider)
    await runAction("save-provider", async () => {
      if (selectedConnection) {
        await updateProviderConnection(selectedConnection.connection_id, {
          name: connectionDraft.name.trim() || selectedConnection.name,
          base_url: connectionDraft.baseUrl.trim() || null,
          api_key: connectionDraft.apiKey.trim() || undefined,
          clear_api_key: connectionDraft.clearApiKey,
          enabled: connectionDraft.enabled,
          preset_type: connectionDraft.presetType.trim() || "custom",
          extra_headers_json: headers,
        })
      } else {
        await updateProvider({
          provider: selectedProvider.provider,
          base_url: connectionDraft.baseUrl.trim() || null,
          api_key: connectionDraft.apiKey.trim() || undefined,
        })
      }
      await loadSnapshot(selectedModelId, key, selectedConnectionId)
    }, "连接配置已保存")
  }

  const startConnection = () => {
    if (!selectedProviderKey) return
    setNewConnection({
      name: selectedProviderKey === "openai-compatible"
        ? `兼容服务 ${providerConnections.length + 1}`
        : `连接 ${providerConnections.length + 1}`,
      baseUrl: "",
      apiKey: "",
      enabled: true,
      presetType: "custom",
      extraHeaders: "{}",
      clearApiKey: false,
    })
    setIsCreatingConnection(true)
    setStatus(null)
  }

  const createConnection = async () => {
    if (!selectedProviderKey || !newConnection.name.trim()) return
    const headers = parseHeaders(newConnection.extraHeaders)
    if (!headers) return
    await runAction("create-connection", async () => {
      const created = await createProviderConnection({
        provider_key: selectedProviderKey,
        name: newConnection.name.trim(),
        preset_type: newConnection.presetType.trim() || "custom",
        base_url: newConnection.baseUrl.trim() || null,
        api_key: newConnection.apiKey.trim() || null,
        enabled: newConnection.enabled,
        extra_headers_json: headers,
      })
      setIsCreatingConnection(false)
      await loadSnapshot(null, selectedProviderKey, created.connection_id)
    }, "服务连接已创建")
  }

  const removeConnection = async (connection: ProviderConnectionInfo) => {
    if (!window.confirm(`确认删除连接“${connection.name}”？关联模型会失去该路由。`)) return
    await runAction(`delete-connection-${connection.connection_id}`, async () => {
      await deleteProviderConnection(connection.connection_id)
      await loadSnapshot(null, selectedProviderKey)
    }, "服务连接已删除")
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="routing-dialog"
        showCloseButton={false}
        aria-describedby="routing-workbench-description"
      >
        <div className="routing-workbench" data-od-id="routing-workbench">
          <header className="rw-mast" data-od-id="routing-mast">
            <div className="rw-mast-copy">
              <DialogTitle className="rw-title">模型与 Provider</DialogTitle>
              <DialogDescription id="routing-workbench-description" className="rw-description">
                统一管理协议适配器、服务连接与模型配置
              </DialogDescription>
            </div>
            <dl className="rw-metrics" aria-label="配置摘要" data-od-id="routing-metrics">
              <div><dt>协议适配器</dt><dd>{providers.length}</dd><span>Provider adapters</span></div>
              <div><dt>服务连接</dt><dd>{connections.length}</dd><span>Base URL + API Key</span></div>
              <div><dt>可用模型</dt><dd>{activeModelCount}</dd><span>共登记 {models.length}</span></div>
              <div><dt>默认模型</dt><dd>{defaultModel ? modelName(defaultModel) : "未设置"}</dd><span>默认聊天路由</span></div>
            </dl>
            <div className="rw-mast-actions">
              <button type="button" className="rw-icon-button" onClick={() => void loadSnapshot(selectedModelId, selectedProviderKey, selectedConnectionId)} disabled={isLoading} aria-label="刷新配置">
                <RefreshCw aria-hidden="true" className={isLoading ? "is-spinning" : ""} />
              </button>
              <button type="button" className="rw-icon-button" onClick={() => onOpenChange(false)} aria-label="关闭模型配置">
                <X aria-hidden="true" />
              </button>
            </div>
          </header>

          {status && (
            <div className={`rw-status is-${status.tone}`} role={status.tone === "error" ? "alert" : "status"}>
              {status.tone === "success" ? <Check aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
              <span>{status.text}</span>
            </div>
          )}

          {isLoading ? (
            <div className="rw-loading" role="status" aria-live="polite">
              <LoaderCircle aria-hidden="true" className="is-spinning" />
              <p>正在读取模型与连接配置…</p>
            </div>
          ) : (
            <div className="rw-console">
              <nav className="rw-provider-index" aria-label="协议适配器" data-od-id="provider-index">
                <div className="rw-column-heading"><span>01</span><h2>协议适配器</h2></div>
                <div className="rw-provider-list">
                  {providers.map((provider, index) => {
                    const key = providerKey(provider)
                    const connectionCount = connections.filter(item => item.provider_key === key).length
                    const modelCount = models.filter(item => modelProviderKey(item) === key).length
                    return (
                      <button
                        type="button"
                        key={key}
                        className={key === selectedProviderKey ? "is-selected" : ""}
                        onClick={() => chooseProvider(provider)}
                        data-od-id={`provider-${key}`}
                      >
                        <span className="rw-provider-number">{String(index + 1).padStart(2, "0")}</span>
                        <span className="rw-provider-mark" aria-hidden="true">{(provider.display_name || key).slice(0, 1).toUpperCase()}</span>
                        <span className="rw-provider-name"><strong>{provider.display_name || key}</strong><small>{connectionCount} 连接 · {modelCount} 模型</small></span>
                      </button>
                    )
                  })}
                </div>
                <div className="rw-provider-note">
                  <strong>自定义厂家</strong>
                  <p>兼容 OpenAI 协议的服务继续归入 OpenAI Compatible，并创建独立连接。</p>
                </div>
              </nav>

              <main className="rw-records" data-od-id="provider-records">
                <section className="rw-record-section" aria-labelledby="connection-section-title">
                  <div className="rw-section-heading">
                    <div><span>02</span><div><h2 id="connection-section-title">服务连接</h2><p>管理 {selectedProvider?.display_name || "当前适配器"} 的 Base URL 与凭据</p></div></div>
                    <button type="button" className="rw-primary" onClick={startConnection} disabled={!selectedProvider}><Plus aria-hidden="true" />新建连接</button>
                  </div>

                  {isCreatingConnection && (
                    <div className="rw-create-sheet" data-od-id="create-connection-sheet">
                      <div className="rw-sheet-head"><div><span>NEW CONNECTION</span><h3>新增服务连接</h3></div><button type="button" className="rw-icon-button" onClick={() => setIsCreatingConnection(false)} aria-label="取消新增连接"><X aria-hidden="true" /></button></div>
                      <div className="rw-field-grid">
                        <label>连接名称<input value={newConnection.name} onChange={event => setNewConnection(current => ({ ...current, name: event.target.value }))} placeholder="例如 SiliconFlow 生产环境" /></label>
                        <label>Base URL<input value={newConnection.baseUrl} onChange={event => setNewConnection(current => ({ ...current, baseUrl: event.target.value }))} placeholder="https://api.example.com/v1" /></label>
                      </div>
                      <label className="rw-full-field">API Key<input type="password" value={newConnection.apiKey} onChange={event => setNewConnection(current => ({ ...current, apiKey: event.target.value }))} autoComplete="new-password" /></label>
                      <details className="rw-advanced-config"><summary>高级连接配置</summary><div className="rw-field-grid"><label>预设类型<input value={newConnection.presetType} onChange={event => setNewConnection(current => ({ ...current, presetType: event.target.value }))} /></label><label>自定义请求头（JSON）<textarea value={newConnection.extraHeaders} onChange={event => setNewConnection(current => ({ ...current, extraHeaders: event.target.value }))} spellCheck={false} /></label></div></details>
                      <div className="rw-sheet-actions"><button type="button" className="rw-primary" onClick={() => void createConnection()} disabled={busyKey === "create-connection"}>保存连接</button><button type="button" className="rw-secondary" onClick={() => setIsCreatingConnection(false)}>取消</button></div>
                    </div>
                  )}

                  <div className="rw-table rw-connection-table" role="table" aria-label="服务连接">
                    <div className="rw-table-head" role="row"><span>连接名称</span><span>Base URL</span><span>状态</span><span>模型</span><span>凭据</span><span>操作</span></div>
                    {showDefaultConnection && (
                      <div className={`rw-table-row ${selectedConnectionId === null ? "is-selected" : ""}`} role="row">
                        <button type="button" className="rw-row-select" onClick={chooseDefaultConnection} aria-pressed={selectedConnectionId === null}>
                          <span><strong>适配器默认配置</strong><small>provider-default</small></span>
                          <code>{selectedProvider?.base_url || "环境默认地址"}</code>
                          <span className="rw-state is-success"><i />启用</span>
                          <span>{defaultConnectionModelCount}</span>
                          <span>{selectedProvider?.has_api_key ? "已配置" : "未配置"}</span>
                        </button>
                        <span className="rw-row-actions">—</span>
                      </div>
                    )}
                    {providerConnections.map(connection => (
                      <div className={`rw-table-row ${connection.connection_id === selectedConnectionId ? "is-selected" : ""}`} role="row" key={connection.connection_id}>
                        <button type="button" className="rw-row-select" onClick={() => chooseConnection(connection)} aria-pressed={connection.connection_id === selectedConnectionId}>
                          <span><strong>{connection.name}</strong><small>{connection.connection_id.slice(0, 8)}</small></span>
                          <code>{connection.base_url || "环境默认地址"}</code>
                          <span className={`rw-state is-${connection.enabled ? "success" : "neutral"}`}><i />{connection.enabled ? "在线" : "停用"}</span>
                          <span>{connection.model_count}</span>
                          <span>{connection.has_api_key ? "已配置" : "缺少 Key"}</span>
                        </button>
                        <button type="button" className="rw-row-delete" onClick={() => void removeConnection(connection)} disabled={busyKey === `delete-connection-${connection.connection_id}`} aria-label={`删除连接 ${connection.name}`}><Trash2 aria-hidden="true" /></button>
                      </div>
                    ))}
                    {!showDefaultConnection && providerConnections.length === 0 && <div className="rw-empty"><p>当前适配器还没有服务连接。</p></div>}
                  </div>
                </section>

                <section className="rw-record-section rw-model-section" aria-labelledby="model-section-title">
                  <div className="rw-section-heading">
                    <div><span>MODEL</span><div><h2 id="model-section-title">该连接下的模型 <b>{selectedConnectionModels.length}</b></h2><p>{selectedConnection?.name || "适配器默认配置"}</p></div></div>
                    <div className="rw-section-tools">
                      <label className="rw-search"><Search aria-hidden="true" /><span className="sr-only">搜索模型</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索模型" /></label>
                      <button type="button" className="rw-secondary" onClick={startModelCreation} disabled={!selectedProvider}><Plus aria-hidden="true" />登记模型</button>
                    </div>
                  </div>

                  {isCreatingModel && (
                    <div className="rw-create-sheet" data-od-id="create-model-sheet">
                      <div className="rw-sheet-head"><div><span>NEW MODEL</span><h3>登记到当前连接</h3></div><button type="button" className="rw-icon-button" onClick={() => setIsCreatingModel(false)} aria-label="取消登记模型"><X aria-hidden="true" /></button></div>
                      <div className="rw-field-grid">
                        <label>模型名称 / model_id<input value={newModel.model_id} onChange={event => setNewModel(current => ({ ...current, model_id: event.target.value }))} placeholder="例如 qwen-max" /></label>
                        <label>模型类型<select value={newModel.model_type} onChange={event => setNewModel(current => ({ ...current, model_type: event.target.value as ModelType }))}>{MODEL_TYPES.map(type => <option key={type} value={type}>{modelTypeLabel(type)}</option>)}</select></label>
                      </div>
                      <div className="rw-switch-row"><label><input type="checkbox" checked={newModel.thinking} onChange={event => setNewModel(current => ({ ...current, thinking: event.target.checked }))} />thinking</label><label><input type="checkbox" checked={newModel.is_active} onChange={event => setNewModel(current => ({ ...current, is_active: event.target.checked }))} />立即启用</label><label><input type="checkbox" checked={newModel.is_default} onChange={event => setNewModel(current => ({ ...current, is_default: event.target.checked }))} />默认模型</label></div>
                      <div className="rw-sheet-actions"><button type="button" className="rw-primary" onClick={() => void createNewModel()} disabled={busyKey === "create-model"}>完成登记</button><button type="button" className="rw-secondary" onClick={() => setIsCreatingModel(false)}>取消</button></div>
                    </div>
                  )}

                  <div className="rw-table rw-model-table" role="table" aria-label="模型列表">
                    <div className="rw-table-head" role="row"><span>模型名称</span><span>类型</span><span>thinking</span><span>状态</span><span>更新时间</span><span>操作</span></div>
                    {visibleModels.map(model => {
                      const capability = capabilityLabel(model)
                      return (
                        <div className={`rw-table-row ${model.id === selectedModelId ? "is-selected" : ""}`} role="row" key={model.id}>
                          <button type="button" className="rw-row-select" onClick={() => chooseModel(model)} aria-pressed={model.id === selectedModelId}>
                            <span><strong>{modelName(model)}</strong><small>{model.is_default ? "默认模型" : model.provider_model_id || model.model_id}</small></span>
                            <span>{modelTypeLabel(model.model_type)}</span>
                            <span>{model.thinking ? "启用" : "关闭"}</span>
                            <span className={`rw-state is-${capability.tone}`}><i />{model.is_active ? capability.text : "已停用"}</span>
                            <span>{formatUpdatedAt(model.updated_at)}</span>
                          </button>
                          <button type="button" className="rw-row-action" onClick={() => void probeModel(model)} disabled={busyKey === `validate-model-${model.id}`}>{busyKey === `validate-model-${model.id}` ? <LoaderCircle aria-hidden="true" className="is-spinning" /> : "验证"}</button>
                        </div>
                      )
                    })}
                    {visibleModels.length === 0 && <div className="rw-empty"><p>{query ? "没有匹配的模型。" : "当前连接还没有登记模型。"}</p>{!query && <button type="button" onClick={startModelCreation}>登记第一个模型</button>}</div>}
                  </div>
                </section>
              </main>

              <aside className="rw-inspector" aria-labelledby="inspector-title" data-od-id="configuration-inspector">
                <div className="rw-column-heading"><span>03</span><div><h2 id="inspector-title">模型配置</h2><p>配置所选连接的模型与路由策略</p></div></div>

                {selectedProvider && connectionDraft ? (
                  <>
                    <section className="rw-inspector-section" aria-labelledby="credential-title">
                      <div className="rw-subheading"><h3 id="credential-title">连接凭据</h3><span className={`rw-state is-${selectedConnection?.enabled ?? selectedProvider.enabled ? "success" : "neutral"}`}><i />{selectedConnection?.enabled ?? selectedProvider.enabled ? "启用" : "停用"}</span></div>
                      <div className="rw-field-grid single">
                        <label>连接名称<input value={connectionDraft.name} onChange={event => setConnectionDraft(current => current ? ({ ...current, name: event.target.value }) : current)} disabled={!selectedConnection} /></label>
                        <label>Base URL<input value={connectionDraft.baseUrl} onChange={event => setConnectionDraft(current => current ? ({ ...current, baseUrl: event.target.value }) : current)} placeholder="使用适配器默认地址" /></label>
                        <label>API Key<span className="rw-secret-field"><input type={showApiKey ? "text" : "password"} value={connectionDraft.apiKey} onChange={event => setConnectionDraft(current => current ? ({ ...current, apiKey: event.target.value }) : current)} placeholder={selectedConnection?.has_api_key || selectedProvider.has_api_key ? "已配置；留空保持不变" : "尚未配置"} autoComplete="new-password" /><button type="button" onClick={() => setShowApiKey(value => !value)} aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}>{showApiKey ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}</button></span></label>
                      </div>
                      {selectedConnection && (
                        <details className="rw-advanced-config"><summary>高级连接配置</summary><div className="rw-field-grid single"><label>预设类型<input value={connectionDraft.presetType} onChange={event => setConnectionDraft(current => current ? ({ ...current, presetType: event.target.value }) : current)} /></label><label>自定义请求头（JSON）<textarea value={connectionDraft.extraHeaders} onChange={event => setConnectionDraft(current => current ? ({ ...current, extraHeaders: event.target.value }) : current)} spellCheck={false} /></label></div>{selectedConnection.has_api_key && <label className="rw-clear-credential"><input type="checkbox" checked={connectionDraft.clearApiKey} onChange={event => setConnectionDraft(current => current ? ({ ...current, clearApiKey: event.target.checked, apiKey: event.target.checked ? "" : current.apiKey }) : current)} />清除已保存的 API Key</label>}</details>
                      )}
                      {selectedConnection && <label className="rw-toggle-row"><span><strong>启用连接</strong><small>停用后不参与模型路由</small></span><input type="checkbox" checked={connectionDraft.enabled} onChange={event => setConnectionDraft(current => current ? ({ ...current, enabled: event.target.checked }) : current)} /></label>}
                      <button type="button" className="rw-secondary rw-save-connection" onClick={() => void saveProviderConfiguration()} disabled={busyKey === "save-provider"}>保存连接配置</button>
                    </section>

                    <section className="rw-inspector-section rw-model-editor" aria-labelledby="model-editor-title">
                      <div className="rw-subheading"><h3 id="model-editor-title">模型设置</h3>{selectedModel && <code>{selectedModel.id.slice(0, 8)}</code>}</div>
                      {selectedModel && modelDraft ? (
                        <>
                          <div className="rw-field-grid single">
                            <label>模型名称<input value={modelDraft.model_id} onChange={event => setModelDraft(current => current ? ({ ...current, model_id: event.target.value }) : current)} /></label>
                            <label>模型类型<select value={modelDraft.model_type} onChange={event => setModelDraft(current => current ? ({ ...current, model_type: event.target.value as ModelType }) : current)}>{MODEL_TYPES.map(type => <option key={type} value={type}>{modelTypeLabel(type)}</option>)}</select></label>
                            <label>服务连接<select value={modelDraft.connection_id || ""} onChange={event => setModelDraft(current => current ? ({ ...current, connection_id: event.target.value || null }) : current)}><option value="">适配器默认配置</option>{providerConnections.map(connection => <option key={connection.connection_id} value={connection.connection_id}>{connection.name}</option>)}</select></label>
                          </div>
                          <div className="rw-toggle-stack">
                            <label><span><strong>启用模型</strong><small>出现在可用模型列表中</small></span><input type="checkbox" checked={modelDraft.is_active} onChange={event => setModelDraft(current => current ? ({ ...current, is_active: event.target.checked }) : current)} /></label>
                            <label><span><strong>thinking</strong><small>传递推理模式标志</small></span><input type="checkbox" checked={modelDraft.thinking} onChange={event => setModelDraft(current => current ? ({ ...current, thinking: event.target.checked }) : current)} /></label>
                            <label><span><strong>默认路由</strong><small>作为聊天请求的首选模型</small></span><input type="checkbox" checked={modelDraft.is_default} onChange={event => setModelDraft(current => current ? ({ ...current, is_default: event.target.checked }) : current)} /></label>
                          </div>
                          <div className="rw-probe-summary"><span>最近验证</span><strong>{capabilityLabel(selectedModel).text}</strong><small>{selectedModel.capability?.latency_ms ? `${selectedModel.capability.latency_ms} ms` : "尚无延迟数据"}</small></div>
                          <button type="button" className="rw-primary rw-save-model" onClick={() => void saveModel()} disabled={busyKey === `save-model-${selectedModel.id}`}>保存模型配置</button>
                          <button type="button" className="rw-danger-link" onClick={() => void removeModel(selectedModel)} disabled={busyKey === `delete-model-${selectedModel.id}`}><Trash2 aria-hidden="true" />删除模型</button>
                        </>
                      ) : (
                        <div className="rw-empty compact"><p>当前连接还没有可编辑的模型。</p></div>
                      )}
                    </section>
                  </>
                ) : (
                  <div className="rw-empty"><p>请先选择协议适配器。</p></div>
                )}
              </aside>
            </div>
          )}

          <footer className="rw-footer"><span>配置层级</span><code>adapter → connection → model</code><p>仅调用现有 API，不改变后端契约。</p></footer>
        </div>
      </DialogContent>
    </Dialog>
  )
}
