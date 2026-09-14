package com.aiassist.service;

import java.io.IOException;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import com.aiassist.client.StreamHandler;
import com.aiassist.dto.SendMessageRequest;
import com.aiassist.exception.AiServiceException;
import com.aiassist.model.Message;
import com.aiassist.model.ToolStep;
import com.aiassist.service.ChatService.PreparedChat;
import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 流式对话编排：ChatService（prepare/complete/rollback）+ AiClient.streamChat + SseEmitter。
 *
 * 时序：
 *   1. prepare 在请求线程同步完成（校验失败直接 400/502，此时 SSE 响应头尚未发出）；
 *   2. 提交到 sseExecutor 后台线程跑 SSE 流，Tomcat 线程立即释放；
 *   3. meta/token/reasoning 帧透传给前端，同时累积全文；
 *   4. 成功 -> complete 落库 assistant 消息 -> done 帧（带完整 Message JSON）；
 *      失败 -> rollback 删除已落库的用户消息和图片 -> error 帧；
 *   5. 客户端提前断开只停止向连接写帧，AI 调用与落库仍正常完成（消息不丢）。
 */
@Service
public class ChatStreamService {

    private static final Logger log = LoggerFactory.getLogger(ChatStreamService.class);

    /** SSE 总超时：略大于 AI 模块读取超时（180s），留出收尾时间 */
    private static final long SSE_TIMEOUT_MS = 300_000L;

    private final ChatService chatService;
    private final AiClient aiClient;
    private final ThreadPoolTaskExecutor sseExecutor;

    public ChatStreamService(ChatService chatService, AiClient aiClient,
                             @Qualifier("sseExecutor") ThreadPoolTaskExecutor sseExecutor) {
        this.chatService = chatService;
        this.aiClient = aiClient;
        this.sseExecutor = sseExecutor;
    }

    /**
     * 队列满时抛 RejectedExecutionException（全局异常处理转 503）。
     * 注意：prepare() 已把用户消息落库，拒绝时必须先回滚，
     * 否则线程池打满的瞬间会留下一批无回应的孤儿用户消息与图片。
     */
    public SseEmitter start(String conversationId, SendMessageRequest body) {
        PreparedChat prepared = chatService.prepare(
                conversationId, body.message(), body.images(),
                Boolean.TRUE.equals(body.thinking()));

        SseEmitter emitter = new SseEmitter(SSE_TIMEOUT_MS);
        AtomicBoolean clientAlive = new AtomicBoolean(true);
        emitter.onError(e -> clientAlive.set(false));
        emitter.onCompletion(() -> clientAlive.set(false));
        emitter.onTimeout(() -> clientAlive.set(false));

        try {
            sseExecutor.execute(() -> runStream(prepared, emitter, clientAlive));
        } catch (RejectedExecutionException e) {
            try {
                chatService.rollback(prepared);
            } catch (Exception rollbackError) {
                log.error("线程池满后回滚也失败 conversationId={}",
                        conversationId, rollbackError);
            }
            throw e;
        }
        return emitter;
    }

