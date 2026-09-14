"""
MemoryManager 自测：用 FakeLLM 验证滑动窗口 + 增量摘要逻辑，
不调用真实大模型、不消耗 API。

运行（在 ai/ 目录下）:
    .venv\\Scripts\\python.exe tests\\test_memory.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from memory_manager import MemoryManager


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """记录每次 invoke 的入参，返回可预期的摘要文本。"""

    def __init__(self, response_text="压缩摘要"):
        self.response_text = response_text
        self.calls = []

    def invoke(self, messages):
        # 深拷贝入参记录，避免后续修改影响断言
        self.calls.append([(type(m).__name__, m.content) for m in messages])
        return FakeResponse(self.response_text)


class FailingLLM:
    def invoke(self, messages):
        raise RuntimeError("模拟摘要服务不可用")


def make_history(n):
    """生成 n 条严格 user/assistant 交替的历史，内容带序号便于断言。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"MSG-{i}-{role}"})
    return out


def roles(messages):
    return [
        "system" if isinstance(m, SystemMessage)
        else "user" if isinstance(m, HumanMessage)
        else "assistant"
        for m in messages
    ]


class TestMemoryManager(unittest.TestCase):

    def build(self, window=10, enabled=True, llm=None, max_sessions=500):
        self.fake = llm or FakeLLM()
        self.mgr = MemoryManager(
            llm_factory=lambda: self.fake,
            window_size=window,
            summary_enabled=enabled,
            max_sessions=max_sessions,
        )

    # 1. 历史未超窗口：不摘要、全量透传
    def test_short_history_passthrough(self):
        self.build(window=10)
        msgs = self.mgr.build_messages("c1", "SYS", make_history(6), "问")
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(roles(msgs),
                         ["system", "user", "assistant", "user", "assistant",
                          "user", "assistant", "user"])
        self.assertEqual(msgs[0].content, "SYS")
        self.assertEqual(msgs[-1].content, "问")

    # 2. 奇数条历史（11 条，窗口10）：cut 取偶后为 0，不拆散配对，11 条全保留
    def test_odd_boundary_keeps_pair(self):
        self.build(window=10)
        msgs = self.mgr.build_messages("c1", "SYS", make_history(11), "问")
        self.assertEqual(len(self.fake.calls), 0)
        # 1 system + 11 history + 1 current
        self.assertEqual(len(msgs), 13)
        self.assertIsInstance(msgs[1], HumanMessage)

    # 3. 超出窗口（12 条，窗口10）：最旧 2 条被摘要，窗口内 10 条原样
    def test_summary_triggered_beyond_window(self):
        self.build(window=10)
        history = make_history(12)
        msgs = self.mgr.build_messages("c1", "SYS", history, "问")

        self.assertEqual(len(self.fake.calls), 1)  # 触发一次摘要
        # system(主) + system(摘要) + 10 窗口 + 当前
        self.assertEqual(len(msgs), 13)
        self.assertIsInstance(msgs[1], SystemMessage)
        self.assertIn("更早对话的摘要", msgs[1].content)
        # 窗口保留的是 MSG-2 .. MSG-11
        self.assertEqual(msgs[2].content, "MSG-2-user")
        self.assertEqual(msgs[-2].content, "MSG-11-assistant")
        self.assertIsInstance(msgs[2], HumanMessage)  # 窗口以 user 开头
        self.assertIsInstance(msgs[-2], AIMessage)    # 以 assistant 结尾

        mem = self.mgr.get_memory("c1")
        self.assertEqual(mem.summarized_count, 2)

        # 摘要入参应包含最旧那一对
        transcript = self.fake.calls[0][1][1]
        self.assertIn("MSG-0-user", transcript)
        self.assertIn("MSG-1-assistant", transcript)
        self.assertNotIn("MSG-2-user", transcript)

    # 4. 增量摘要：12 -> 14 条时，只对新滑出的一对再调一次 LLM
    def test_incremental_summary(self):
        self.build(window=10)
        self.mgr.build_messages("c1", "SYS", make_history(12), "问1")
        self.assertEqual(len(self.fake.calls), 1)

        self.mgr.build_messages("c1", "SYS", make_history(14), "问2")
        self.assertEqual(len(self.fake.calls), 2)

        # 第二次摘要只包含新滑出窗口的 MSG-2 / MSG-3
        second_prompt = self.fake.calls[1][1][1]
        self.assertIn("MSG-2-user", second_prompt)
        self.assertIn("MSG-3-assistant", second_prompt)
        self.assertNotIn("MSG-0-user", second_prompt)
        self.assertNotIn("MSG-4-user", second_prompt)

        mem = self.mgr.get_memory("c1")
        self.assertEqual(mem.summarized_count, 4)

    # 5. 会话隔离：不同 conversation_id 的摘要互不影响
    def test_conversation_isolation(self):
        self.build(window=4)
        self.mgr.build_messages("a", "SYS", make_history(6), "问")
        self.mgr.build_messages("b", "SYS", make_history(6), "问")
        self.assertEqual(self.mgr.get_memory("a").summarized_count, 2)
        self.assertEqual(self.mgr.get_memory("b").summarized_count, 2)

        # b 增长到 8 条不影响 a
        self.mgr.build_messages("b", "SYS", make_history(8), "问")
        self.assertEqual(self.mgr.get_memory("a").summarized_count, 2)
        self.assertEqual(self.mgr.get_memory("b").summarized_count, 4)

    # 6. 关闭摘要：纯滑动窗口，旧消息直接丢弃，零 LLM 调用
    def test_summary_disabled(self):
        self.build(window=10, enabled=False)
        msgs = self.mgr.build_messages("c1", "SYS", make_history(14), "问")
        self.assertEqual(len(self.fake.calls), 0)
        # 1 system + 10 窗口 + 1 current，无摘要 system
        self.assertEqual(len(msgs), 12)
        self.assertNotIsInstance(msgs[1], SystemMessage)
        self.assertEqual(msgs[1].content, "MSG-4-user")

    # 7. 摘要 LLM 失败：不抛异常，降级为无摘要上下文，主对话不受影响
    def test_summary_failure_degrades(self):
        self.build(window=10, llm=FailingLLM())
        msgs = self.mgr.build_messages("c1", "SYS", make_history(12), "问")
        # 降级：system + 10 窗口 + current（无摘要消息）
        self.assertEqual(len(msgs), 12)
        self.assertNotIsInstance(msgs[1], SystemMessage)
        self.assertEqual(msgs[1].content, "MSG-2-user")
        # 失败后不推进 summarized_count，下次会重试
        self.assertEqual(self.mgr.get_memory("c1").summarized_count, 0)

    # 8. LRU 淘汰：超过 max_sessions 时最久未用的会话被驱逐
    def test_lru_eviction(self):
        self.build(window=4, max_sessions=2)
        self.mgr.build_messages("a", "SYS", make_history(6), "问")
        self.mgr.build_messages("b", "SYS", make_history(6), "问")
        self.mgr.build_messages("a", "SYS", make_history(6), "问")  # 触达 a
        self.mgr.build_messages("c", "SYS", make_history(6), "问")  # 淘汰 b
        self.assertIsNotNone(self.mgr.get_memory("a"))
        self.assertIsNone(self.mgr.get_memory("b"))
        self.assertIsNotNone(self.mgr.get_memory("c"))

    # 9. 空历史：只有系统提示 + 当前消息
    def test_empty_history(self):
        self.build(window=10)
        msgs = self.mgr.build_messages("c1", "SYS", [], "你好")
        self.assertEqual(len(msgs), 2)
        self.assertIsInstance(msgs[0], SystemMessage)
        self.assertIsInstance(msgs[1], HumanMessage)
        self.assertEqual(msgs[1].content, "你好")


PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


class TestImageSupport(unittest.TestCase):
    def build(self, **kw):
        # 兼容测试里简写的 window= 参数
        if "window" in kw:
            kw["window_size"] = kw.pop("window")
        self.fake = FakeLLM()
        self.mgr = MemoryManager(llm_factory=lambda: self.fake, **kw)

    # 10. 当前消息带图：content 为 blocks，含 image_url 块，文本在前
    def test_current_message_with_image(self):
        self.build(window_size=10)
        msgs = self.mgr.build_messages(
            "c1", "SYS", [], "这是什么图", [PNG_DATA_URL]
        )
        last = msgs[-1]
        self.assertIsInstance(last, HumanMessage)
        self.assertIsInstance(last.content, list)
        types = [b["type"] for b in last.content]
        self.assertEqual(types, ["text", "image_url"])
        self.assertEqual(last.content[1]["image_url"]["url"], PNG_DATA_URL)

    # 11. 纯图片提问（无文字）：自动补默认提示文本，不出现空 text
    def test_image_only_message_gets_fallback_text(self):
        self.build(window_size=10)
        msgs = self.mgr.build_messages("c1", "SYS", [], "", [PNG_DATA_URL])
        blocks = msgs[-1].content
        self.assertTrue(blocks[0]["text"])

    # 12. 窗口内历史图片原样保留；assistant 消息恒为字符串
    def test_history_images_in_window(self):
        self.build(window_size=10)
        history = [
            {"role": "user", "content": "看图", "images": [PNG_DATA_URL]},
            {"role": "assistant", "content": "这是一张图"},
        ]
        msgs = self.mgr.build_messages("c1", "SYS", history, "再问")
        user_msg = msgs[1]
        self.assertIsInstance(user_msg, HumanMessage)
        self.assertIsInstance(user_msg.content, list)
        self.assertEqual(user_msg.content[1]["image_url"]["url"], PNG_DATA_URL)
        self.assertIsInstance(msgs[2], AIMessage)
        self.assertEqual(msgs[2].content, "这是一张图")

    # 13. 旧消息被摘要折叠时：摘要文本含"图片"标记，且不把 data URL 送入摘要 LLM
    def test_summarized_image_marked_in_transcript(self):
        self.build(window=4)
        history = [
            {"role": "user", "content": "旧图", "images": [PNG_DATA_URL, PNG_DATA_URL]},
            {"role": "assistant", "content": "旧回答"},
        ] + make_history(4)  # 凑超窗口
        self.mgr.build_messages("c1", "SYS", history, "问")
        self.assertEqual(len(self.fake.calls), 1)
        transcript_parts = self.fake.calls[0]
        joined = " ".join(str(c) for _, c in transcript_parts)
        self.assertIn("附带 2 张图片", joined)
        self.assertNotIn("base64", joined)

    # 14. 后端窗口裁剪后的旧消息：images=[]（不传字节）但 image_count=2，
    #     摘要仍保留图片数量标记；窗口内消息不受影响
    def test_summarized_image_count_without_data_urls(self):
        self.build(window=4)
        history = [
            # 最旧一对：会被折叠，只带计数不带 data URL
            {"role": "user", "content": "旧图", "images": [], "image_count": 2},
            {"role": "assistant", "content": "旧回答"},
            # 窗口内：真实 data URL
            {"role": "user", "content": "窗口内图", "images": [PNG_DATA_URL],
             "image_count": 1},
            {"role": "assistant", "content": "新回答"},
        ] + make_history(2)  # 凑到 6 条，使最旧 2 条被折叠、中间一对落在窗口内
        msgs = self.mgr.build_messages("c1", "SYS", history, "问")
        self.assertEqual(len(self.fake.calls), 1)
        joined = " ".join(str(c) for _, c in self.fake.calls[0])
        self.assertIn("附带 2 张图片", joined)
        self.assertNotIn("base64", joined)
        # 窗口内的 user 消息仍是 content blocks，图片字节保留（system + 摘要 + 窗口首条）
        win_user = msgs[2]
        self.assertIsInstance(win_user.content, list)
        self.assertEqual(win_user.content[1]["image_url"]["url"], PNG_DATA_URL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
