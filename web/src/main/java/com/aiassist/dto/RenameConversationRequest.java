package com.aiassist.dto;

/**
 * 手动重命名对话请求体。
 * title 必填（去空白后非空），长度上限由 Service 层校验。
 */
public record RenameConversationRequest(String title) {
}
