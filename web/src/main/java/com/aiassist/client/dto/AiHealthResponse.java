package com.aiassist.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * AI 模块 GET /health 的响应体。
 *
 * @param status          服务状态（"ok" 等）
 * @param model           当前模型名
 * @param baseUrl         DeepSeek 接口地址
 * @param keyConfigured   API Key 是否已配置
 * @param error           不可达时的错误信息（正常响应为 null）
 * @param toolsEnabled    只读工具总开关
 * @param defaultWorkspace 默认工作区绝对路径（阶段2新增）
 * @param maxToolIterations 工具循环最大轮数
 */
public record AiHealthResponse(
        String status,
        String model,
        @JsonProperty("base_url") String baseUrl,
        @JsonProperty("key_configured") Boolean keyConfigured,
        String error,
        @JsonProperty("tools_enabled") Boolean toolsEnabled,
        @JsonProperty("default_workspace") String defaultWorkspace,
        @JsonProperty("max_tool_iterations") Integer maxToolIterations
) {
    /** AI 模块不可达时的降级响应 */
    public static AiHealthResponse unreachable(String message) {
        return new AiHealthResponse("unreachable", null, null, null, message, null, null, null);
    }
}
