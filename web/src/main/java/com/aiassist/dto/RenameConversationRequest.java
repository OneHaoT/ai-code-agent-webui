package com.aiassist.dto;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * 更新对话元数据请求体（title 和 workspaceRoot 都可选，只传要改的字段）。
 * null 表示不更新；空字符串视为清空。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record RenameConversationRequest(String title, String workspaceRoot) {
}
