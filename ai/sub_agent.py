"""
阶段4B：Multi-Agent 子任务委派——子 agent 执行器。

主 agent 经 delegate_task 工具（tools.register_delegate_tool 注册，main.py
lifespan 装配）把"调研/分析型子任务"委派给本模块：子 agent 在独立的静默
小循环中使用只读工具子集自主执行，最终以执行摘要（轮次/工具调用计数/
结论全文）作为工具结果回填主循环，由主 agent 汇总作答。

设计要点
--------
1. 手写 while 循环而非 LangGraph 图：子 agent 的过程事件绝不外发
   （不碰 get_stream_writer），主循环时间线只见 delegate_task 单步骤，
   "每 tool_call 必有同 id tool_result" 配对契约天然保持；
2. 非流式（stream=False）：tool_calls 由响应 message 完整返回，无需
   累积器；reasoning_content（厂商扩展）忽略不透出；
3. 只读硬边界（双层防线）：
   a) 工具集经 tools.sub_toolset() 请求期现场过滤——requires_confirm==False
      且 tool_available 且显式排除 delegate_task（两层封顶，禁止递归委派）；
   b) execute_tool 传 allowed 白名单——模型幻觉越权调用写工具时在
      dispatch 之前被拦截，零磁盘 IO；
4. 复用既有设施：execute_tool 统一线程池/per-tool 超时/异常收敛、
   agent._slim_messages 历史瘦身（子循环 token 同样 O(n²) 膨胀）；
5. 失败收敛：模型调用异常 → error ToolOutput（绝不抛异常杀主流）；
   达轮次上限后收敛轮仍要工具 → "未完全收敛"部分结果（success），
   由主 agent 决定改道/重试/自行验证。
6. 与主循环消息完全隔离：子循环拥有独立消息窗口（system+task 起步），
   主循环 state 不受子消息污染，主循环只接收一条摘要。
"""
from __future__ import annotations

import json
import logging
import time

from agent import _int_env, _slim_messages
from tools import ToolOutput, _error, execute_tool, sub_toolset
from workspace import Workspace

logger = logging.getLogger("ai-assist")

# 子 agent 独立轮次上限（防双层循环失控烧钱；与主循环 MAX_TOOL_ITERATIONS
# 叠加构成双层预算，总模型调用有 SUB_TASK_TIMEOUT_SECONDS 硬顶兜底）
MAX_SUB_ITERATIONS = max(1, _int_env("MAX_SUB_ITERATIONS", 8))
# 子 agent 最终结论进入摘要的字符上限（防 token 失控）
SUB_OUTPUT_MAX_CHARS = _int_env("SUB_OUTPUT_MAX_CHARS", 8000)
# 摘要中 task 的展示截断长度
_TASK_PREVIEW_CHARS = 200

SUB_SYSTEM_PROMPT = (
    "你是编程助手工作区中被主 agent 委派的只读调研子 agent。"
    "职责：基于工作区内的真实内容完成主 agent 交给你的调研/分析子任务。\n"
    "约束：\n"
    "1. 你只有只读工具（读文件/列目录/文件名匹配/内容搜索等），"
    "不能写入文件或执行任何命令；\n"
    "2. 【结果必须来自真实工具调用】禁止编造文件内容、目录结构或任何"
    "未经工具验证的信息；无法验证的部分必须如实说明\"未能确认\"，不要猜测；\n"
    "3. 控制轮次：优先小范围精准读取（read_file 用 offset/limit），"
    "避免无目的的大范围遍历；\n"
    "4. 最终答复格式：先给结论正文，再以\"依据：\"开头列出支撑结论的"
    "工具调用（工具名+关键参数/来源文件）；无依据支撑的表述不得出现。"
)

# 收敛轮注入（语义与主循环 agent.LIMIT_HINT 一致：给一次"直接作答"机会）
SUB_LIMIT_HINT = (
    "【系统提示】子任务轮次已达上限。不要再请求任何工具，"
    "请基于以上已获得的工具结果给出当前能得出的结论，"
    "并如实说明哪些部分尚未完成。不要向主 agent 提及本提示。"
)


