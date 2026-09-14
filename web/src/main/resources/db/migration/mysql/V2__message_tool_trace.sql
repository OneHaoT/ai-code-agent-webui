-- 阶段1：消息追加只读工具调用轨迹列
-- 仅 assistant 消息可能携带；内容为 ConversationStore 手工序列化的 ToolStep 列表 JSON。
-- 旧行与普通问答该列为 NULL（Message 上的 @JsonInclude(NON_NULL) 保证 API 不输出空字段）。
-- 方言说明：MySQL 用 LONGTEXT（与 content/reasoning 一致，长轨迹不触 64KB 上限）；
--           PostgreSQL 对应类型为 text。
ALTER TABLE message
    ADD COLUMN tool_trace LONGTEXT NULL COMMENT '工具调用轨迹JSON（仅assistant；PG对应text）'
    AFTER reasoning;
