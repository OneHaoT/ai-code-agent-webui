package com.aiassist.service;

import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.io.UncheckedIOException;

import com.aiassist.client.dto.AiHistoryMessage;
import com.aiassist.config.AiServiceProperties;
import com.aiassist.exception.ResourceNotFoundException;
import com.aiassist.model.Conversation;
import com.aiassist.model.ImageRef;
import com.aiassist.model.Message;
import com.aiassist.model.ToolStep;
import com.aiassist.storage.StoredImage;
import com.aiassist.storage.StorageService;
import com.aiassist.store.ConversationStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * 对话业务: 管理 CRUD 与消息收发。
 * 每个对话(conversation)拥有独立消息列表，发给 AI 模块时仅携带该会话历史，
 * 因此各对话的 AI 记忆相互隔离。
 *
 * 收发主流程被拆成 prepare() / complete() / rollback() 三段，供流式 SSE 入口复用：
 *   prepare  —— 校验、历史快照、图片转 data URL、先落库用户消息
 *   complete —— AI 成功后落库 assistant 消息
 *   rollback —— AI 失败时移除用户消息并删除本轮图片
 */
@Service
public class ChatService {

    private static final Logger log = LoggerFactory.getLogger(ChatService.class);

    /** 单条消息最多携带的图片数（与前端 MAX_IMAGES 收紧为同一产品限制） */
    private static final int MAX_IMAGES_PER_MESSAGE = 4;

    private final ConversationStore store;
    private final StorageService storageService;
    private final AiServiceProperties aiServiceProperties;

    public ChatService(ConversationStore store, StorageService storageService,
                       AiServiceProperties aiServiceProperties) {
        this.store = store;
        this.storageService = storageService;
        this.aiServiceProperties = aiServiceProperties;
    }

    public Conversation createConversation(String title, String workspaceRoot) {
        return store.create(title, workspaceRoot);
    }

    public List<Conversation> listConversations() {
        return store.list();
    }

    public void deleteConversation(String id) {
        // 级联删除该对话引用的所有图片文件（best-effort，失败只记日志，不阻断删除）
        Conversation conv = store.get(id);
        if (conv != null) {
            conv.getMessages().forEach(m -> deleteRefsQuietly(m.getImages()));
        }
        store.delete(id);
    }

    public Conversation getConversation(String id) {
        return store.get(id);
    }

    /** 更新对话元数据：title 和 workspaceRoot 都是可选字段（null 表示不更新）。 */
    public Conversation updateConversation(String id, String title, String workspaceRoot) {
        String trimmedTitle = null;
        if (title != null) {
            String t = title.trim();
            if (t.isEmpty()) {
                throw new IllegalArgumentException("标题不能为空");
            }
            if (t.codePointCount(0, t.length()) > 50) {
                throw new IllegalArgumentException("标题最多 50 个字符");
            }
            trimmedTitle = t;
        }
        // workspaceRoot：空串视为清空（设为 null），null 表示不更新
        String ws = workspaceRoot;
        if (workspaceRoot != null && workspaceRoot.trim().isEmpty()) {
            ws = "";
        }
        Conversation updated = store.update(id, trimmedTitle, ws);
        if (updated == null) {
            throw new ResourceNotFoundException("对话不存在: " + id);
        }
        return updated;
    }

    /** 一次已完成校验的待发送上下文 */
    public record PreparedChat(
            String conversationId,
            String content,
            List<ImageRef> refs,
            Message userMessage,
            List<AiHistoryMessage> history,
            List<String> currentImages,
            boolean thinking,
            /** 该对话绑定的 workspace_root（null 表示 AI 默认） */
            String workspaceRoot
    ) {
    }