    private void runStream(PreparedChat prepared, SseEmitter emitter, AtomicBoolean clientAlive) {
        StringBuilder answer = new StringBuilder();
        StringBuilder reasoning = new StringBuilder();
        // 工具轨迹：按 id 配对、按到达有序（tool_call 建 running 步骤，result 补全）
        Map<String, ToolStep> toolSteps = new LinkedHashMap<>();
        Map<String, Long> toolStartNanos = new HashMap<>();
        AtomicInteger toolSeq = new AtomicInteger();

        try {
            aiClient.streamChat(
                    prepared.conversationId(), prepared.content(),
                    prepared.currentImages(), prepared.history(), prepared.thinking(),
                    new StreamHandler() {
                        @Override
                        public void onMeta(String model) {
                            sendFrame(emitter, clientAlive, "meta", Map.of("model", model));
                        }

                        @Override
                        public void onToken(String delta) {
                            answer.append(delta);
                            sendFrame(emitter, clientAlive, "token", Map.of("delta", delta));
                        }

                        @Override
                        public void onReasoning(String delta) {
                            reasoning.append(delta);
                            sendFrame(emitter, clientAlive, "reasoning", Map.of("delta", delta));
                        }

                        @Override
                        public void onToolCall(String id, String name,
                                               JsonNode args, String rawArgs) {
                            int seq = toolSeq.incrementAndGet();
                            toolSteps.put(id, ToolStep.started(seq, id, name, args, rawArgs));
                            toolStartNanos.put(id, System.nanoTime());
                            // HashMap：args 允许为 null（非法参数帧），需序列化为 "args":null
                            Map<String, Object> data = new HashMap<>();
                            data.put("id", id);
                            data.put("name", name);
                            data.put("args", args);
                            if (rawArgs != null) {
                                data.put("raw_args", rawArgs);
                            }
                            sendFrame(emitter, clientAlive, "tool_call", data);
                        }

                        @Override
                        public void onToolResult(String id, String name, String status,
                                                 String output, boolean truncated) {
                            Long durationMs = null;
                            Long startNanos = toolStartNanos.get(id);
                            if (startNanos != null) {
                                durationMs = TimeUnit.NANOSECONDS.toMillis(
                                        System.nanoTime() - startNanos);
                            }
                            ToolStep finished;
                            ToolStep prev = toolSteps.get(id);
                            if (prev != null) {
                                finished = prev.finished(status, output, truncated, durationMs);
                            } else {
                                // 防御：无配对 call 的 result（协议不允许出现），补占位步骤
                                finished = new ToolStep(id, name, null, null,
                                        status, output, truncated,
                                        toolSeq.incrementAndGet(), durationMs);
                            }
                            toolSteps.put(id, finished);
                            sendFrame(emitter, clientAlive, "tool_result", Map.of(
                                    "id", id, "name", name, "status", status,
                                    "output", output, "truncated", truncated));
                        }
                    });

            if (answer.isEmpty() || answer.toString().isBlank()) {
                throw new AiServiceException("AI 模块返回内容为空");
            }

            List<ToolStep> toolTrace = toolSteps.isEmpty()
                    ? null : List.copyOf(toolSteps.values());
            // 即使客户端已断开也完成落库（后台任务语义：消息不丢）
            Message saved = chatService.complete(
                    prepared, answer.toString(),
                    reasoning.length() == 0 ? null : reasoning.toString(),
                    toolTrace);

            sendFrame(emitter, clientAlive, "done", Map.of("message", saved));
            safeComplete(emitter);
        } catch (Exception e) {
            log.warn("流式对话失败 conversationId={}: {}",
                    prepared.conversationId(), e.getMessage());
            try {
                chatService.rollback(prepared);
            } catch (Exception rollbackError) {
                log.error("流式失败后回滚也失败 conversationId={}",
                        prepared.conversationId(), rollbackError);
            }
            sendFrame(emitter, clientAlive, "error", Map.of("message", userFacingMessage(e)));
            safeComplete(emitter);
        }
    }

    /** 客户端已断开时静默放弃；emitter 结束后的重复发送同样忽略 */
    private void sendFrame(SseEmitter emitter, AtomicBoolean clientAlive,
                           String event, Object data) {
        if (!clientAlive.get()) {
            return;
        }
        try {
            emitter.send(SseEmitter.event()
                    .name(event)
                    .data(data, MediaType.APPLICATION_JSON));
        } catch (IOException | IllegalStateException e) {
            // 连接断开/响应已提交结束：标记后让后台任务继续跑完落库
            clientAlive.set(false);
        }
    }

    private void safeComplete(SseEmitter emitter) {
        try {
            emitter.complete();
        } catch (Exception ignored) {
            // 客户端断开或异步容器已回收，无需处理
        }
    }

    private String userFacingMessage(Exception e) {
        String msg = e.getMessage();
        return msg == null || msg.isBlank() ? "AI 服务暂时不可用，请稍后重试" : msg;
    }
}
