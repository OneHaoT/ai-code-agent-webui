package com.aiassist.persistence.entity;

import java.time.Instant;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 对话实体，对应表 conversation。
 * 主键沿用应用层生成的 32 位 UUID 字符串（与前端/AI 模块现有 ID 格式保持一致）。
 * 列名映射由 map-underscore-to-camel-case 完成（created_at -> createdAt）。
 */
@TableName("conversation")
public class ConversationEntity {

    /** 应用层生成 UUID 字符串，非数据库自增，用 INPUT 手动赋值 */
    @TableId(value = "id", type = IdType.INPUT)
    private String id;

    private String title;

    private String workspaceRoot;

    private Instant createdAt;

    private Instant updatedAt;

    public ConversationEntity() {
    }

    public ConversationEntity(String id, String title, Instant createdAt, Instant updatedAt) {
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
}
