import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowDown, SearchCheck, XIcon } from "lucide-react"

import type { CompletedExecutionStep, LocalChatMessage, ToolCallInfo, ModelInfo } from "@/types"
import { ModelSelector } from "@/features/chat/components/model-selector"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
} from "@/components/ai/prompt-input"
import { ChatMessageItem } from "@/features/chat/components/chat-message-item"
import { ExecutionProgressCard } from "@/features/chat/components/execution-progress-card"
import { SciFiLoader } from "@/components/ai/neural-network-loader"
import { useI18n } from "@/i18n"
import { cn } from "@/lib/utils"
import type {
  FollowUpQuestionOption,
  FollowUpSendContext,
} from "@/features/chat/recommendation-followups"


type ChatMainPanelProps = {
  appError: string | null
  onDismissError: () => void
  isStreaming: boolean
  isInitializing: boolean
  isLoadingConversation: boolean
  isProcessing: boolean // Processing, no content received yet
  isAgentThinking: boolean
  calledTools: ToolCallInfo[]
  liveExecutionSteps: CompletedExecutionStep[]
  thinkingContent: string // Accumulated thinking content
  messages: LocalChatMessage[]
  selectedRequestId?: string | null // Currently selected request_id for DAG viewing
  onSendMessage: (
    rawInput: string,
    quotedMessageId?: string,
    userContent?: string,
    followUpContext?: FollowUpSendContext,
    researchMode?: boolean,
  ) => Promise<void>
  onStopStreaming: () => void
  onJumpToMessage?: (localId: string) => void // Jump to quoted message callback
  onToggleSidebarProcess?: () => void // Toggle sidebar process panel visibility
  onSelectRequestId?: (requestId: string | null) => void // Select request_id for DAG viewing
  // Model selection props
  models: ModelInfo[]
  selectedModel: string | null
  onSelectModel: (modelId: string | null) => void
  onOpenModelConfig?: () => void // Open model configuration dialog
  hasAvailableModels?: boolean // Whether there are available models to select from
}

const SCROLL_BOTTOM_HIDE_THRESHOLD = 24
const SCROLL_BUTTON_SHOW_OFFSET = 180
const SCROLLBAR_FADE_OUT_DELAY = 420
const STREAM_SCROLL_EASING = 0.2
const STREAM_SCROLL_MIN_STEP = 1
const USER_SCROLL_INTERRUPT_DELTA = -2

