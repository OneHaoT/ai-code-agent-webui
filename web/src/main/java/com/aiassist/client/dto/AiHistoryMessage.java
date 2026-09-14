package com.aiassist.client.dto;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * AI 对话历史中的一条消息。
 *
 * @param role       "user" | "assistant"（图片只会出现在 user 消息中）
 * @param content    消息文本
 * @param images     图片 data URL 列表；窗口之外的旧消息恒为空列表（不传输字节）
 * @param imageCount 该消息实际携带的图片数量。旧消息被 AI 模块折叠进摘要时，
 *                   仅靠该计数在摘要中保留“曾附带 N 张图片”的事实，无需传 data URL
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record AiHistoryMessage(
        String role,
        String content,
        List<String> images,
        @JsonProperty("image_count") int imageCount
) {

    public AiHistoryMessage(String role, String content, List<String> images) {
        this(role, content, images, images == null ? 0 : images.size());
    }

    public AiHistoryMessage(String role, String content) {
        this(role, content, List.of(), 0);
    }
}
