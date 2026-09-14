package com.aiassist.model;

import java.time.Instant;
import java.util.List;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 对话中的一条消息: 用户(user) 或 AI 回答(assistant)。
 * 用户消息可能携带图片（image 理解），AI 消息的 images 始终为空。
 * assistant 消息可能携带 reasoning（深度思考模式的思维链）。
 */
public class Message {

    private String role;       // "user" | "assistant"
    private String content;
    private List<ImageRef> images;
    // 思考模式下的思维链（仅 assistant 消息可能有值，不回传给大模型）
    @JsonInclude(JsonInclude.Include.NON_NULL)
    private String reasoning;
    // 只读工具调用轨迹（阶段1，仅 assistant 消息可能有值；历史回传大模型时不含此字段）
    @JsonInclude(JsonInclude.Include.NON_NULL)
    private List<ToolStep> toolTrace;
    private Instant createdAt;

    /**
     * 持久化自增主键，以 "id" 字段序列化给前端（流式 done 帧与历史加载共用，
     * 供前端作为 v-for 的稳定 key）。内存态消息（组装中/流式占位）为 null，不输出。
     */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    private Long persistId;

    public Message() {
    }

    public Message(String role, String content) {
        this(role, content, null);
    }

    public Message(String role, String content, List<ImageRef> images) {
        this.role = role;
        this.content = content;
        this.images = images;
        this.createdAt = Instant.now();
    }

    public String getRole() { return role; }
    public void setRole(String role) { this.role = role; }
    public String getContent() { return content; }
    public void setContent(String content) { this.content = content; }
    public List<ImageRef> getImages() { return images; }
    public void setImages(List<ImageRef> images) { this.images = images; }
    public String getReasoning() { return reasoning; }
    public void setReasoning(String reasoning) { this.reasoning = reasoning; }
    public List<ToolStep> getToolTrace() { return toolTrace; }
    public void setToolTrace(List<ToolStep> toolTrace) { this.toolTrace = toolTrace; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    @JsonProperty("id")
    public Long getPersistId() { return persistId; }
    public void setPersistId(Long persistId) { this.persistId = persistId; }
}
