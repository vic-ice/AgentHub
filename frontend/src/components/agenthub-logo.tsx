import { cn } from "@/lib/utils"

interface AgentHubLogoProps {
  className?: string
  size?: "sm" | "md" | "lg"
}

export function AgentHubLogo({ className, size = "md" }: AgentHubLogoProps) {
  const sizeClasses = {
    sm: "text-xl",
    md: "text-3xl",
    lg: "text-5xl",
  }

  return (
    <span className={cn("inline-flex items-center gap-[0.32em] select-none", sizeClasses[size], className)}>
      <span className="font-semibold tracking-[-0.025em] text-foreground">AgentHub</span>
      <span className="size-[0.28em] rounded-full bg-primary" aria-hidden="true" />
    </span>
  )
}
