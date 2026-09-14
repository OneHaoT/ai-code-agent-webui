package com.aiassist.dto;

/**
 * 新建对话请求体（title 可空，空则由后端用首条消息自动命名）
 */
public record CreateConversationRequest(String title) {
}
