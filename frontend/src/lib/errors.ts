export type ErrorDetails = {
  message: string
  code?: string
  requestId?: string
  stage?: string
  retryable?: boolean
  status?: number
}

type ApiErrorPayload = {
  detail?: unknown
  error_type?: string
  error_code?: string
  request_id?: string
  stage?: string
  retryable?: boolean
}

function detailToMessage(detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim()
  if (Array.isArray(detail)) {
    const messages = detail.slice(0, 3).map(item => {
      if (!item || typeof item !== "object") return String(item)
      const record = item as Record<string, unknown>
      const location = Array.isArray(record.loc)
        ? record.loc.filter(part => part !== "body").join(".")
        : ""
      const message = typeof record.msg === "string" ? record.msg : "参数无效"
      return location ? `${location}：${message}` : message
    })
    return messages.join("；")
  }
  if (detail && typeof detail === "object") {
    try {
      return JSON.stringify(detail)
    } catch {
      return "请求处理失败"
    }
  }
  return "请求处理失败"
}

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly requestId?: string
  readonly stage?: string
  readonly retryable: boolean

  constructor(details: ErrorDetails) {
    super(details.message)
    this.name = "ApiError"
    this.status = details.status ?? 0
    this.code = details.code
    this.requestId = details.requestId
    this.stage = details.stage
    this.retryable = details.retryable ?? false
  }
}

export function createClientRequestId(): string {
  return `web-${crypto.randomUUID()}`
}

export async function throwApiError(response: Response): Promise<never> {
  let payload: ApiErrorPayload = {}
  try {
    payload = (await response.json()) as ApiErrorPayload
  } catch {
    // Non-JSON proxy/server responses still retain HTTP status and response id.
  }

  const fallback = response.status >= 500
    ? "服务暂时不可用，请稍后重试"
    : `请求失败（HTTP ${response.status}）`
  throw new ApiError({
    message: payload.detail === undefined ? fallback : detailToMessage(payload.detail),
    status: response.status,
    code: payload.error_code ?? payload.error_type ?? `http_${response.status}`,
    requestId: payload.request_id ?? response.headers.get("X-Request-ID") ?? undefined,
    stage: payload.stage,
    retryable: payload.retryable ?? response.status >= 500,
  })
}

export function getErrorDetails(
  error: unknown,
  fallback = "操作失败，请稍后重试",
): ErrorDetails {
  if (error instanceof ApiError) {
    return {
      message: error.message || fallback,
      status: error.status,
      code: error.code,
      requestId: error.requestId,
      stage: error.stage,
      retryable: error.retryable,
    }
  }
  if (error instanceof TypeError && /fetch|network|load/i.test(error.message)) {
    return {
      message: "无法连接到服务，请检查后端是否运行及网络连接",
      code: "network_unreachable",
      stage: "network",
      retryable: true,
    }
  }
  if (error instanceof Error) return { message: error.message || fallback }
  return { message: fallback }
}

export function formatErrorForDisplay(
  error: unknown,
  fallback = "操作失败，请稍后重试",
): string {
  const details = getErrorDetails(error, fallback)
  const diagnostics = [
    details.code ? `错误码：${details.code}` : "",
    details.requestId ? `请求 ID：${details.requestId}` : "",
  ].filter(Boolean)
  return diagnostics.length > 0
    ? `${details.message}\n${diagnostics.join(" · ")}`
    : details.message
}

export function formatDiagnosticDetails(details: ErrorDetails): string {
  const diagnostics = [
    details.code ? `错误码：${details.code}` : "",
    details.requestId ? `请求 ID：${details.requestId}` : "",
  ].filter(Boolean)
  return diagnostics.length > 0
    ? `${details.message}\n${diagnostics.join(" · ")}`
    : details.message
}
