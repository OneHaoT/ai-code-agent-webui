"""
对话记忆管理：滑动窗口 + 增量 LLM 摘要压缩。

策略
----
后端每次请求仍带来该会话的全量历史（后端是消息的唯一事实来源），
本模块在送给大模型之前做上下文压缩：

  [SystemMessage(系统提示)]
  [SystemMessage(历史对话摘要)]   <- 窗口之外的旧消息，经 LLM 增量摘要
  [最近 window_size 条原始消息]    <- 滑动窗口，保持 user/assistant 配对完整
  [当前用户消息]

要点
----
1. 窗口按"消息条数"裁剪，裁剪边界强制为偶数，保证窗口内以 user 开头、
   以 assistant 结尾，不拆散问答对；
2. 摘要是"增量"维护的：只有新滑出窗口的消息才会触发一次摘要 LLM 调用，
   摘要结果按 conversation_id 缓存在内存中（AI 模块本身无状态、可随时重启，
   重启后下一轮会基于后端传来的全量历史自动重建摘要）；
3. 摘要生成失败不阻断主对话，降级为不带摘要的窗口上下文；
4. 对话接口为 SSE 流式（/ai/chat/stream），本模块只负责消息压缩，与传输方式无关。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    BaseMessage,
)

# 摘要专用提示词（独立于主系统提示，便于替换）
SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。请把对话进展压缩成一份中文摘要，供后续对话引用。"
    "要求：保留用户关键信息（姓名/技术栈/目标/环境）、已确认的决策与结论、"
    "尚未解决的问题；忽略寒暄与重复内容；使用分点，总长不超过 300 字。"
)

SUMMARY_USER_TEMPLATE = """请更新对话摘要。

【已有摘要】
{previous}

【需要并入摘要的新增对话】
{transcript}

