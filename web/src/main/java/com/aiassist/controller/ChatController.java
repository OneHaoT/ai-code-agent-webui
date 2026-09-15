package com.aiassist.controller;

import java.io.IOException;
import java.util.List;
import java.util.Map;

import com.aiassist.client.dto.AiHealthResponse;
import com.aiassist.dto.ConfirmDecisionRequest;
import com.aiassist.dto.CreateConversationRequest;
import com.aiassist.dto.RenameConversationRequest;
import com.aiassist.dto.SendMessageRequest;
import com.aiassist.exception.ResourceNotFoundException;
import com.aiassist.model.Conversation;
import com.aiassist.service.AiClient;
import com.aiassist.service.ChatService;
import com.aiassist.service.ChatStreamService;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
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
        String ws = body == null ? null : body.workspaceRoot();
        return chatService.createConversation(title, ws);
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

    /** 更新对话元数据（标题 / 工作区；均可选） */
    @PatchMapping("/conversations/{id}")
    public Conversation updateConversation(@PathVariable String id,
                                           @RequestBody(required = false) RenameConversationRequest body) {
        String title = body == null ? null : body.title();
        String ws = body == null ? null : body.workspaceRoot();
        return chatService.updateConversation(id, title, ws);
    }

    /** 流式发送消息（SSE）；校验失败在响应头提交前返回 400 */
    @PostMapping(path = "/conversations/{id}/chat/stream", produces = "text/event-stream;charset=UTF-8")
    public SseEmitter chatStream(@PathVariable String id, @RequestBody SendMessageRequest body) {
        return chatStreamService.start(id, body);
    }

    /**
     * 转发用户对 confirm 帧的决策（阶段3 写/执行工具人机确认）。
     * web 不理解 confirm 语义，仅校验参数后转发 POST /ai/confirm；
     * confirmId 未知/已失效时 ai 返回 404，此处透传 404。
     */
    @PostMapping("/conversations/{id}/confirm")
    public JsonNode confirm(@PathVariable String id, @RequestBody ConfirmDecisionRequest body) {
        if (body == null || body.confirmId() == null || body.confirmId().isBlank()) {
            throw new IllegalArgumentException("confirmId 不能为空");
        }
        if (body.approved() == null) {
            throw new IllegalArgumentException("approved 必须为布尔值");
        }
        return aiClient.confirmExecution(body.confirmId(), body.approved());
    }

    /** AI 模块健康/配置状态(便于前端提示 key 是否已配置) */
    @GetMapping("/ai/status")
    public AiHealthResponse aiStatus() {
        return aiClient.health();
    }

    /** 把前端拖入的单个文件保存到 AI 默认工作区 */
    @PostMapping(value = "/workspace/files", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public JsonNode uploadWorkspaceFile(@RequestPart("file") MultipartFile file) throws IOException {
        if (file.isEmpty()) {
            throw new IllegalArgumentException("文件为空");
        }
        return aiClient.uploadWorkspaceFile(
                file.getOriginalFilename() != null ? file.getOriginalFilename() : "upload",
                file.getBytes());
    }

    /** 让 AI 模块在本机文件浏览器中打开默认工作区 */
    @PostMapping("/workspace/open")
    public JsonNode openWorkspace() {
        return aiClient.openWorkspace();
    }

    /**
     * 只读浏览工作区单层目录（阶段3.5 项目树）。
     * root = 对话绑定的工作区绝对路径（空 = AI 默认工作区）；path = 相对子目录。
     */
    @GetMapping("/workspace/list")
    public JsonNode listWorkspace(
            @RequestParam(required = false) String root,
            @RequestParam(required = false) String path) {
        return aiClient.listWorkspace(root, path);
    }
}
