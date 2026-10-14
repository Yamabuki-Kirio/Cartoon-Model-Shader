import type { GraphConfidence } from "../../api/types";
import {
  CONFIDENCE_HINTS,
  CONFIDENCE_LABELS,
  CONFIDENCE_TONES,
} from "../../schema/graph";

/**
 * 识别等级芯片。
 *
 * 等级是**服务端判定**，这里只负责让它无法被误读：
 *
 * * 文案固定（已确认 / 推测 / 用户确认 / 实验性 / 不支持），不用同义词；
 * * `data-confidence` 带上原始等级，测试与排障都能断言到确切值；
 * * `title` 说明这个等级**意味着什么**（能不能写、要不要确认）。
 */
export function ConfidenceBadge({
  confidence,
  testId,
}: {
  confidence: GraphConfidence;
  testId?: string;
}) {
  return (
    <span
      class={`chip chip-${CONFIDENCE_TONES[confidence]} confidence-badge`}
      data-testid={testId}
      data-confidence={confidence}
      title={`识别等级：${CONFIDENCE_LABELS[confidence]} —— ${CONFIDENCE_HINTS[confidence]}`}
    >
      {CONFIDENCE_LABELS[confidence]}
    </span>
  );
}
