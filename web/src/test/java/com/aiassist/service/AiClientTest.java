package com.aiassist.service;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

import com.aiassist.client.StreamHandler;
import com.aiassist.config.AiServiceProperties;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.web.client.RestClient;

import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

/**
 * AiClient SSE 帧分发单测（阶段1 tool_call/tool_result）。
 * 不走 HTTP：parseSseStream 包可见，直接喂构造的 SSE InputStream；
 * RestClient.Builder 用 RETURNS_SELF 桩掉链式构造，build() 返回 mock。
 */
class AiClientTest {

    private AiClient aiClient;
    private StreamHandler handler;

    @BeforeEach
    void setUp() {
        RestClient.Builder builder = mock(RestClient.Builder.class,
                org.mockito.Answers.RETURNS_SELF);
        when(builder.build()).thenReturn(mock(RestClient.class));
        AiServiceProperties props = new AiServiceProperties(
                "http://ai", Duration.ofSeconds(5), Duration.ofSeconds(120), 10);
        aiClient = new AiClient(builder, props, new ObjectMapper());
        handler = mock(StreamHandler.class);
    }

    private void parse(String sse) throws Exception {
        aiClient.parseSseStream(
                new ByteArrayInputStream(sse.getBytes(StandardCharsets.UTF_8)), handler);
    }

    @Test
    void toolFrames_dispatchedInOrderWithPairedFields() throws Exception {
        String sse = """
                event: meta
                data: {"model":"deepseek-flash"}

                event: tool_call
                data: {"id":"t1","name":"read_file","args":{"path":"a.txt","offset":1}}

                event: tool_result
                data: {"id":"t1","name":"read_file","status":"success","output":"1 | hi","truncated":false}

                event: tool_call
                data: {"id":"t2","name":"read_file","args":null,"raw_args":"bad-json"}

                event: tool_result
                data: {"id":"t2","name":"read_file","status":"error","output":"参数不是合法 JSON","truncated":false}

                event: done
                data: {"answer":"x"}
                """;
        parse(sse);

        InOrder ordered = inOrder(handler);
        ordered.verify(handler).onMeta("deepseek-flash");
        ordered.verify(handler).onToolCall(
                eq("t1"), eq("read_file"),
                argThat((JsonNode n) -> "a.txt".equals(n.path("path").asText())
                        && n.path("offset").asInt() == 1),
                isNull());
        ordered.verify(handler).onToolResult(
                "t1", "read_file", "success", "1 | hi", false);
        ordered.verify(handler).onToolCall(
                eq("t2"), eq("read_file"), isNull(), eq("bad-json"));
        ordered.verify(handler).onToolResult(
                "t2", "read_file", "error", "参数不是合法 JSON", false);
        // done 帧不产生回调
        verifyNoMoreInteractions(handler);
    }

    @Test
    void toolCall_missingFields_safeDefaults() throws Exception {
        parse("event: tool_call\ndata: {\"id\":\"t3\"}\n\n");
        verify(handler).onToolCall(eq("t3"), eq(""), isNull(), isNull());
    }

    @Test
    void confirmFrame_dispatchedInOrderBetweenToolFrames() throws Exception {
        String sse = """
                event: tool_call
                data: {"id":"t1","name":"write_file","args":{"path":"a.txt"}}

                event: confirm
                data: {"id":"cf1","tool":"write_file","summary":"写入 a.txt（新建）"}

                event: tool_result
                data: {"id":"t1","name":"write_file","status":"success","output":"[write_file] 已写入 a.txt（3 字节，新建）","truncated":false}

                event: done
                data: {"answer":"ok"}
                """;
        parse(sse);

        InOrder ordered = inOrder(handler);
        ordered.verify(handler).onToolCall(
                eq("t1"), eq("write_file"),
                argThat((JsonNode n) -> "a.txt".equals(n.path("path").asText())),
                isNull());
        ordered.verify(handler).onConfirm("cf1", "write_file", "写入 a.txt（新建）");
        ordered.verify(handler).onToolResult(
                "t1", "write_file", "success",
                "[write_file] 已写入 a.txt（3 字节，新建）", false);
        // done 帧不产生回调
        verifyNoMoreInteractions(handler);
    }

    @Test
    void confirmFrame_missingFields_safeDefaults() throws Exception {
        parse("event: confirm\ndata: {\"id\":\"cf2\"}\n\n");
        verify(handler).onConfirm("cf2", "", "");
    }

    @Test
    void toolResult_missingStatus_defaultsToError() throws Exception {
        parse("event: tool_result\ndata: {\"id\":\"t4\"}\n\n");
        verify(handler).onToolResult(eq("t4"), eq(""), eq("error"), eq(""), eq(false));
    }

    @Test
    void unknownFrame_ignored() throws Exception {
        parse("event: future_frame\ndata: {\"x\":1}\n\n");
        verifyNoInteractions(handler);
    }
}
