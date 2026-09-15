package com.aiassist.service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.RejectedExecutionException;

import com.aiassist.client.StreamHandler;
import com.aiassist.dto.SendMessageRequest;
import com.aiassist.exception.AiServiceException;
import com.aiassist.model.Message;
import com.aiassist.model.ToolStep;
import com.aiassist.service.ChatService.PreparedChat;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.MockedConstruction;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.servlet.mvc.method.annotation.ResponseBodyEmitter;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockConstruction;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * ChatStreamService 编排单测：成功落库 done、失败回滚 error、空回答保护、
 * 线程池拒绝时先回滚用户消息再重抛（终验审计缺陷 3 的回归护栏）。
 * SseEmitter 由 mockConstruction 接管；executor 在当前线程同步执行，测试确定性运行。
 */
class ChatStreamServiceTest {

    private ChatService chatService;
    private AiClient aiClient;
    private ThreadPoolTaskExecutor executor;
    private ChatStreamService streamService;

    @BeforeEach
    void setUp() {
        chatService = mock(ChatService.class);
        aiClient = mock(AiClient.class);
        executor = mock(ThreadPoolTaskExecutor.class);
        // 模拟提交即执行（确定性，无需真实线程池）
        doAnswer(inv -> {
            ((Runnable) inv.getArgument(0)).run();
            return null;
        }).when(executor).execute(any(Runnable.class));
        streamService = new ChatStreamService(chatService, aiClient, executor);
    }

    private PreparedChat prepared() {
        return new PreparedChat("c1", "hi", List.of(),
                new Message("user", "hi"), List.of(), List.of(), false, null);
    }

    private SendMessageRequest request() {
        return new SendMessageRequest("hi", List.of(), false);
    }

