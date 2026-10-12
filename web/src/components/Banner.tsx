import type { ComponentChildren } from "preact";

export type BannerTone = "info" | "warn" | "error" | "ok";

export interface BannerProps {
  tone: BannerTone;
  title?: string;
  children: ComponentChildren;
  onDismiss?: () => void;
  testId?: string;
}

/** 不可忽略的状态横幅：基线失效、结构变化、回滚失败、外部改动都走它。 */
export function Banner({ tone, title, children, onDismiss, testId }: BannerProps) {
  return (
    <div class={`banner banner-${tone}`} role={tone === "error" ? "alert" : "status"} data-testid={testId}>
      {title ? <strong class="banner-title">{title}</strong> : null}
      <div class="banner-body">{children}</div>
      {onDismiss ? (
        <button type="button" class="banner-dismiss" onClick={onDismiss} aria-label="关闭提示">
          ×
        </button>
      ) : null}
    </div>
  );
}
