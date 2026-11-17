import { useI18n } from "@/i18n"
import { cn } from "@/lib/utils"

export type SciFiLoaderProps = {
  className?: string
  showText?: boolean
}

/**
 * Quiet, product-scale loading indicator. The exported name remains unchanged
 * to preserve existing call sites while removing the previous neon treatment.
 */
export function SciFiLoader({
  className,
  showText = true,
}: SciFiLoaderProps) {
  const { t } = useI18n()

  return (
    <div
      className={cn("flex flex-col items-center justify-center gap-3", className)}
      role="status"
      aria-live="polite"
    >
      <span className="relative block size-8" aria-hidden="true">
        <span className="absolute inset-0 rounded-full border-2 border-border" />
        <span className="absolute inset-0 animate-spin rounded-full border-2 border-transparent border-t-primary" />
      </span>
      {showText ? (
        <span className="text-xs font-medium text-muted-foreground">
          {t("loader.title")}
        </span>
      ) : (
        <span className="sr-only">{t("loader.title")}</span>
      )}
    </div>
  )
}
