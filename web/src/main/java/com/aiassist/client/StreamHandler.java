package com.aiassist.client;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * AI 模块 SSE 流的回调接口（所有方法默认空实现，按需覆写）。
 *
 * * 帧契约与 AI 模块 /ai/chat/stream 一一对应：
 * meta       —— 流开始，携带模型名
 * reasoning  —— 思维链增量（思考模式，0..N 次）
 * token      —— 正文增量（0..N 次）
 * tool_call  —— 模型发起一次工具调用（args 解析失败为 null，rawArgs 带原始串）
 * confirm    —— 写/执行工具的人机确认请求（阶段3），原样透传前端，web 不理解其语义
 * tool_result —— 与 tool_call 同 id 配对的执行结果（status=success|error）
 *
 * done/error 两种终止帧由 AiClient 内部处理（正常返回 / 抛异常），不暴露给本接口。
 */
public interface StreamHandler {

    default void onMeta(String model) {
    }

    default void onToken(String delta) {
    }

    default void onReasoning(String delta) {
    }

    default void onToolCall(String id, String name, JsonNode args, String rawArgs) {
    }

    /** 阶段3：写/执行工具的确认请求帧 {id, tool, summary}，仅透传 */
    default void onConfirm(String id, String tool, String summary) {
    }

    default void onToolResult(String id, String name, String status,
                              String output, boolean truncated) {
    }
}
