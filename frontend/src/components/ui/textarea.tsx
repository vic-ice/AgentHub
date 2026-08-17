import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        // Base styles with modern design
        "flex field-sizing-content min-h-24 w-full rounded-md px-3.5 py-3 text-[15px] leading-7",
        // Border and background
        "border border-input bg-card dark:bg-white/[0.03]",
        // Placeholder
        "placeholder:text-[var(--text-dim)]",
        // Transitions
        "transition-[background-color,border-color,box-shadow] duration-150 outline-none",
        // Focus state with warm glow
        "focus:border-primary/60 focus:ring-2 focus:ring-primary/10",
        // Hover state
        "hover:border-foreground/25",
        // Disabled state
        "disabled:cursor-not-allowed disabled:opacity-50",
        // Text color
        "text-[var(--text-main)]",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