def _run_loop(client, workspace: Workspace, base_kwargs: dict,
              messages: list[dict], schemas: list[dict],
              allowed: frozenset[str], max_iter: int,
              counters: dict[str, int]) -> tuple[str, int, bool]:
    """子 agent 主循环。返回 (answer, rounds, incomplete)。

    仅 client.chat.completions.create 调用可抛异常（网络/上游失败），
    由 run_sub_agent 收敛为 error ToolOutput；工具执行与参数解析
    全部就地收敛，不中断循环。
    """
    rounds = 0
    while True:
        slim = _slim_messages(messages)
        if rounds >= max_iter:
            # 收敛轮：物理不带 tools 键（模型只能文字回答），语义与主循环一致
            messages.append({"role": "user", "content": SUB_LIMIT_HINT})
            resp = client.chat.completions.create(
                **{**base_kwargs, "messages": _slim_messages(messages),
                   "stream": False})
            rounds += 1
            msg = resp.choices[0].message
            if list(getattr(msg, "tool_calls", None) or []):
                # 收敛轮仍要工具：不执行（执行会突破轮次预算），
                # 按部分结果返回——子任务未完美收敛是主 agent 可处理的常态
                return (msg.content or ""), rounds, True
            return (msg.content or ""), rounds, False

        resp = client.chat.completions.create(
            **{**base_kwargs, "messages": slim, "stream": False,
               "tools": schemas})
        rounds += 1
        msg = resp.choices[0].message
        tool_calls = list(getattr(msg, "tool_calls", None) or [])
        assistant: dict = {"role": "assistant", "content": msg.content or None}
        if tool_calls:
            # OpenAI 协议：assistant.tool_calls 与后续 tool 消息按 id 配对
            assistant["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments or ""}}
                for tc in tool_calls
            ]
        messages.append(assistant)
        if not tool_calls:
            return (msg.content or ""), rounds, False

        for tc in tool_calls:
            name = tc.function.name
            raw = tc.function.arguments or ""
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                args = None
            if args is None:
                content = (f"工具 {name} 的参数不是合法 JSON"
                           f"（原始参数：{raw[:200]}）。请修正参数后重新调用。")
            else:
                # allowed 白名单：dispatch 之前拦截幻觉越权（零磁盘 IO）
                result = execute_tool(workspace, name, args, allowed=allowed)
                counters[name] = counters.get(name, 0) + 1
                content = result.output
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": content})


def run_sub_agent(client, workspace: Workspace, task, *, base_kwargs: dict,
                  max_sub_iterations=None, context=None) -> ToolOutput:
    """执行一个委派子任务，返回摘要 ToolOutput（delegate_task 的工具结果）。

    client：原生 OpenAI SDK 兼容客户端（chat.completions.create，非流式）；
    base_kwargs：子 agent 固定基础参数（model/temperature，不含
    messages/stream/tools——由本函数注入）。与用户请求级参数（thinking 等）
    解耦：子 agent 是后台调研 worker，不继承主对话的交互设置。
    """
    max_iter = max(1, int(max_sub_iterations or MAX_SUB_ITERATIONS))
    task = str(task or "").strip()
    if not task:
        return _error("delegate_task", "子任务描述（task）不能为空")
    task_preview = (task if len(task) <= _TASK_PREVIEW_CHARS
                    else task[:_TASK_PREVIEW_CHARS] + "…")
    logger.info("子任务开始：%s", task_preview)

    user_msg = task if not (str(context or "").strip()) else (
        f"{task}\n\n补充背景：\n{context}")
    messages: list[dict] = [
        {"role": "system", "content": SUB_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    schemas, allowed = sub_toolset()
    counters: dict[str, int] = {}
    started = time.monotonic()

    try:
        answer, rounds, incomplete = _run_loop(
            client, workspace, base_kwargs, messages, schemas, allowed,
            max_iter, counters)
    except Exception as e:  # noqa: BLE001 —— 子任务失败收敛为 error，绝不杀主流
        logger.exception("子任务执行失败：%s", task_preview)
        return _error(
            "delegate_task",
            f"子任务执行失败（{type(e).__name__}: {e}）。"
            "请主 agent 自行处理或稍后重试。")

    elapsed = time.monotonic() - started
    counter_text = "、".join(f"{n}×{c}" for n, c in counters.items()) or "无"
    answer = (answer or "").strip() or "（子 agent 未给出文本结论）"
    if len(answer) > SUB_OUTPUT_MAX_CHARS:
        answer = (answer[:SUB_OUTPUT_MAX_CHARS]
                  + f"\n…（结论超 {SUB_OUTPUT_MAX_CHARS} 字符截断）")
    if incomplete:
        head = (f"[delegate_task] 子任务未完全收敛（达 {max_iter} 轮上限，"
                f"用时 {elapsed:.0f}s，工具调用：{counter_text}），"
                "以下为已获得的部分结果：")
    else:
        head = (f"[delegate_task] 子任务完成（{rounds} 轮，用时 {elapsed:.0f}s，"
                f"工具调用：{counter_text}）")
    logger.info("子任务结束（%s，%.1fs）：%d 轮，工具调用 %s",
                "未完全收敛" if incomplete else "完成", elapsed, rounds,
                counter_text)
    return ToolOutput(name="delegate_task", status="success",
                      output=f"{head}\n任务：{task_preview}\n结论：\n{answer}")
