"""
阶段1：LangGraph 只读工具调用循环。

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
   把它们转为统一事件流：token / reasoning / tool_call / tool_result /
   fatal_error（_ 前缀事件为内部事件，不映射 SSE，如 _final_state）；
3. 支持单轮并行多个 tool_calls（按 id 回填）；循环上限 max_iterations：
   达到上限的那一轮工具照执行并注入“直接作答”提示，给模型一次收敛机会；
   若提示轮仍请求工具，抛 AgentLimitError，由 main 转 error 帧。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.config import get_stream_writer

from tools import TOOL_SCHEMAS, Workspace, execute_tool

logger = logging.getLogger("ai-assist")

LIMIT_HINT = (
    "【系统提示】工具调用次数已达上限（最多 {limit} 轮）。"
    "不要再请求任何工具，请基于以上工具结果直接给出最终回答。"
)


class AgentLimitError(Exception):
    """工具循环超过上限且模型在收敛轮仍继续请求工具。"""


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


def compile_tool_graph(client: Any, workspace: Workspace,
                       max_iterations: int, base_kwargs: dict):
    """构造工具循环图（client 可注入，便于单测用 Fake 客户端驱动）。"""

    def call_model(state: AgentState) -> dict:
        writer = get_stream_writer()
        new_iter = state["iterations"] + 1
        # 收敛提示轮（max+1）之后仍进入本节点，说明模型拒不收敛
        if new_iter > max_iterations + 1:
            raise AgentLimitError(
                f"工具调用次数超过上限（{max_iterations} 轮），已终止")

        kwargs = {
            **base_kwargs,
            "messages": state["messages"],
            "stream": True,
            "tools": TOOL_SCHEMAS,
        }
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
                      max_iterations: int, tools_enabled: bool):
    """统一事件流生成器。

    事件：
      {"type":"token","delta"} / {"type":"reasoning","delta"}
      {"type":"tool_call","id","name","args"[,"raw_args"]}
      {"type":"tool_result","id","name","status","output","truncated"}
      {"type":"fatal_error","message"}
      {"type":"_final_state","messages"}   # 内部事件，不映射 SSE
    """
    if not tools_enabled:
        yield from _iter_plain_stream(client, messages, base_kwargs)
        return

    graph = compile_tool_graph(client, workspace, max_iterations, base_kwargs)
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
