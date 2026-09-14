package com.aiassist.controller;

import java.util.List;
import java.util.Map;

import com.aiassist.client.dto.AiHealthResponse;
import com.aiassist.dto.CreateConversationRequest;
import com.aiassist.dto.RenameConversationRequest;
import com.aiassist.dto.SendMessageRequest;
import com.aiassist.exception.ResourceNotFoundException;
import com.aiassist.model.Conversation;
import com.aiassist.service.AiClient;
import com.aiassist.service.ChatService;
import com.aiassist.service.ChatStreamService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 前端 REST 接口。
 * 跨域由 WebConfig 统一配置，异常由 GlobalExceptionHandler 统一处理。
 */
@RestController
@RequestMapping("/api")
public class ChatController {

    private final ChatService chatService;
    private final ChatStreamService chatStreamService;
    private final AiClient aiClient;

    public ChatController(ChatService chatService, ChatStreamService chatStreamService,
                          AiClient aiClient) {
        this.chatService = chatService;
        this.chatStreamService = chatStreamService;
        this.aiClient = aiClient;
    }

    /** 对话列表 */
    @GetMapping("/conversations")
    public List<Conversation> listConversations() {
        return chatService.listConversations();
    }

    /** 新建对话 */
    @PostMapping("/conversations")
    public Conversation createConversation(@RequestBody(required = false) CreateConversationRequest body) {
        String title = body == null ? null : body.title();
        return chatService.createConversation(title);
    }

    /** 删除对话 */
    @DeleteMapping("/conversations/{id}")
    public Map<String, Object> deleteConversation(@PathVariable String id) {
        chatService.deleteConversation(id);
        // best-effort 清理 AI 模块的摘要缓存，失败不影响删除
        aiClient.forgetMemory(id);
        return Map.of("success", true, "id", id);
    }

    /** 获取对话详情(含全部消息)；不存在按 404 处理（而非 400 参数错误） */
    @GetMapping("/conversations/{id}")
    public Conversation getConversation(@PathVariable String id) {
        Conversation c = chatService.getConversation(id);
        if (c == null) {
            throw new ResourceNotFoundException("对话不存在: " + id);
        }
        return c;
    }

    /** 手动重命名对话 */
    @PatchMapping("/conversations/{id}")
    public Conversation renameConversation(@PathVariable String id,
                                           @RequestBody RenameConversationRequest body) {
        return chatService.renameConversation(id, body == null ? null : body.title());
    }

    /** 流式发送消息（SSE）；校验失败在响应头提交前返回 400 */
    @PostMapping(path = "/conversations/{id}/chat/stream", produces = "text/event-stream;charset=UTF-8")
    public SseEmitter chatStream(@PathVariable String id, @RequestBody SendMessageRequest body) {
        return chatStreamService.start(id, body);
    }

    /** AI 模块健康/配置状态(便于前端提示 key 是否已配置) */
    @GetMapping("/ai/status")
    public AiHealthResponse aiStatus() {
        return aiClient.health();
    }
}
