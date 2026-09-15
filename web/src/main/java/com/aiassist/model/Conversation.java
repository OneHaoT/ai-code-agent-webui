package com.aiassist.model;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/**
 * 一个对话: 拥有独立记忆的消息列表。
 * 纯 API 视图对象（持久化由 persistence 层实体负责）：
 * 列表接口返回时 messages 为空列表（不带消息），详情接口才完整填充。
 */
public class Conversation {

    private String id;
    private String title;
    /** 绑定的 Agent 工作区绝对路径（null/空串表示走 AI 默认工作区） */
    private String workspaceRoot;
    private Instant createdAt;
    private Instant updatedAt;
    private List<Message> messages = new ArrayList<>();

    public Conversation() {
    }

    public Conversation(String id, String title) {
        this(id, title, Instant.now(), Instant.now());
    }

    public Conversation(String id, String title, Instant createdAt, Instant updatedAt) {
        this.id = id;
        this.title = title;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }
    public String getWorkspaceRoot() { return workspaceRoot; }
    public void setWorkspaceRoot(String workspaceRoot) { this.workspaceRoot = workspaceRoot; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
    public List<Message> getMessages() { return messages; }
    public void setMessages(List<Message> messages) { this.messages = messages; }
}
