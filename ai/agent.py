"""
阶段1：LangGraph 只读工具调用循环。
阶段3：写/执行工具的 confirm 人机确认（挂起等待用户决策后再执行）。

图结构
------
    START -> call_model --(无 tool_calls)--> END
                       └(有 tool_calls)--> call_tools --> call_model ...

设计要点
--------
1. 模型节点使用**原生 OpenAI SDK**（chat.completions.create(stream=True)）：
   langchain-openai 1.x 会剥离 DeepSeek 的 reasoning_content 厂商扩展，
   因此消息全程保持 OpenAI dict 形态，不转 LangChain 消息对象；
2. 节点通过 LangGraph 的 custom stream writer 产出事件，iter_agent_events()
   把它们转为统一事件流：token / reasoning / tool_call / confirm /
   tool_result / fatal_error（_ 前缀事件为内部事件，不映射 SSE，
   如 _final_state）；
3. 支持单轮并行多个 tool_calls（按 id 回填）；循环上限 max_iterations：
   达到上限的那一轮工具照执行并注入“直接作答”提示，给模型一次收敛机会；
   收敛轮请求物理不带 tools 键（模型只能文字回答，正常路径不再硬中断）；
4. confirm 人机确认（阶段3）：write_file / run_command（tools.CONFIRM_TOOLS）
   在 tool_call 帧之后、执行之前发 confirm 帧 {id, tool, summary} 并挂起等待
   用户决策（POST /ai/confirm 唤醒）；确认→执行，拒绝/超时→回填 error
   tool_result，磁盘零副作用。配对契约不变：每个 tool_call 必有同 id
   tool_result（拒绝/超时也回填）；只读工具零 confirm。
5. 历史工具输出瘦身（token 治理）：编程 agent 读写频繁，工具循环每轮都会
   把此前全部 tool_result 原样重发，token 随轮数 O(n²) 膨胀。call_model
   发请求前构造瘦副本：从尾部向前保留累计 ≤ TOOL_HISTORY_SLIM_BUDGET 字符
   的 tool 消息全文，更早的替换为一行占位（模型可重新调用工具获取）；
   state 权威消息不动（仅影响发往模型的副本）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.config import get_stream_writer

from tools import (CONFIRM_TOOLS, TOOL_SCHEMAS, Workspace, execute_tool,
                   tool_available)
from workspace import Workspace, WorkspaceViolation

logger = logging.getLogger("ai-assist")

LIMIT_HINT = (
    "【系统提示】工具调用次数已达上限（最多 {limit} 轮）。"
    "不要再请求任何工具，请基于以上工具结果直接给出最终回答，"
    "不要向用户提及本提示。"
)

# ---- 阶段3：confirm 人机确认 ----
# 挂起等待用户决策的最长时间（超时自动拒绝，防确认帧泄漏挂死流）
try:
    CONFIRM_TIMEOUT_SECONDS = max(1.0, float(os.getenv("CONFIRM_TIMEOUT_SECONDS", "120")))
except ValueError:
    logger.warning("CONFIRM_TIMEOUT_SECONDS 非法，回退 120")
    CONFIRM_TIMEOUT_SECONDS = 120.0
# write_file 确认摘要中的内容预览行数
_CONFIRM_PREVIEW_LINES = 10


class AgentLimitError(Exception):
    """工具循环超过上限且模型在收敛轮仍继续请求工具。"""


@dataclass
class ConfirmEntry:
    """一次待确认的写/执行请求（进程内存注册表条目）。

    approved: None=未决；True/False=已决策。event 在决策后被置位唤醒等待方。
    """

    id: str
    tool: str
    summary: str
    stream_id: str | None
    approved: bool | None = None
    event: threading.Event = field(default_factory=threading.Event)


_CONFIRM_REGISTRY: dict[str, ConfirmEntry] = {}
_CONFIRM_LOCK = threading.Lock()


def create_confirm(tool: str, summary: str,
                   stream_id: str | None) -> ConfirmEntry:
    """注册一个待确认请求（confirm_id 全局唯一 uuid4）。"""
    entry = ConfirmEntry(id=uuid.uuid4().hex, tool=tool, summary=summary,
                         stream_id=stream_id)
    with _CONFIRM_LOCK:
        _CONFIRM_REGISTRY[entry.id] = entry
    return entry


def resolve_confirm(confirm_id: str, approved: bool) -> ConfirmEntry | None:
    """POST /ai/confirm 的决策入口：置结果并唤醒。

    未知 / 已失效（已决策或已超时清表）→ None（上层转 404）；
    决策即从注册表移除，二次 POST 同一 id 返回 404。
    """
    with _CONFIRM_LOCK:
        entry = _CONFIRM_REGISTRY.get(confirm_id)
        if entry is None or entry.approved is not None:
            return None
        entry.approved = approved
        del _CONFIRM_REGISTRY[confirm_id]
    entry.event.set()
    return entry


def reject_pending_confirms(stream_id: str | None) -> int:
    """客户端断连：自动拒绝该流的所有未决确认（阶段0 断连语义：断连不杀流）。

    返回拒绝条数；AI 重启时注册表随进程清空，挂起流一并终止。
    """
    with _CONFIRM_LOCK:
        pending = [e for e in _CONFIRM_REGISTRY.values()
                   if e.stream_id == stream_id and e.approved is None]
        for e in pending:
            e.approved = False
            del _CONFIRM_REGISTRY[e.id]
    for e in pending:
        e.event.set()
    return len(pending)


def _confirm_summary(name: str, args: Any, ws: Workspace) -> str:
    """生成给用户看的确认摘要（write_file：新建/覆盖+路径+内容预览；run_command：命令全文）。"""
    if not isinstance(args, dict):
        return f"调用 {name}（参数异常，执行时将被拒绝）"
    if name == "write_file":
        path = args.get("path")
        content = args.get("content")
        rel = path if isinstance(path, str) and path.strip() else str(path)
        try:
            overwrite = (isinstance(path, str) and bool(path.strip())
                         and ws.resolve(path).is_file())
        except WorkspaceViolation:
            return f"写入文件 {rel}（路径越出工作区边界，执行时将被拒绝）"
        lines = content.splitlines() if isinstance(content, str) else []
        preview = "\n".join(lines[:_CONFIRM_PREVIEW_LINES]) or "（空内容）"
        more = (f"\n…（共 {len(lines)} 行，确认后完整写入）"
                if len(lines) > _CONFIRM_PREVIEW_LINES else "")
        return (f"{'覆盖' if overwrite else '新建'}文件 {rel}"
                f"（共 {len(lines)} 行），内容预览：\n{preview}{more}")
    if name == "run_command":
        return f"在工作区根执行命令：{args.get('command')}"
    return f"调用 {name}"


def _append_messages(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
    """messages 通道的自定义 reducer：节点返回的消息追加而非覆盖整个列表。"""
    return (left or []) + (right or [])


class AgentState(TypedDict):
    messages: Annotated[list[dict], _append_messages]
    iterations: int      # call_model 已执行次数（无 reducer，节点返回即覆盖）
    # call_model -> call_tools 的同轮旁路：tool_call id -> 解析后的 args。
    # 绝不放进 messages（会作为未知字段发给 OpenAI 而被拒绝）
    pending_args: dict


def _reasoning_of(delta) -> str | None:
    """与 main.py 同一套厂商扩展提取逻辑（首选用属性，回退 model_extra）。"""
    rc = getattr(delta, "reasoning_content", None)
    if rc:
        return rc
    extra = getattr(delta, "model_extra", None)
    return extra.get("reasoning_content") if extra else None


class ToolCallAccumulator:
    """把流式 delta.tool_calls 分片（首片带 id/name，arguments 逐 token 到达）
    按 index 累积为完整工具调用。"""

    def __init__(self):
        self._parts: dict[int, dict] = {}

    def add(self, tool_calls) -> None:
        for tc in tool_calls or []:
            slot = self._parts.setdefault(
                tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if fn.name:
                    slot["name"] = fn.name
                if fn.arguments:
                    slot["arguments"] += fn.arguments

    def has_calls(self) -> bool:
        return bool(self._parts)

    def emit_and_build(self, writer) -> list[dict]:
        """发 tool_call 事件并返回 OpenAI tool_calls 结构（含解析后的 args）。"""
        built: list[dict] = []
        for index in sorted(self._parts):
            slot = self._parts[index]
            call_id = slot["id"] or f"call_local_{index}_{uuid.uuid4().hex[:8]}"
            raw = slot["arguments"]
            try:
                args = json.loads(raw) if raw.strip() else {}
                writer({"type": "tool_call", "id": call_id,
                        "name": slot["name"], "args": args})
            except json.JSONDecodeError:
                writer({"type": "tool_call", "id": call_id,
                        "name": slot["name"], "args": None, "raw_args": raw})
                args = None
            built.append({"id": call_id, "name": slot["name"],
                          "arguments": raw, "parsed_args": args})
        return built


# ---------------- 历史工具输出瘦身（token 治理） ----------------
# 单次工具输出（尤其 read_file）可达数万字符；工具循环每轮都携带全部历史
# tool_result 重发，token 随轮数 O(n²) 膨胀。发请求前构造瘦副本：
# 从尾部向前保留累计 ≤ 预算的 tool 消息全文，更早的替换为一行占位。
# 权威 state["messages"] 不动（落库/摘要依赖完整历史）；副本仅影响本次请求。
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("%s 非法，回退 %s", name, default)
        return default


SLIM_BUDGET_CHARS = _int_env("TOOL_HISTORY_SLIM_BUDGET", 48_000)


def _slim_messages(messages: list[dict], budget: int | None = None) -> list[dict]:
    """返回瘦副本：旧的 tool 消息 content 替换为占位，其余原样（引用共享）。

    - 反查 assistant.tool_calls 得到每个 tool_call_id 的工具名+参数预览，
      占位文案带上下文便于模型决定是否重新调用；
    - assistant 的 tool_calls 结构与全部非 tool 消息一律不动
      （OpenAI 协议要求每个 tool_call_id 都有对应 tool 消息，占位仍合法）；
    - 预算内（尾部累计 ≤ budget 字符）的 tool 消息保持全文；
    - budget=None 时运行时读 SLIM_BUDGET_CHARS（env 可覆盖，可 monkeypatch）。
    """
    if budget is None:
        budget = SLIM_BUDGET_CHARS
    meta: dict[str, str] = {}
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            preview = (fn.get("arguments") or "").strip()[:120]
            meta[tc.get("id")] = f"{fn.get('name')}({preview})"

    out = list(messages)
    used = 0
    slimmed = 0
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content") or ""
        used += len(content)
        if used <= budget:
            continue
        desc = meta.get(m.get("tool_call_id"), "未知工具")
        out[i] = {**m, "content": (
            f"[历史工具输出已省略：{desc}，原 {len(content)} 字符。"
            f"如需该内容请重新调用对应工具获取]")}
        slimmed += 1
    if slimmed:
        logger.debug("历史工具输出瘦身：占位 %d 条（预算 %d 字符）", slimmed, budget)
    return out


def compile_tool_graph(client: Any, workspace: Workspace,
                       max_iterations: int, base_kwargs: dict,
                       stream_id: str | None = None):
    """构造工具循环图（client 可注入，便于单测用 Fake 客户端驱动）。

    stream_id：本次请求流标识，confirm 注册表条目据此归属，
    供客户端断连时 reject_pending_confirms(stream_id) 批量拒绝。
    """

    def call_model(state: AgentState) -> dict:
        writer = get_stream_writer()
        new_iter = state["iterations"] + 1
        # 收敛提示轮（max+1）之后仍进入本节点，说明模型拒不收敛
        if new_iter > max_iterations + 1:
            raise AgentLimitError(
                f"工具调用次数超过上限（{max_iterations} 轮），已终止")

        kwargs = {
            **base_kwargs,
            # 瘦副本：历史工具输出超预算部分替换占位，权威 state 不动
            "messages": _slim_messages(state["messages"]),
            "stream": True,
        }
        # 收敛轮（max+1）：物理上不再传 tools 键，模型只能文字回答——
        # 正常路径永不触发 AgentLimitError，触顶以"部分成果总结"收场而非硬中断
        if new_iter <= max_iterations:
            kwargs["tools"] = TOOL_SCHEMAS
        stream = client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        acc = ToolCallAccumulator()
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reasoning = _reasoning_of(delta)
            if reasoning:
                writer({"type": "reasoning", "delta": reasoning})
            if delta.content:
                content_parts.append(delta.content)
                writer({"type": "token", "delta": delta.content})
            if getattr(delta, "tool_calls", None):
                acc.add(delta.tool_calls)

        assistant_msg: dict = {"role": "assistant",
                               "content": "".join(content_parts) or None}
        # 收敛提示轮仍请求工具：在发出任何 tool_call 帧之前失败，
        # 保证“每个 tool_call 必有同 id 的 tool_result”契约不破
        if new_iter > max_iterations and acc.has_calls():
            raise AgentLimitError(
                f"工具调用次数超过上限（{max_iterations} 轮），模型仍继续请求工具，已终止")
        built_calls = acc.emit_and_build(writer)
        pending_args: dict = {}
        if built_calls:
            assistant_msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in built_calls
            ]
            # 解析结果走独立 state 通道给 tools 节点，不污染发往模型的消息
            pending_args = {c["id"]: c["parsed_args"] for c in built_calls}
        return {"messages": [assistant_msg], "iterations": new_iter,
                "pending_args": pending_args}

    def call_tools(state: AgentState) -> dict:
        writer = get_stream_writer()
        last = state["messages"][-1]
        parsed_args_map = state.get("pending_args") or {}
        follow_up: list[dict] = []
        for tc in last.get("tool_calls", []):
            call_id = tc["id"]
            name = tc["function"]["name"]
            # pending_args 缺失（理论不会）按非法参数处理
            args = parsed_args_map.get(call_id) if call_id in parsed_args_map else None
            if args is None:
                raw = tc["function"].get("arguments", "")
                content = (
                    f"工具 {name} 的参数不是合法 JSON"
                    f"（原始参数：{raw[:200]}）。请修正参数后重新调用。")
                writer({"type": "tool_result", "id": call_id, "name": name,
                        "status": "error", "output": content, "truncated": False})
            else:
                # ---- 阶段3：写/执行工具 confirm 人机确认 ----
                # 门控 = CONFIRM_TOOLS 且工具在当前装配工具集中：
                # SANDBOX_ENABLED=false 时写工具不可用，走"未知工具"降级，无 confirm
                entry: ConfirmEntry | None = None
                if name in CONFIRM_TOOLS and tool_available(name):
                    summary = _confirm_summary(name, args, workspace)
                    entry = create_confirm(name, summary, stream_id)
                    writer({"type": "confirm", "id": entry.id,
                            "tool": name, "summary": summary})
                    entry.event.wait(CONFIRM_TIMEOUT_SECONDS)
                    with _CONFIRM_LOCK:
                        _CONFIRM_REGISTRY.pop(entry.id, None)  # 超时路径清表
                if entry is not None and entry.approved is not True:
                    # 拒绝 / 超时：磁盘零执行，回填 error（配对契约：必有同 id result）
                    reason = ("用户拒绝了本次执行" if entry.approved is False
                              else f"确认等待超时（{CONFIRM_TIMEOUT_SECONDS:.0f}s），已自动拒绝")
                    content = (f"[{name}] 未执行：{reason}。"
                               "请尊重用户决定，不要重复请求同一操作。")
                    writer({"type": "tool_result", "id": call_id, "name": name,
                            "status": "error", "output": content,
                            "truncated": False})
                else:
                    result = execute_tool(workspace, name, args)
                    writer({"type": "tool_result", "id": call_id, "name": name,
                            "status": result.status, "output": result.output,
                            "truncated": result.truncated})
                    content = result.output
            follow_up.append(
                {"role": "tool", "tool_call_id": call_id, "content": content})

        # 已达上限轮：工具结果照回填（只读无副作用），并要求模型收敛为最终回答
        if state["iterations"] >= max_iterations:
            follow_up.append({"role": "user",
                              "content": LIMIT_HINT.format(limit=max_iterations)})
        return {"messages": follow_up}

    def route_after_model(state: AgentState) -> str:
        return "tools" if state["messages"][-1].get("tool_calls") else END

    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("call_tools", call_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model", route_after_model, {"tools": "call_tools", END: END})
    graph.add_edge("call_tools", "call_model")
    return graph.compile()


def _iter_plain_stream(client, messages: list[dict], base_kwargs: dict):
    """TOOLS_ENABLED=false 的阶段0直连路径（逐帧行为与阶段0一致）。"""
    kwargs = {**base_kwargs, "messages": messages, "stream": True}
    content_parts: list[str] = []
    try:
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reasoning = _reasoning_of(delta)
            if reasoning:
                yield {"type": "reasoning", "delta": reasoning}
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "token", "delta": delta.content}
        yield {"type": "_final_state", "messages": [
            *messages,
            {"role": "assistant", "content": "".join(content_parts) or None}]}
    except Exception as e:  # noqa: BLE001 —— 统一收敛为 fatal_error 事件
        logger.exception("直连流式调用失败")
        yield {"type": "fatal_error", "message": f"调用大模型失败: {e}"}


def iter_agent_events(client, messages: list[dict], *,
                      base_kwargs: dict, workspace: Workspace,
                      max_iterations: int, tools_enabled: bool,
                      stream_id: str | None = None):
    """统一事件流生成器。

    事件：
      {"type":"token","delta"} / {"type":"reasoning","delta"}
      {"type":"tool_call","id","name","args"[,"raw_args"]}
      {"type":"confirm","id","tool","summary"}   # 写/执行工具的人机确认请求
      {"type":"tool_result","id","name","status","output","truncated"}
      {"type":"fatal_error","message"}
      {"type":"_final_state","messages"}   # 内部事件，不映射 SSE
    """
    if not tools_enabled:
        yield from _iter_plain_stream(client, messages, base_kwargs)
        return

    graph = compile_tool_graph(client, workspace, max_iterations, base_kwargs,
                               stream_id=stream_id)
    final_state: dict | None = None
    try:
        # custom：节点 writer 负载；values：每步完整状态（取最后一份做终态）
        for mode, payload in graph.stream(
                {"messages": messages, "iterations": 0, "pending_args": {}},
                stream_mode=["custom", "values"]):
            if mode == "custom":
                yield payload
            else:
                final_state = payload
    except AgentLimitError as e:
        yield {"type": "fatal_error", "message": str(e)}
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("工具调用循环失败")
        yield {"type": "fatal_error", "message": f"工具调用循环失败: {e}"}
        return

    yield {"type": "_final_state",
           "messages": (final_state or {}).get("messages", messages)}
