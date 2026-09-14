package com.aiassist.model;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.databind.JsonNode;

/**
 * 一次工具调用的轨迹步骤（阶段1，仅 assistant 消息可能携带）。
 *
 * tool_call 帧到达时为 running 态（output / durationMs 为 null），
 * 同 id 的 tool_result 到达后以 {@link #finished} 补全为不可变快照。
 *
 * @param id         工具调用 id（与 SSE 帧的 tool_call/tool_result 配对键）
 * @param name       工具名（read_file/list_dir/glob/grep）
 * @param args       解析后的参数 JSON；参数非法时为 null（配合 rawArgs）
 * @param rawArgs    非法 JSON 时的原始参数串，正常调用为 null
 * @param status     running | success | error
 * @param output     工具输出文本（result 到达前为 null）
 * @param truncated  输出是否被截断（result 到达前为 null）
 * @param seq        本轮对话内的调用序号（从 1 开始，按 tool_call 到达顺序）
 * @param durationMs 调用耗时毫秒（result 到达前为 null）
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ToolStep(
        String id,
        String name,
        JsonNode args,
        String rawArgs,
        String status,
        String output,
        Boolean truncated,
        int seq,
        Long durationMs
) {

    public static ToolStep started(int seq, String id, String name,
                                   JsonNode args, String rawArgs) {
        return new ToolStep(id, name, args, rawArgs, "running", null, null, seq, null);
    }

    public ToolStep finished(String status, String output,
                             boolean truncated, Long durationMs) {
        return new ToolStep(id, name, args, rawArgs, status, output,
                truncated, seq, durationMs);
    }
}