    @Test
    void start_happyStream_completesAndSavesAssistantMessage() throws Exception {
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doAnswer(inv -> {
            StreamHandler handler = inv.getArgument(6);
            handler.onMeta("deepseek-flash");
            handler.onToken("你好");
            return null;
        }).when(aiClient).streamChat(anyString(), anyString(), any(), any(), anyBoolean(), any(), any());
        when(chatService.complete(any(), eq("你好"), isNull(), isNull()))
                .thenReturn(new Message("assistant", "你好"));

        try (MockedConstruction<SseEmitter> mocked = mockConstruction(SseEmitter.class)) {
            SseEmitter emitter = streamService.start("c1", request());

            SseEmitter mockEmitter = mocked.constructed().get(0);
            assertThat(emitter).isSameAs(mockEmitter);
            verify(chatService).complete(any(), eq("你好"), isNull(), isNull());
            verify(chatService, never()).rollback(any());
            verify(mockEmitter).complete();
            verify(mockEmitter, org.mockito.Mockito.atLeastOnce())
                    .send(any(SseEmitter.SseEventBuilder.class));
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void start_toolFrames_passthroughInOrderAndAccumulatesPairedTrace() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doAnswer(inv -> {
            StreamHandler handler = inv.getArgument(6);
            handler.onMeta("deepseek-flash");
            handler.onToolCall("s1", "read_file",
                    mapper.readTree("{\"path\":\"a.txt\"}"), null);
            handler.onToolResult("s1", "read_file", "success", "1 | hi", false);
            handler.onToolCall("s2", "read_file", null, "raw!");
            handler.onToolResult("s2", "read_file", "error", "参数错误", true);
            handler.onToken("答案");
            return null;
        }).when(aiClient).streamChat(anyString(), anyString(), any(), any(), anyBoolean(), any(), any());
        when(chatService.complete(any(), eq("答案"), isNull(), anyList()))
                .thenReturn(new Message("assistant", "答案"));

        try (MockedConstruction<SseEmitter> mocked = mockConstruction(SseEmitter.class)) {
            streamService.start("c1", request());
            SseEmitter mockEmitter = mocked.constructed().get(0);

            // 1) 落库轨迹：按 id 配对、顺序/状态/耗时/参数正确
            ArgumentCaptor<List<ToolStep>> traceCap = ArgumentCaptor.forClass(List.class);
            verify(chatService).complete(any(), eq("答案"), isNull(), traceCap.capture());
            List<ToolStep> trace = traceCap.getValue();
            assertThat(trace).hasSize(2);
            ToolStep s1 = trace.get(0);
            assertThat(s1.id()).isEqualTo("s1");
            assertThat(s1.name()).isEqualTo("read_file");
            assertThat(s1.status()).isEqualTo("success");
            assertThat(s1.seq()).isEqualTo(1);
            assertThat(s1.truncated()).isFalse();
            assertThat(s1.durationMs()).isNotNull().isNotNegative();
            assertThat(s1.output()).isEqualTo("1 | hi");
            assertThat(s1.args().get("path").asText()).isEqualTo("a.txt");
            assertThat(s1.rawArgs()).isNull();
            ToolStep s2 = trace.get(1);
            assertThat(s2.id()).isEqualTo("s2");
            assertThat(s2.status()).isEqualTo("error");
            assertThat(s2.seq()).isEqualTo(2);
            assertThat(s2.truncated()).isTrue();
            assertThat(s2.args()).isNull();
            assertThat(s2.rawArgs()).isEqualTo("raw!");

            // 2) 透传帧：meta + 2*(tool_call/tool_result) + token + done = 7，名称有序
            ArgumentCaptor<SseEmitter.SseEventBuilder> frameCap =
                    ArgumentCaptor.forClass(SseEmitter.SseEventBuilder.class);
            verify(mockEmitter, times(7)).send(frameCap.capture());
            List<String> frameNames = new ArrayList<>();
            List<Object> framePayloads = new ArrayList<>();
            for (SseEmitter.SseEventBuilder builder : frameCap.getAllValues()) {
                Set<ResponseBodyEmitter.DataWithMediaType> datas = builder.build();
                datas.forEach(d -> {
                    Object data = d.getData();
                    // SseEventBuilder 把 "event:<name>\ndata:" 合并为一个字符串块；
                    // 帧分隔块 "\n\n" 忽略；真正的负载是 Map 等非字符串对象
                    if (data instanceof String s && s.startsWith("event:")) {
                        frameNames.add(s.substring("event:".length(), s.indexOf('\n')));
                    } else if (!(data instanceof String)) {
                        framePayloads.add(data);
                    }
                });
            }
            assertThat(frameNames).containsExactly(
                    "meta", "tool_call", "tool_result", "tool_call", "tool_result",
                    "token", "done");

            // payload 顺序与名称一一对应（meta/call/result/call/result/token/done）
            Map<String, Object> call1 = (Map<String, Object>) framePayloads.get(1);
            assertThat(call1).containsEntry("id", "s1")
                    .containsEntry("name", "read_file");
            assertThat(call1.get("args").toString()).contains("a.txt");
            assertThat(call1).doesNotContainKey("raw_args");
            Map<String, Object> call2 = (Map<String, Object>) framePayloads.get(3);
            // 非法参数：args 必须序列化为 null（不能缺字段），raw_args 透传
            assertThat(call2).containsEntry("args", null)
                    .containsEntry("raw_args", "raw!");
            Map<String, Object> result2 = (Map<String, Object>) framePayloads.get(4);
            assertThat(result2).containsEntry("id", "s2")
                    .containsEntry("status", "error")
                    .containsEntry("truncated", true);
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void start_confirmFrame_forwardedInOrderAndNotAccumulatedInTrace() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doAnswer(inv -> {
            StreamHandler handler = inv.getArgument(6);
            handler.onMeta("deepseek-flash");
            handler.onToolCall("t1", "write_file",
                    mapper.readTree("{\"path\":\"a.txt\",\"content\":\"hi\"}"), null);
            handler.onConfirm("cf1", "write_file", "写入 a.txt（新建）");
            handler.onToolResult("t1", "write_file", "success",
                    "[write_file] 已写入 a.txt（2 字节，新建）", false);
            handler.onToken("完成");
            return null;
        }).when(aiClient).streamChat(anyString(), anyString(), any(), any(), anyBoolean(), any(), any());
        when(chatService.complete(any(), eq("完成"), isNull(), anyList()))
                .thenReturn(new Message("assistant", "完成"));

        try (MockedConstruction<SseEmitter> mocked = mockConstruction(SseEmitter.class)) {
            streamService.start("c1", request());
            SseEmitter mockEmitter = mocked.constructed().get(0);

            // 1) 轨迹不含 confirm：只有配对的 tool_call/tool_result 步骤
            ArgumentCaptor<List<ToolStep>> traceCap = ArgumentCaptor.forClass(List.class);
            verify(chatService).complete(any(), eq("完成"), isNull(), traceCap.capture());
            assertThat(traceCap.getValue()).hasSize(1);
            assertThat(traceCap.getValue().get(0).id()).isEqualTo("t1");

            // 2) 透传帧：meta + tool_call + confirm + tool_result + token + done = 6，名称有序
            ArgumentCaptor<SseEmitter.SseEventBuilder> frameCap =
                    ArgumentCaptor.forClass(SseEmitter.SseEventBuilder.class);
            verify(mockEmitter, times(6)).send(frameCap.capture());
            List<String> frameNames = new ArrayList<>();
            List<Object> framePayloads = new ArrayList<>();
            for (SseEmitter.SseEventBuilder builder : frameCap.getAllValues()) {
                for (ResponseBodyEmitter.DataWithMediaType d : builder.build()) {
                    Object data = d.getData();
                    if (data instanceof String s && s.startsWith("event:")) {
                        frameNames.add(s.substring("event:".length(), s.indexOf('\n')));
                    } else if (!(data instanceof String)) {
                        framePayloads.add(data);
                    }
                }
            }
            assertThat(frameNames).containsExactly(
                    "meta", "tool_call", "confirm", "tool_result", "token", "done");

            // confirm 帧三字段原样透传
            Map<String, Object> confirmPayload = (Map<String, Object>) framePayloads.get(2);
            assertThat(confirmPayload).containsEntry("id", "cf1")
                    .containsEntry("tool", "write_file")
                    .containsEntry("summary", "写入 a.txt（新建）");
        }
    }

    @Test
    void start_aiFailure_rollsBackAndCompletesWithErrorFrame() {
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doThrow(new AiServiceException("AI 模块不可用"))
                .when(aiClient).streamChat(anyString(), anyString(), any(), any(), anyBoolean(), any(), any());

        try (MockedConstruction<SseEmitter> mocked = mockConstruction(SseEmitter.class)) {
            streamService.start("c1", request());

            verify(chatService).rollback(any());
            verify(chatService, never()).complete(any(), anyString(), any(), any());
            verify(mocked.constructed().get(0)).complete();
        }
    }

    @Test
    void start_emptyAnswer_treatedAsFailureAndRollsBack() {
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doAnswer(inv -> {
            ((StreamHandler) inv.getArgument(6)).onMeta("deepseek-flash");
            return null; // 一个 token 都没有
        }).when(aiClient).streamChat(anyString(), anyString(), any(), any(), anyBoolean(), any(), any());

        try (MockedConstruction<SseEmitter> mocked = mockConstruction(SseEmitter.class)) {
            streamService.start("c1", request());

            verify(chatService).rollback(any());
            verify(chatService, never()).complete(any(), anyString(), any(), any());
            verify(mocked.constructed().get(0)).complete();
        }
    }

    @Test
    void start_executorRejected_rollsBackBeforeRethrowing() {
        when(chatService.prepare(eq("c1"), any(), any(), eq(false))).thenReturn(prepared());
        doThrow(new RejectedExecutionException("队列已满"))
                .when(executor).execute(any(Runnable.class));

        // 使用真实 SseEmitter：拒绝发生在提交瞬间，不会发送任何帧，无需容器参与
        assertThatThrownBy(() -> streamService.start("c1", request()))
                .isInstanceOf(RejectedExecutionException.class);

        verify(chatService).rollback(any());
        verify(chatService, never()).complete(any(), anyString(), any(), any());
    }
}
