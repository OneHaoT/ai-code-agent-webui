-- 阶段2：对话绑定工作区（per-conversation workspace）
-- workspace_root 为绝对路径；留空表示使用 AI 模块默认工作区（ai/workspace_default/）。
-- 历史对话该列为 NULL（向后兼容），发消息时会按当前会话传入的 workspace 设置覆盖。

ALTER TABLE conversation
    ADD COLUMN workspace_root VARCHAR(1024) NULL COMMENT '该对话绑定的 Agent 工作区绝对路径（留空=AI 默认工作区）'
    AFTER title;
