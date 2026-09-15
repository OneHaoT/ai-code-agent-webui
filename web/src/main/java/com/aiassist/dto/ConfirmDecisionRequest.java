package com.aiassist.dto;

/**
 * confirm 帧用户决策请求体（阶段3 写/执行工具人机确认）。
 * confirmId 非空、approved 非空的校验在 ChatController 中做（违规 400）。
 *
 * @param confirmId confirm 帧携带的 id（AI 进程内注册表键）
 * @param approved  true=确认执行，false=拒绝
 */
public record ConfirmDecisionRequest(
        String confirmId,
        Boolean approved
) {
}
