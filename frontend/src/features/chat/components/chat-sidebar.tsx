import { useState } from "react"
import { MessageSquarePlus, MoreHorizontal, PencilLine, Trash2, History, ChevronsLeft, ChevronsRight, ChevronsDown, Search } from "lucide-react"

import type { ConversationInDB } from "@/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
  useSidebar,
} from "@/components/ui/sidebar"
import { AgentHubLogo } from "@/components/agenthub-logo"

import { formatUpdatedAt } from "@/features/chat/utils"
import { useI18n } from "@/i18n"

import type { UserInfo } from "@/types"

type ChatSidebarProps = {
  threadId: string
  conversations: ConversationInDB[]
  onOpenConversation: (conversation: ConversationInDB) => void
  onRenameConversation: (conversation: ConversationInDB) => void
  onDeleteConversation: (conversation: ConversationInDB) => void
  onCreateConversation: () => void
  disableCreateConversation: boolean
  hasMore?: boolean
  isLoadingMore?: boolean
  onLoadMore?: () => void
  onSwitchUser?: () => void
  currentUser?: UserInfo | null
}

// Search component
function SearchInput({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useI18n()
  return (
    <div className="relative group">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-sidebar-foreground/50 transition-colors group-focus-within:text-primary" />
      <input
        type="text"
        placeholder={t("conversation.search") || "Search conversations..."}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full h-9 pl-9 pr-3 rounded-lg bg-background/70 border border-sidebar-border
                   text-sm text-sidebar-foreground placeholder:text-sidebar-foreground/40
                   transition-[background-color,border-color,box-shadow] duration-150
                   focus:outline-none focus:border-primary/60 focus:bg-background
                   focus:ring-2 focus:ring-primary/10
                   hover:bg-background"
      />
    </div>
  )
}

export function ChatSidebar({
  threadId,
  conversations,
  onOpenConversation,
  onRenameConversation,
  onDeleteConversation,
  onCreateConversation,
  disableCreateConversation,
  hasMore: hasMoreProp,
  isLoadingMore,
  onLoadMore,
  onSwitchUser,
  currentUser,
}: ChatSidebarProps) {
  const { locale, t } = useI18n()
  const { state, toggleSidebar } = useSidebar()
  const isCollapsed = state === "collapsed"

  // Search state
  const [searchQuery, setSearchQuery] = useState("")

  // Filter conversations by search query
  const filteredConversations = searchQuery
    ? conversations.filter((c) =>
      c.title.toLowerCase().includes(searchQuery.toLowerCase())
    )
    : conversations

  // Use prop hasMore if provided (server-side pagination), otherwise calculate from search results
  const hasMore = hasMoreProp !== undefined ? hasMoreProp : false

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader className={isCollapsed ? "gap-2" : "gap-2 p-3"}>
        {isCollapsed ? (
          <div className="flex items-center justify-center py-2">
            <button
              onClick={() => toggleSidebar()}
              title={t("sidebar.expand") || "Expand sidebar"}
              className="size-8 flex items-center justify-center rounded-lg transition-colors duration-150 hover:bg-sidebar-accent cursor-pointer"
            >
              <div className="flex size-8 items-center justify-center rounded-lg bg-foreground text-xs font-semibold text-background">
                AH
              </div>
            </button>
          </div>
        ) : (
          <div className="flex w-full items-center justify-between gap-3">
            <a
              href="/"
              title={t("sidebar.logoAlt")}
              className="flex items-center"
            >
              <AgentHubLogo size="sm" className="h-9" />
            </a>
            {/* Collapse button with << arrow */}
            <Button
              variant="ghost"
              size="icon"
              className="size-8 cursor-pointer rounded-md"
              onClick={() => toggleSidebar()}
              title={t("sidebar.collapse") || "Collapse sidebar"}
            >
              <ChevronsLeft className="size-4" />
            </Button>
          </div>
        )}
      </SidebarHeader>

      <SidebarSeparator />

      <SidebarContent className={isCollapsed ? "pb-3" : "px-1 pb-3"}>
        <SidebarGroup className="mt-2 border-b border-sidebar-border pb-3 pt-2">
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  className="cursor-pointer"
                  tooltip={t("conversation.new")}
                  onClick={onCreateConversation}
                  disabled={disableCreateConversation}
                >
                  <MessageSquarePlus className="size-4" />
                  <span>{t("conversation.new")}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              {/* Recent history icon when collapsed - below new conversation */}
              {isCollapsed && (
                <SidebarMenuItem>
                  <SidebarMenuButton
                    className="cursor-pointer"
                    tooltip={t("conversation.recent")}
                    onClick={() => toggleSidebar()}
                  >
                    <History className="size-4" />
                    <span>{t("conversation.recent")}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup className="pt-3 group-data-[collapsible=icon]:hidden">
          <SidebarGroupLabel className="px-2">{t("conversation.recent")}</SidebarGroupLabel>
          <SidebarGroupContent>
            {/* Search input */}
            <div className="px-1 mb-3">
              <SearchInput value={searchQuery} onChange={setSearchQuery} />
            </div>

            {filteredConversations.length === 0 ? (
              <p className="mt-3 rounded-xl border border-dashed border-sidebar-border/50 p-4 text-sm text-sidebar-foreground/60 text-center">
                {searchQuery
                  ? (t("conversation.noResults") || "No conversations found")
                  : t("conversation.none")}
              </p>
            ) : (
              <SidebarMenu>
                {filteredConversations.map((conversation) => {
                  const isActive = conversation.thread_id === threadId

                  return (
                    <SidebarMenuItem key={conversation.thread_id}>
                      <div className="group/item relative">
                        <SidebarMenuButton
                          isActive={isActive}
                          className={`h-auto min-h-14 items-start border-l-2 py-2.5 pr-10 cursor-pointer rounded-none
                                     transition-[background-color,color] duration-150
                                     ${isActive
                              ? 'border-primary bg-sidebar-accent text-sidebar-accent-foreground'
                              : 'border-transparent hover:bg-sidebar-accent/65'}`}
                          onClick={() => onOpenConversation(conversation)}
                        >
                          <div className="min-w-0">
                            <p className={`line-clamp-1 text-[15px] transition-colors duration-200 ${isActive ? 'font-semibold text-sidebar-foreground' : 'font-medium text-sidebar-foreground/90'}`}>
                              {conversation.title}
                            </p>
                            <p className="text-xs text-sidebar-foreground/50 mt-0.5">
                              {formatUpdatedAt(conversation.updated_at, locale)}
                            </p>
                          </div>
                        </SidebarMenuButton>

                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="absolute top-2 cursor-pointer right-2 size-7 text-sidebar-foreground/50
                                         opacity-0 group-hover/item:opacity-100 transition-[background-color,color,opacity] duration-150
                                         hover:bg-sidebar-accent/80 hover:text-sidebar-foreground
                                         rounded-md"
                              onClick={(event) => {
                                event.stopPropagation()
                              }}
                              aria-label={t("conversation.actions")}
                            >
                              <MoreHorizontal className="size-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" className="w-36">
                            <DropdownMenuItem
                              className="cursor-pointer"
                              onClick={() => onRenameConversation(conversation)}
                            >
                              <PencilLine className="size-4" />
                              {t("common.rename")}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              variant="destructive"
                              className="cursor-pointer"
                              onClick={() => onDeleteConversation(conversation)}
                            >
                              <Trash2 className="size-4" />
                              {t("common.delete")}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </SidebarMenuItem>
                  )
                })}

                {/* Load more button */}
                {hasMore && onLoadMore && (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      className="cursor-pointer justify-center"
                      onClick={onLoadMore}
                      disabled={isLoadingMore}
                    >
                      {isLoadingMore ? (
                        <span className="text-xs">{t("common.loading") || "Loading..."}</span>
                      ) : (
                        <ChevronsDown className="size-5" />
                      )}
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )}
              </SidebarMenu>
            )}
          </SidebarGroupContent>
        </SidebarGroup>

      </SidebarContent>

      {/* Footer with expand button and user switch - fixed at bottom */}
      <SidebarFooter className={isCollapsed ? "p-2" : "p-3"}>
        {/* Expand button when collapsed */}
        {isCollapsed && (
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                className="cursor-pointer"
                tooltip={t("sidebar.expand") || "Expand sidebar"}
                onClick={() => toggleSidebar()}
              >
                <ChevronsRight className="size-4" />
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        )}

        {/* User switch button */}
        {onSwitchUser && (
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                className="cursor-pointer"
                tooltip={isCollapsed ? (currentUser?.name ?? t("user.switchUser")) : undefined}
                onClick={onSwitchUser}
              >
                <div className="flex size-6 items-center justify-center rounded-full bg-muted text-xs shrink-0">
                  {currentUser?.avatar || currentUser?.name?.slice(0, 1).toUpperCase() || "U"}
                </div>
                {!isCollapsed && (
                  <span className="truncate">{currentUser?.name ?? t("user.switchUser")}</span>
                )}
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        )}
      </SidebarFooter>
    </Sidebar>
  )
}
