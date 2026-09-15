package com.aiassist.client.dto;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 调用 AI 模块 POST /ai/chat 的请求体。
 *
 * @param conversationId 对话 ID（每个对话记忆独立）
 * @param message        当前用户消息文本（纯图片提问时为空字符串）
 * @param images         当前消息图片（data URL 字符串，MIME 已内嵌在 data URL 中）
 * @param history        该会话已有消息历史（按时间顺序）
 * @param thinking       是否开启 DeepSeek 深度思考模式
 * @param workspaceRoot  本轮对话要使用的工作区绝对路径（null=AI 默认工作区）
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record AiChatRequest(
        @JsonProperty("conversation_id") String conversationId,
        String message,
        List<String> images,
        List<AiHistoryMessage> history,
        Boolean thinking,
        @JsonProperty("workspace_root") String workspaceRoot
) {
}