请直接输出更新后的完整摘要（不要输出"摘要："之类的前缀）。
若已有摘要为空，则基于新增对话直接生成摘要。"""


@dataclass
class ConversationMemory:
    """单个会话的压缩状态。"""

    conversation_id: str
    # 已被折叠进 summary 的历史消息条数
    summarized_count: int = 0
    summary: str = ""

    def reset(self) -> None:
        self.summarized_count = 0
        self.summary = ""


class MemoryManager:
    """
    按 conversation_id 管理压缩记忆。

    Parameters
    ----------
    llm_factory : () -> BaseChatModel
        惰性构造 LLM 的工厂（摘要调用复用同一个模型客户端）。
    window_size : int
        滑窗保留的最近消息条数（内部会取偶数，保证问答配对完整）。
    summary_enabled : bool
        False 时退化为纯滑动窗口（旧消息直接丢弃，不调用摘要）。
    max_sessions : int
        内存中最多缓存的会话数（LRU 淘汰），防止长期运行内存无限增长。
    """

    def __init__(
        self,
        llm_factory,
        window_size: int = 10,
        summary_enabled: bool = True,
        max_sessions: int = 500,
    ):
        self._llm_factory = llm_factory
        # 强制偶数，避免拆散 user/assistant 配对
        self.window_size = max(2, int(window_size) - (int(window_size) % 2))
        self.summary_enabled = summary_enabled
        self.max_sessions = max_sessions
        self._sessions: "OrderedDict[str, ConversationMemory]" = OrderedDict()
        # 摘要更新涉及 LLM 调用，按会话加锁避免并发请求重复摘要
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    # ---------- 对外主入口 ----------

    def build_messages(
        self,
        conversation_id: str,
        system_prompt: str,
        history: list,
        current_message: str,
        current_images: list[str] | None = None,
    ) -> list[BaseMessage]:
        """
        根据全量 history + 当前消息，构造压缩后的 LangChain 消息序列。

        history: [{"role": "user"|"assistant", "content": str,
                   "images": [data_url, ...]?,
                   "image_count": int?}, ...]
            image_count：消息实际图片数。窗口外旧消息不传字节（images=[]），
            摘要时仍靠该计数保留"曾附带 N 张图片"的事实。
        current_images: 当前消息附带的图片（data URL 列表，可空）。
            图片只能出现在 user 消息中；仅保留在滑动窗口内，
            被摘要折叠的旧图片不参与摘要（文本中以标记提示曾发过图片）。
        """
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]

        total = len(history)
        if total == 0:
            messages.append(self._human_message(current_message, current_images))
            return messages

        # 计算窗口裁剪点：保留最近 window_size 条，边界向下取偶
        cut = max(0, total - self.window_size)
        cut -= cut % 2

        # 更新/获取摘要（只覆盖 cut 之前的消息）
        summary = ""
        if cut > 0:
            if self.summary_enabled:
                summary = self._update_summary(conversation_id, history, cut)
            else:
                # 纯窗口模式：直接丢弃旧消息
                self._touch(conversation_id)
        else:
            # 历史未超出窗口，清理可能残留的状态（理论上不会发生，防御性）
            mem = self._get(conversation_id)
            if mem.summarized_count > 0:
                mem.reset()

        if summary:
            messages.append(
                SystemMessage(content=f"【更早对话的摘要】\n{summary}")
            )

        for m in history[cut:]:
            messages.append(self._to_langchain_message(m))
        messages.append(self._human_message(current_message, current_images))
        return messages

    def get_memory(self, conversation_id: str) -> ConversationMemory | None:
        """供调试/观测使用。"""
        with self._global_lock:
            return self._sessions.get(conversation_id)

    def drop(self, conversation_id: str) -> None:
        """会话被删除时调用，清理缓存。"""
        with self._global_lock:
            self._sessions.pop(conversation_id, None)
            self._locks.pop(conversation_id, None)

    # ---------- 内部实现 ----------

    def _update_summary(self, conversation_id: str, history: list, cut: int) -> str:
        mem = self._get(conversation_id)
        lock = self._get_lock(conversation_id)
        with lock:
            # 任何访问都刷新 LRU 顺序
            self._touch(conversation_id)
            # 双重检查，避免排队期间其他线程已更新
            pending = history[mem.summarized_count:cut]
            if not pending:
                return mem.summary

            transcript = self._format_transcript(pending)
            llm = self._llm_factory()
            try:
                resp = llm.invoke([
                    SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
                    HumanMessage(content=SUMMARY_USER_TEMPLATE.format(
                        previous=mem.summary or "（无）",
                        transcript=transcript,
                    )),
                ])
                new_summary = resp.content if isinstance(resp.content, str) else str(resp.content)
                new_summary = new_summary.strip()
                if new_summary:
                    mem.summary = new_summary
                    mem.summarized_count = cut
                # 摘要为空视为失败，保留旧摘要
            except Exception:
                # 摘要失败不阻断主对话：保留已有摘要/降级无摘要
                pass
            return mem.summary

    @staticmethod
    def _format_transcript(messages: list) -> str:
        lines = []
        for m in messages:
            role = "用户" if m.get("role") == "user" else "助手"
            text = m.get("content", "") or ""
            # 优先用 image_count（窗口外旧消息不传字节只传计数），
            # 回退到 images 长度（兼容旧调用方/测试）
            count = m.get("image_count") or len(m.get("images") or [])
            if role == "用户" and count:
                # 被摘要折叠的图片不送入摘要 LLM，仅保留"曾发过图片"的事实
                text = (text + f"（附带 {count} 张图片）").strip()
            lines.append(f"{role}：{text}")
        return "\n".join(lines)

    @staticmethod
    def _to_langchain_message(m: dict) -> BaseMessage:
        # DeepSeek 规定图片只能出现在 user 消息；assistant 恒为纯文本
        if m.get("role") == "assistant":
            return AIMessage(content=m.get("content", "") or "")
        return MemoryManager._human_message(
            m.get("content", ""), m.get("images")
        )

    @staticmethod
    def _human_message(text: str, images: list[str] | None) -> HumanMessage:
        """
        构造用户消息：无图时 content 为纯字符串（最省 token）；
        有图时使用 OpenAI 兼容的 content blocks 数组。
        纯图片提问（无文字）补一个默认提示，避免空 text 块。
        """
        images = images or []
        if not images:
            return HumanMessage(content=text or "")

        parts: list = [
            {"type": "text", "text": text.strip() or "请分析这张（些）图片的内容并回答。"}
        ]
        for data_url in images:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        return HumanMessage(content=parts)

    # ---------- 会话缓存（LRU） ----------

    def _get(self, conversation_id: str) -> ConversationMemory:
        with self._global_lock:
            mem = self._sessions.get(conversation_id)
            if mem is None:
                mem = ConversationMemory(conversation_id=conversation_id)
                self._sessions[conversation_id] = mem
            return mem

    def _touch(self, conversation_id: str) -> None:
        with self._global_lock:
            if conversation_id in self._sessions:
                self._sessions.move_to_end(conversation_id)
            while len(self._sessions) > self.max_sessions:
                old_id, _ = self._sessions.popitem(last=False)
                self._locks.pop(old_id, None)

    def _get_lock(self, conversation_id: str) -> threading.Lock:
        with self._global_lock:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[conversation_id] = lock
            return lock