export function ChatMainPanel({
  appError,
  onDismissError,
  isStreaming,
  isInitializing,
  isLoadingConversation,
  isProcessing,
  isAgentThinking,
  calledTools,
  liveExecutionSteps,
  thinkingContent,
  messages,
  selectedRequestId,
  onSendMessage,
  onStopStreaming,
  onJumpToMessage,
  onToggleSidebarProcess,
  onSelectRequestId,
  models,
  selectedModel,
  onSelectModel,
  onOpenModelConfig,
  hasAvailableModels = true,
}: ChatMainPanelProps) {
  const { t } = useI18n()
  const [inputValue, setInputValue] = useState("")
  const [researchMode, setResearchMode] = useState(false)

  // Quote state - show quoted content above input
  const [quotedContent, setQuotedContent] = useState<string | null>(null)
  const [quotedMessageId, setQuotedMessageId] = useState<string | null>(null)

  const endOfMessagesRef = useRef<HTMLDivElement | null>(null)
  const conversationRef = useRef<HTMLDivElement | null>(null)
  const scrollbarHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const streamFollowRafRef = useRef<number | null>(null)
  const autoScrollEnabledRef = useRef(true)
  const isStreamingRef = useRef(isStreaming)
  const lastKnownScrollTopRef = useRef(0)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const [isMessagesScrolling, setIsMessagesScrolling] = useState(false)

  const suggestions = useMemo(
    () => [
      t("chat.suggestion.1"),
      t("chat.suggestion.2"),
      t("chat.suggestion.3"),
      t("chat.suggestion.4"),
    ],
    [t],
  )

  const status: "streaming" | "submitted" | "ready" = useMemo(() => {
    if (isStreaming) {
      return "streaming"
    }
    if (isInitializing || isLoadingConversation) {
      return "submitted"
    }
    return "ready"
  }, [isInitializing, isLoadingConversation, isStreaming])

  const isComposerDisabled =
    isInitializing || isLoadingConversation || !hasAvailableModels

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const element = conversationRef.current
    if (!element) {
      return
    }
    element.scrollTo({ top: element.scrollHeight, behavior })
  }, [])

  const stopStreamFollow = useCallback(() => {
    if (streamFollowRafRef.current !== null) {
      window.cancelAnimationFrame(streamFollowRafRef.current)
      streamFollowRafRef.current = null
    }
  }, [])

  const startStreamFollow = useCallback(() => {
    if (streamFollowRafRef.current !== null) {
      return
    }

    const step = () => {
      const element = conversationRef.current
      if (!element || !isStreamingRef.current || !autoScrollEnabledRef.current) {
        streamFollowRafRef.current = null
        return
      }

      const targetTop = element.scrollHeight - element.clientHeight
      const distance = targetTop - element.scrollTop

      if (distance <= 0.5) {
        element.scrollTop = targetTop
      } else {
        element.scrollTop += Math.max(STREAM_SCROLL_MIN_STEP, distance * STREAM_SCROLL_EASING)
      }

      streamFollowRafRef.current = window.requestAnimationFrame(step)
    }

    streamFollowRafRef.current = window.requestAnimationFrame(step)
  }, [])

  useEffect(() => {
    isStreamingRef.current = isStreaming
    if (!isStreaming) {
      stopStreamFollow()
    }
  }, [isStreaming, stopStreamFollow])

  useEffect(() => {
    if (isLoadingConversation || !autoScrollEnabledRef.current) {
      return
    }

    if (isStreaming) {
      startStreamFollow()
      return
    }

    scrollToBottom("auto")
  }, [
    isLoadingConversation,
    isStreaming,
    messages,
    scrollToBottom,
    startStreamFollow,
  ])

  useEffect(() => {
    return () => {
      if (scrollbarHideTimerRef.current) {
        clearTimeout(scrollbarHideTimerRef.current)
      }
      stopStreamFollow()
    }
  }, [stopStreamFollow])

  const updateScrollButtonState = useCallback(() => {
    const element = conversationRef.current
    if (!element) {
      return
    }

    setIsMessagesScrolling(true)
    if (scrollbarHideTimerRef.current) {
      clearTimeout(scrollbarHideTimerRef.current)
    }
    scrollbarHideTimerRef.current = setTimeout(() => {
      setIsMessagesScrolling(false)
    }, SCROLLBAR_FADE_OUT_DELAY)

    const distanceFromBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight
    const isAtBottom = distanceFromBottom <= SCROLL_BOTTOM_HIDE_THRESHOLD
    const scrollDelta = element.scrollTop - lastKnownScrollTopRef.current
    lastKnownScrollTopRef.current = element.scrollTop

    if (isAtBottom) {
      autoScrollEnabledRef.current = true
      if (isStreamingRef.current) {
        startStreamFollow()
      }
    } else if (isStreamingRef.current && scrollDelta < USER_SCROLL_INTERRUPT_DELTA) {
      autoScrollEnabledRef.current = false
      stopStreamFollow()
    }

    setShowScrollButton(!isAtBottom && distanceFromBottom > SCROLL_BUTTON_SHOW_OFFSET)
  }, [startStreamFollow, stopStreamFollow])

  const submitMessage = useCallback((rawInput: string, followUpContext?: FollowUpSendContext) => {
    const trimmed = rawInput.trim()
    if (!trimmed || isStreaming || isComposerDisabled) {
      return
    }

    // If there's quoted content, format the message and pass quotedMessageId
    const finalContent = quotedContent
      ? `> ${quotedContent}\n\n${trimmed}`
      : trimmed

    setInputValue("")
    const currentQuotedMessageId = quotedMessageId
    const currentResearchMode = researchMode
    setQuotedContent(null)
    setQuotedMessageId(null)
    setResearchMode(false)

    // Pass quotedMessageId, userContent and one-shot research mode
    void onSendMessage(
      finalContent,
      currentQuotedMessageId || undefined,
      trimmed,
      followUpContext,
      currentResearchMode,
    )
  }, [isComposerDisabled, isStreaming, onSendMessage, quotedContent, quotedMessageId, researchMode])

  const handleSuggestionClick = useCallback(
    (value: string) => {
      if (isStreaming || isComposerDisabled) {
        return
      }
      submitMessage(value)
    },
    [isComposerDisabled, isStreaming, submitMessage],
  )

  const handleFollowUpQuestionClick = useCallback(
    (question: FollowUpQuestionOption, message: LocalChatMessage) => {
      if (isStreaming || isComposerDisabled) {
        return
      }
      submitMessage(question.question, {
        followUpQuestion: question,
        parentMessageId: message.local_id,
        parentRequestId: message.request_id ?? null,
      })
    },
    [isComposerDisabled, isStreaming, submitMessage],
  )

  const submitButtonDisabled = isStreaming
    ? false
    : !inputValue.trim() || status !== "ready" || isComposerDisabled
  const shouldShowScrollButton =
    showScrollButton && !isLoadingConversation
  const retryText = useMemo(() => {
    const latestUserMessage = [...messages].reverse().find((message) => message.type === "human")
    if (!latestUserMessage) return ""
    const userContent = latestUserMessage.custom_data?.user_content
    return typeof userContent === "string" && userContent.trim()
      ? userContent.trim()
      : latestUserMessage.content.trim()
  }, [messages])

  // Clear quoted content
  const clearQuote = useCallback(() => {
    setQuotedContent(null)
    setQuotedMessageId(null)
  }, [])

  // Check if there are messages (for layout decision)
  const hasMessages = messages.length > 0

  return (
    <section className={[
      "grid h-full min-h-0 min-w-0 flex-1 overflow-hidden bg-background",
      hasMessages ? "grid-rows-[minmax(0,1fr)_auto]" : "grid-rows-[0fr_1fr]"
    ].join(" ")}>
      <div
        className="flex min-h-0 flex-1 flex-col overflow-hidden bg-background px-4 pb-2 md:px-6"
      >
        {appError ? (
          <Alert variant="destructive" className="mt-4">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <AlertTitle>{t("chat.requestFailed")}</AlertTitle>
                <AlertDescription className="mt-1 whitespace-pre-line">{appError}</AlertDescription>
                <div className="mt-3 flex flex-wrap gap-2">
                  {retryText && !isStreaming ? (
                    <Button type="button" size="sm" variant="outline" onClick={() => submitMessage(retryText)}>
                      重新发送
                    </Button>
                  ) : null}
                  <Button type="button" size="sm" variant="ghost" onClick={onDismissError}>
                    暂时关闭
                  </Button>
                </div>
              </div>
              <Button type="button" size="icon" variant="ghost" className="size-8 shrink-0" onClick={onDismissError} aria-label="关闭错误提示">
                <XIcon className="size-4" />
              </Button>
            </div>
          </Alert>
        ) : null}

        <div className="relative min-h-0 flex-1">
          <div
            ref={conversationRef}
            className={[
              "chat-messages-scroll-area mx-auto h-full max-w-5xl overflow-y-auto",
              isMessagesScrolling ? "is-scrolling" : "",
            ].join(" ")}
            onScroll={updateScrollButtonState}
          >
            {isLoadingConversation && messages.length === 0 ? (
              <div className="flex h-full w-full items-center justify-center">
                <SciFiLoader className="w-32 h-32" showText={false} />
              </div>
            ) : (
              <div
                className={cn(
                  "mx-auto flex w-full flex-col gap-6 pb-5 px-4 pt-8 transition-opacity duration-200",
                  isLoadingConversation && "opacity-50 pointer-events-none"
                )}
              >
                {messages.length === 0 ? null : (
                  messages.map((message, index) => {
                    const isLastAIMessage = index === messages.length - 1 && message.type === "ai"

                    // Determine if this message is "selected" (its DAG is shown in sidebar):
                    // 1. During streaming, the last AI message is always "selected" 
                    // 2. When user clicks a brain icon, selectedRequestId is set - check if this message's request_id matches
                    // 3. When sidebar shows default (selectedRequestId is null): last AI message with request_id is selected
                    const isMessageSelected = message.type === "ai" && (
                      (isLastAIMessage && isStreaming)
                        ? true
                        : selectedRequestId !== null
                          ? message.request_id === selectedRequestId  // User clicked this message's brain icon
                          : isLastAIMessage && Boolean(message.request_id)  // Default: show last AI message with request_id
                    )

                    return (
                      <ChatMessageItem
                        key={`msg-${index}`}
                        message={{ ...message, local_id: `msg-${index}` }}
                        calledTools={isLastAIMessage ? calledTools : []}
                        executionSteps={isLastAIMessage ? liveExecutionSteps : []}
                        isAgentThinking={isLastAIMessage ? isAgentThinking : false}
                        thinkingContent={isLastAIMessage ? thinkingContent : ""}
                        isProcessing={isLastAIMessage && isProcessing}
                        isStreaming={message.is_streaming}
                        isSelected={isMessageSelected}
                        onJumpToMessage={onJumpToMessage}
                        onToggleSidebarProcess={onToggleSidebarProcess}
                        onSelectRequestId={onSelectRequestId}
                        onFollowUpQuestionClick={handleFollowUpQuestionClick}
                      />
                    )
                  })
                )}

                <ExecutionProgressCard
                  isStreaming={isStreaming}
                  isProcessing={isProcessing}
                  isAgentThinking={isAgentThinking}
                  steps={liveExecutionSteps}
                  tools={calledTools}
                  onStop={onStopStreaming}
                />

                <div ref={endOfMessagesRef} />
              </div>
            )}
          </div>
          {hasMessages ? (
            <div
              aria-hidden="true"
              className="chat-messages-bottom-fade pointer-events-none absolute inset-x-0 bottom-0 z-10 mx-auto h-8 max-w-5xl"
            />
          ) : null}


        </div>

      </div>

      <footer className={[
        "relative z-20 bg-background transition-[height,background-color] duration-200",
        hasMessages ? "" : "h-full flex flex-col items-center justify-center"
      ].join(" ")}>
        {shouldShowScrollButton && hasMessages ? (
          <Button
            size="icon"
            variant="secondary"
            className="absolute top-0 left-1/2 z-30 cursor-pointer -translate-x-1/2 -translate-y-1/2 rounded-full border border-border bg-card"
            onClick={() => {
              autoScrollEnabledRef.current = true
              setShowScrollButton(false)
              scrollToBottom("smooth")
              if (isStreamingRef.current) {
                startStreamFollow()
              }
            }}
          >
            <ArrowDown className="size-4" />
          </Button>
        ) : null}
        <div className={[
          "mx-auto w-full max-w-5xl space-y-4 overflow-y-auto",
          hasMessages ? "mb-3 p-3" : "p-6"
        ].join(" ")}>
          {messages.length === 0 ? (
            <div className="mx-auto mb-4 max-w-2xl text-center">
              <p className="font-mono text-xs font-semibold tracking-[0.1em] text-primary">新对话</p>
              <h1 className="mt-3 text-4xl font-semibold sm:text-5xl">今天想推进什么？</h1>
              <p className="mt-4 text-[16px] leading-7 text-muted-foreground">直接描述目标；需要系统检索和多步分析时，再开启深度搜索。</p>
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2 justify-center">
            {messages.length === 0 ? (
              suggestions.map((suggestion) => (
                <Button
                  key={suggestion}
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-10 cursor-pointer rounded-full border-border/70 bg-card px-4 hover:bg-muted/60"
                  onClick={() => handleSuggestionClick(suggestion)}
                  disabled={isStreaming || isComposerDisabled}
                >
                  {suggestion}
                </Button>
              ))
            ) : null}
          </div>

          <PromptInput
            className="chat-composer h-auto min-h-14 border-0 bg-transparent [&_[data-slot=input-group]]:rounded-[26px] [&_[data-slot=input-group]]:border-border/70 [&_[data-slot=input-group]]:bg-card [&_[data-slot=input-group]]:shadow-sm [&_[data-slot=input-group]]:transition-[border-color,box-shadow,background-color] [&_[data-slot=input-group]]:duration-150 hover:[&_[data-slot=input-group]]:border-foreground/20 focus-within:[&_[data-slot=input-group]]:border-primary/35 focus-within:[&_[data-slot=input-group]]:shadow-md"
            data-od-id="chat-composer"
            onSubmit={({ text }) => {
              submitMessage(text)
            }}
          >
            {/* Quoted content display above input */}
            {quotedContent ? (
              <div className="relative px-3 pt-3">
                <div className="rounded-2xl border border-border/60 bg-muted/30 p-3">
                  {/* Close button */}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="absolute top-2 right-2 size-5 cursor-pointer"
                    onClick={clearQuote}
                  >
                    <XIcon className="size-3" />
                  </Button>
                  {/* Quoted text - gray, show first 100 chars */}
                  <p className="text-sm text-muted-foreground whitespace-pre-wrap break-words line-clamp-3 pr-6">
                    {quotedContent.length > 100 ? `${quotedContent.slice(0, 100)}_` : quotedContent}
                  </p>
                </div>
                {/* Separator line */}
                <Separator className="mt-3" />
              </div>
            ) : null}

            <PromptInputBody  >
              <PromptInputTextarea
                className="max-h-40 min-h-14 px-4 pb-2 pt-4 text-[16px] leading-7 placeholder:text-muted-foreground/80"
                disabled={isComposerDisabled}
                onChange={(event) => setInputValue(event.currentTarget.value)}
                value={inputValue}
                placeholder={quotedContent ? t("message.addYourMessage") : t("prompt.placeholder")}
              />
            </PromptInputBody>
            <PromptInputFooter className="justify-between gap-3 px-3 pb-3 pt-1">
              <div className="flex min-w-0 items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className={cn(
                    "h-9 shrink-0 cursor-pointer gap-1.5 rounded-full border px-3 text-sm transition-[background-color,border-color,color]",
                    researchMode
                      ? "border-primary/30 bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary"
                      : "border-border/75 bg-muted/35 text-muted-foreground hover:border-foreground/20 hover:bg-muted/70 hover:text-foreground",
                  )}
                  disabled={isStreaming || isInitializing || isLoadingConversation}
                  onClick={() => setResearchMode((prev) => !prev)}
                  title="开启后，本轮将执行深度搜索；发送后自动关闭"
                  aria-pressed={researchMode}
                  data-od-id="deep-search-toggle"
                >
                  <SearchCheck className="size-4" />
                  深度搜索
                </Button>
                {/* Model selector - only show if there are available models */}
                {hasAvailableModels && (
                  <ModelSelector
                    models={models}
                    selectedModel={selectedModel}
                    onSelectModel={onSelectModel}
                    disabled={isStreaming || isInitializing || isLoadingConversation}
                    onOpenConfig={onOpenModelConfig}
                  />
                )}
              </div>
              <PromptInputSubmit
                disabled={submitButtonDisabled}
                onClick={isStreaming ? onStopStreaming : undefined}
                status={status}
                className="size-9 shrink-0 cursor-pointer rounded-full shadow-none"
                type={isStreaming ? "button" : "submit"}
              />
            </PromptInputFooter>
          </PromptInput>
        </div>
      </footer>
    </section>
  )
}
