package com.aiassist.dto;

/**
 * 新建对话请求体（title 可空，空则由后端用首条消息自动命名；
 * workspaceRoot 可空，空则该对话使用 AI 默认工作区）
 */
public record CreateConversationRequest(String title, String workspaceRoot) {
}