    /**
     * 发送前准备（非流式/流式共用）：
     * 1. 校验文本/图片数量、对话存在
     * 2. 取本轮之前的历史快照并转 AI 模块 DTO
     * 3. 图片读为 base64 data URL
     * 4. 用户消息落库（AI 失败时由 rollback 撤销）
     */
    public PreparedChat prepare(String conversationId, String text,
                                List<ImageRef> images, boolean thinking) {
        String content = text == null ? "" : text.trim();
        List<ImageRef> refs = images == null ? List.of() : images;
        if (content.isBlank() && refs.isEmpty()) {
            throw new IllegalArgumentException("消息不能为空");
        }
        if (refs.size() > MAX_IMAGES_PER_MESSAGE) {
            throw new IllegalArgumentException("单条消息最多上传 " + MAX_IMAGES_PER_MESSAGE + " 张图片");
        }

        Conversation conv = store.get(conversationId);
        if (conv == null) {
            throw new IllegalArgumentException("对话不存在: " + conversationId);
        }

        // 历史快照: 不包含本轮用户消息。
        // 只有最近 historyImageWindow 条消息携带图片 data URL（与 AI 模块滑动窗口对齐），
        // 更早的消息只传图片数量：AI 折叠摘要时仅保留“曾附带 N 张图片”的文本标记，
        // 避免长会话每轮都把全部历史图片读盘+base64+走一遍网络。
        List<Message> history = conv.getMessages();
        int imageWindowCutoff = Math.max(0, history.size() - aiServiceProperties.historyImageWindow());
        List<AiHistoryMessage> historyDto = new ArrayList<>(history.size());
        for (int i = 0; i < history.size(); i++) {
            historyDto.add(toHistoryDto(history.get(i), i >= imageWindowCutoff));
        }
        List<String> currentImages = toDataUrls(refs);

        // 校验通过后再记录用户消息（只存图片引用，字节由存储层管理）
        Message userMsg = new Message("user", content, refs.isEmpty() ? null : refs);
        Message saved = store.appendMessage(conversationId, userMsg);

        return new PreparedChat(conversationId, content, refs, saved,
                historyDto, currentImages, thinking, conv.getWorkspaceRoot());
    }

    /**
     * AI 成功：落库 assistant 消息（思考模式下同时保存思维链，
     * 阶段1起可能携带只读工具调用轨迹；toolTrace 为空时不设置，JSON 不输出该字段）
     */
    public Message complete(PreparedChat prepared, String answer, String reasoning,
                            List<ToolStep> toolTrace) {
        Message aiMsg = new Message("assistant", answer);
        if (reasoning != null && !reasoning.isBlank()) {
            aiMsg.setReasoning(reasoning);
        }
        if (toolTrace != null && !toolTrace.isEmpty()) {
            aiMsg.setToolTrace(List.copyOf(toolTrace));
        }
        return store.appendMessage(prepared.conversationId(), aiMsg);
    }

    /** AI 失败：撤销已落库的用户消息并删除本轮上传图片，保证消息与文件一致 */
    public void rollback(PreparedChat prepared) {
        store.removeMessage(prepared.conversationId(), prepared.userMessage());
        deleteRefsQuietly(prepared.refs());
    }

    /** best-effort 批量删除图片文件（级联清理/失败回滚用），异常只记日志不外抛 */
    private void deleteRefsQuietly(List<ImageRef> refs) {
        if (refs == null || refs.isEmpty()) {
            return;
        }
        for (ImageRef ref : refs) {
            try {
                storageService.delete(ref.id());
            } catch (Exception e) {
                log.warn("删除图片文件失败（等待 GC 兜底）id={}: {}", ref.id(), e.getMessage());
            }
        }
    }

    /**
     * @param includeImageData 该消息是否在图片窗口内：窗口内读文件转 data URL，
     *                         窗口外只传图片数量（images 为空列表，imageCount 保留计数）
     */
    private AiHistoryMessage toHistoryDto(Message m, boolean includeImageData) {
        String text = m.getContent() == null ? "" : m.getContent();
        List<ImageRef> refs = m.getImages();
        int imageCount = refs == null ? 0 : refs.size();
        if (!includeImageData || imageCount == 0) {
            return new AiHistoryMessage(m.getRole(), text, List.of(), imageCount);
        }
        List<String> images = toDataUrls(refs);
        return new AiHistoryMessage(m.getRole(), text,
                images == null ? List.of() : images, imageCount);
    }

    /**
     * 图片引用 -> AI 模块需要的 base64 data URL 列表。
     * 单张图片文件丢失（误删/磁盘损坏/历史遗留）只跳过并告警，不阻断整条对话，
     * 否则一个坏文件会让该会话之后所有消息都无法发送。
     */
    private List<String> toDataUrls(List<ImageRef> refs) {
        if (refs == null || refs.isEmpty()) {
            return null;
        }
        List<String> result = new ArrayList<>(refs.size());
        Base64.Encoder encoder = Base64.getEncoder();
        for (ImageRef ref : refs) {
            try {
                StoredImage stored = storageService.load(ref.id());
                String dataUrl = "data:" + stored.contentType()
                        + ";base64," + encoder.encodeToString(stored.data());
                result.add(dataUrl);
            } catch (ResourceNotFoundException | UncheckedIOException e) {
                log.warn("图片文件丢失，已跳过 id={}: {}", ref.id(), e.getMessage());
            }
        }
        return result;
    }
}
