import * as React from "react"

import { cn } from "@/lib/utils"

function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn(
        "bg-card text-card-foreground flex flex-col gap-6 rounded-lg border border-border py-6 transition-[background-color,border-color] duration-150",
        className
      )}
      {...props}
    />
  )
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "@container/card-header grid auto-rows-min grid-rows-[auto_auto] items-start gap-2 px-6 has-data-[slot=card-action]:grid-cols-[1fr_auto] [.border-b]:pb-6",
        className
      )}
      {...props}
    />
  )
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn("leading-none font-semibold tracking-tight", className)}
      {...props}
    />
  )
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-muted-foreground text-sm", className)}
      {...props}
    />
  )
}

function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        className
      )}
      {...props}
    />
  )
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-content"
      className={cn("px-6", className)}
      {...props}
    />
  )
}

function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn("flex items-center px-6 [.border-t]:pt-6", className)}
      {...props}
    />
  )
}

// Glass card variant (enhanced glass effect)
function GlassCard({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="glass-card"
      className={cn(
        "flex flex-col gap-6 rounded-lg border border-border bg-card py-6 transition-[background-color,border-color] duration-150",
        className
      )}
      {...props}
    />
  )
}

// Highlight card (for selected state)
function HighlightCard({ className, active, ...props }: React.ComponentProps<"div"> & { active?: boolean }) {
  return (
    <div
      data-slot="highlight-card"
      data-active={active}
      className={cn(
        "flex flex-col gap-4 rounded-lg border border-transparent bg-muted/35 p-4 transition-[background-color,border-color] duration-150 cursor-pointer",
        "hover:bg-muted/70 hover:border-border",
        // Active state
        active && [
          "border-primary/30 bg-primary/8",
        ],
        className
      )}
      {...props}
    />
  )
}

// Data card (for displaying data)
function DataCard({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="data-card"
      className={cn(
        "rounded-lg border border-border bg-card p-5 transition-[background-color,border-color] duration-150",
        className
      )}
      {...props}
    />
  )
}

export {
  Card,
  CardHeader,
  CardFooter,
  CardTitle,
  CardAction,
  CardDescription,
  CardContent,
  GlassCard,
  HighlightCard,
  DataCard,
}
