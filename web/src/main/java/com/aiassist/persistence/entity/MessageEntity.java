package com.aiassist.persistence.entity;

import java.time.Instant;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 消息实体，对应表 message（扁平行映射，不含关联对象）。
 * seq 为会话内序号（唯一键 conversation_id+seq 防并发重号）；
 * 自增 id 仅为内部主键，不暴露给前端。
 * content/reasoning 为 LONGTEXT，String 直接映射，无方言特殊处理。
 */
@TableName("message")
public class MessageEntity {

    /** 数据库自增主键，insert 后由 useGeneratedKeys 回填 */
    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    private String conversationId;

    private Integer seq;

    private String role;

    private String content;

    private String reasoning;

    /** ToolStep 列表 JSON（ConversationStore 手工序列化）；普通问答/旧行为 NULL */
    private String toolTrace;

    private Instant createdAt;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getConversationId() { return conversationId; }
    public void setConversationId(String conversationId) { this.conversationId = conversationId; }
    public Integer getSeq() { return seq; }
    public void setSeq(Integer seq) { this.seq = seq; }
    public String getRole() { return role; }
    public void setRole(String role) { this.role = role; }
    public String getContent() { return content; }
    public void setContent(String content) { this.content = content; }
    public String getReasoning() { return reasoning; }
    public void setReasoning(String reasoning) { this.reasoning = reasoning; }
    public String getToolTrace() { return toolTrace; }
    public void setToolTrace(String toolTrace) { this.toolTrace = toolTrace; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
}
