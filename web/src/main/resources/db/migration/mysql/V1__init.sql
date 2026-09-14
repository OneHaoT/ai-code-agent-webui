-- 阶段0：对话/消息/图片引用初始结构
-- 对应后端 model：Conversation / Message / ImageRef（前端 JSON 契约不变）
-- 主键策略：对话沿用应用层生成的 32 位 UUID 字符串；消息/图片引用用 BIGINT 自增（不对外暴露）

CREATE TABLE conversation (
    id         VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT '对话ID（32位UUID）',
    title      VARCHAR(100) COMMENT '标题（首条用户消息自动生成）',
    created_at DATETIME(6)  NOT NULL COMMENT '创建时间(UTC)',
    updated_at DATETIME(6)  NOT NULL COMMENT '最近更新时间(UTC)',
    INDEX idx_conv_updated (updated_at DESC)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '对话';

CREATE TABLE message (
    id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '消息自增主键（内部）',
    conversation_id VARCHAR(32)  NOT NULL COMMENT '所属对话',
    seq             INT          NOT NULL COMMENT '会话内消息序号（从1开始）',
    role            VARCHAR(16)  NOT NULL COMMENT '角色：user | assistant',
    -- LONGTEXT：与实体 @Lob（Hibernate 在 MySQL 映射 LONGTEXT）保持一致，避免 validate 类型不符；
    -- 同时容纳超长回答/思维链（TEXT 上限仅 64KB）
    content         LONGTEXT     COMMENT '文本内容（纯图片消息可为空）',
    reasoning       LONGTEXT     COMMENT '深度思考链（仅 assistant）',
    created_at      DATETIME(6)  NOT NULL COMMENT '创建时间(UTC)',
    CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id)
        REFERENCES conversation (id) ON DELETE CASCADE,
    UNIQUE KEY uk_msg_conv_seq (conversation_id, seq),
    INDEX idx_msg_conv (conversation_id, seq)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '消息';

CREATE TABLE message_image (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '引用自增主键',
    message_id   BIGINT       NOT NULL COMMENT '所属消息',
    seq          INT          NOT NULL COMMENT '消息内图片序号',
    image_id     VARCHAR(64)  NOT NULL COMMENT 'StorageService 图片文件ID',
    filename     VARCHAR(255) NOT NULL COMMENT '原始文件名（展示用）',
    content_type VARCHAR(100) NOT NULL COMMENT 'MIME 类型',
    CONSTRAINT fk_img_msg FOREIGN KEY (message_id)
        REFERENCES message (id) ON DELETE CASCADE,
    INDEX idx_img_msg (message_id, seq),
    INDEX idx_img_id (image_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '消息图片引用';
