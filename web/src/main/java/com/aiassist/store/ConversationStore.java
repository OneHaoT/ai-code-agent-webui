package com.aiassist.store;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import com.aiassist.model.Conversation;
import com.aiassist.model.ImageRef;
import com.aiassist.model.Message;
import com.aiassist.model.ToolStep;
import com.aiassist.persistence.entity.ConversationEntity;
import com.aiassist.persistence.entity.MessageEntity;
import com.aiassist.persistence.entity.MessageImageEntity;
import com.aiassist.persistence.mapper.ConversationMapper;
import com.aiassist.persistence.mapper.MessageImageMapper;
import com.aiassist.persistence.mapper.MessageMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * 对话仓储（MyBatis-Plus/MySQL 实现）。
 *
 * 对外仍返回 model 包下的 Conversation/Message（前端 JSON 契约不变）；
 * 写操作各自独立事务，避免把最长 180s 的 AI HTTP 调用包进长事务持有连接。
 * 查询均为标准 SQL（Wrapper/@Select），切换其他数据库时本类无需改动。
 */
@Component
public class ConversationStore {

    private static final Logger log = LoggerFactory.getLogger(ConversationStore.class);

    /**
     * seq 唯一键冲突时的总尝试次数（含首次）。
     * 撞号只在同会话高并发下出现，正常 2 次即成功，3 次足以覆盖极端竞争。
     */
    private static final int MAX_SEQ_ATTEMPTS = 3;

    private final ConversationMapper conversationMapper;
    private final MessageMapper messageMapper;
    private final MessageImageMapper imageMapper;
    /**
     * 工具轨迹 List<ToolStep> ↔ JSON 字符串的映射器（阶段1）。
     * 选型：映射层手工序列化而非 MyBatis-Plus JacksonTypeHandler——
     * 1) ToolStep 是含 JsonNode 的 record，TypeHandler 内部自持的 ObjectMapper
     *    与容器配置不一致时有反序列化风险；
     * 2) 库中 JSON 损坏时需要降级为 null + warn 而不是让整条历史加载失败，
     *    在映射层捕获最直接。
     */
    private final ObjectMapper objectMapper;

    /**
     * 自身 Bean 的延迟引用：seq 冲突重试必须重新进入事务边界（冲突事务已被标记
     * rollback-only，同一事务内重试必然失败），因此通过代理调用 appendMessageInTx，
     * 每次尝试都在一个全新事务中执行。ObjectProvider 本身即延迟解析，不会形成循环依赖。
     */
    private final ObjectProvider<ConversationStore> selfProvider;

    public ConversationStore(ConversationMapper conversationMapper,
                             MessageMapper messageMapper,
                             MessageImageMapper imageMapper,
                             ObjectProvider<ConversationStore> selfProvider,
                             ObjectMapper objectMapper) {
        this.conversationMapper = conversationMapper;
        this.messageMapper = messageMapper;
        this.imageMapper = imageMapper;
        this.selfProvider = selfProvider;
        this.objectMapper = objectMapper;
    }

    // ---------- 对话 ----------

    @Transactional
    public Conversation create(String title) {
        Instant now = Instant.now();
        String id = UUID.randomUUID().toString().replace("-", "");
        ConversationEntity entity = new ConversationEntity(
                id, title == null || title.isBlank() ? null : title, now, now);
        conversationMapper.insert(entity);
        return new Conversation(id, entity.getTitle(), now, now);
    }

    /** 完整聚合：含全部消息（按 seq）与图片引用 */
    @Transactional(readOnly = true)
    public Conversation get(String id) {
        ConversationEntity entity = conversationMapper.selectById(id);
        if (entity == null) {
            return null;
        }

        // 图片一次查出后按消息分组（避免逐消息 N+1）
        List<MessageImageEntity> allImages = imageMapper.findByConversationId(id);
        Map<Long, List<MessageImageEntity>> imagesByMessage = new LinkedHashMap<>();
        for (MessageImageEntity img : allImages) {
            imagesByMessage.computeIfAbsent(img.getMessageId(), k -> new ArrayList<>()).add(img);
        }

        List<MessageEntity> messages = messageMapper.selectList(
                new LambdaQueryWrapper<MessageEntity>()
                        .eq(MessageEntity::getConversationId, id)
                        .orderByAsc(MessageEntity::getSeq));

        Conversation conv = new Conversation(
                entity.getId(), entity.getTitle(), entity.getCreatedAt(), entity.getUpdatedAt());
        List<Message> result = new ArrayList<>();
        for (MessageEntity me : messages) {
            List<ImageRef> refs = imagesByMessage
                    .getOrDefault(me.getId(), List.of())
                    .stream()
                    .sorted(Comparator.comparingInt(MessageImageEntity::getSeq))
                    .map(i -> new ImageRef(i.getImageId(), i.getFilename(), i.getContentType()))
                    .toList();
            Message dm = new Message(
                    me.getRole(), me.getContent(), refs.isEmpty() ? null : refs);
            dm.setReasoning(me.getReasoning());
            dm.setToolTrace(readToolTrace(me.getId(), me.getToolTrace()));
            dm.setCreatedAt(me.getCreatedAt());
            dm.setPersistId(me.getId());
            result.add(dm);
        }
        conv.setMessages(result);
        return conv;
    }

    @Transactional(readOnly = true)
    public boolean exists(String id) {
        return conversationMapper.selectCount(
                new LambdaQueryWrapper<ConversationEntity>()
                        .eq(ConversationEntity::getId, id)) > 0;
    }

    /** 列表：不装载消息（侧栏只需要标题/时间） */
    @Transactional(readOnly = true)
    public List<Conversation> list() {
        return conversationMapper.selectList(
                        new LambdaQueryWrapper<ConversationEntity>()
                                .orderByDesc(ConversationEntity::getUpdatedAt))
                .stream()
                .map(c -> new Conversation(c.getId(), c.getTitle(), c.getCreatedAt(), c.getUpdatedAt()))
                .toList();
    }

    @Transactional
    public void delete(String id) {
        // 数据库外键 ON DELETE CASCADE 级联清理 message / message_image
        conversationMapper.deleteById(id);
    }

    /**
     * 手动重命名：立即落库并刷新 updatedAt，返回更新后的对话元数据（不含消息）。
     * 对话不存在返回 null（由 Service 层转成参数异常）。
     */
    @Transactional
    public Conversation rename(String id, String title) {
        ConversationEntity entity = conversationMapper.selectById(id);
        if (entity == null) {
            return null;
        }
        Instant now = Instant.now();
        conversationMapper.update(null, new LambdaUpdateWrapper<ConversationEntity>()
                .eq(ConversationEntity::getId, id)
                .set(ConversationEntity::getTitle, title)
                .set(ConversationEntity::getUpdatedAt, now));
        return new Conversation(id, title, entity.getCreatedAt(), now);
    }

    // ---------- 消息 ----------

    /**
     * 追加消息（带 seq 冲突重试）：生成会话内序号、落库、刷新对话时间；
     * 首条用户消息自动生成标题（纯图片消息用占位标题）。
     *
     * seq = findMaxSeq+1 的"读后写"不是原子操作：同会话两个并发事务可能读到同一
     * maxSeq，后提交者被 DB 唯一约束 UNIQUE(conv_id,seq) 拦截（由 MyBatis/Spring
     * 翻译为 DuplicateKeyException，绝不会产生重号脏数据）。捕获后整个事务回滚
     * （图片插入、标题更新一并撤销），在新事务里重读 maxSeq 再试。
     */
    public Message appendMessage(String conversationId, Message message) {
        DuplicateKeyException lastError = null;
        for (int attempt = 1; attempt <= MAX_SEQ_ATTEMPTS; attempt++) {
            try {
                return selfProvider.getObject().appendMessageInTx(conversationId, message);
            } catch (DuplicateKeyException e) {
                lastError = e;
                log.warn("消息 seq 冲突，准备重试 conversationId={} attempt={}/{}",
                        conversationId, attempt, MAX_SEQ_ATTEMPTS);
            }
        }
        throw lastError;
    }

    /** 单次事务尝试（仅由 {@link #appendMessage} 经代理调用，勿直接调用） */
    @Transactional
    public Message appendMessageInTx(String conversationId, Message message) {
        ConversationEntity conv = conversationMapper.selectById(conversationId);
        if (conv == null) {
            throw new IllegalArgumentException("对话不存在: " + conversationId);
        }

        int seq = messageMapper.findMaxSeq(conversationId) + 1;
        Instant now = message.getCreatedAt() != null ? message.getCreatedAt() : Instant.now();

        MessageEntity me = new MessageEntity();
        me.setConversationId(conversationId);
        me.setSeq(seq);
        me.setRole(message.getRole());
        me.setContent(nullIfBlank(message.getContent()));
        me.setReasoning(nullIfBlank(message.getReasoning()));
        me.setToolTrace(writeToolTrace(message.getToolTrace()));
        me.setCreatedAt(now);
        messageMapper.insert(me); // 自增 id 回填 me.id

        List<ImageRef> refs = message.getImages() == null ? List.of() : message.getImages();
        int imgSeq = 0;
        for (ImageRef ref : refs) {
            imageMapper.insert(new MessageImageEntity(
                    me.getId(), ++imgSeq, ref.id(), ref.filename(), ref.contentType()));
        }

        // 标题只在首条消息时确定（与历史内存行为一致）
        String newTitle = null;
        if (conv.getTitle() == null || conv.getTitle().isBlank()) {
            String content = message.getContent();
            if (content != null && !content.isBlank()) {
                newTitle = truncateTitle(content, 24);
            } else if (!refs.isEmpty()) {
                newTitle = "图片对话";
            }
        }
        updateConversation(conv, newTitle, now);

        Message saved = new Message(message.getRole(), message.getContent(),
                refs.isEmpty() ? null : refs);
        saved.setReasoning(me.getReasoning());
        saved.setToolTrace(message.getToolTrace());
        saved.setCreatedAt(now);
        saved.setPersistId(me.getId());
        return saved;
    }

    /**
     * 移除一条消息（AI 调用失败回滚用户消息）。
     * 若移除后对话已无消息，标题归位为 null，保持与旧内存实现一致。
     */
    @Transactional
    public void removeMessage(String conversationId, Message message) {
        if (message.getPersistId() == null) {
            return;
        }
        messageMapper.deleteById(message.getPersistId());

        if (messageMapper.findMaxSeq(conversationId) == 0) {
            conversationMapper.update(null, new LambdaUpdateWrapper<ConversationEntity>()
                    .eq(ConversationEntity::getId, conversationId)
                    .set(ConversationEntity::getTitle, null)
                    .set(ConversationEntity::getUpdatedAt, Instant.now()));
        }
    }

    /** GC 用：全库仍被消息引用的图片文件 ID */
    @Transactional(readOnly = true)
    public Set<String> findAllReferencedImageIds() {
        return Set.copyOf(imageMapper.findAllReferencedImageIds());
    }

    /** 图片文件是否仍被某条消息引用（删除图片接口的保护校验用） */
    @Transactional(readOnly = true)
    public boolean isImageReferenced(String imageId) {
        return imageMapper.selectCount(
                new LambdaQueryWrapper<MessageImageEntity>()
                        .eq(MessageImageEntity::getImageId, imageId)) > 0;
    }

    /**
     * 刷新对话的标题与更新时间。
     * 用 UpdateWrapper 显式 set：MP 的 updateById 默认忽略 null 字段，
     * 而标题归位为 null（removeMessage）必须能写 null。
     */
    private void updateConversation(ConversationEntity conv, String newTitle, Instant updatedAt) {
        LambdaUpdateWrapper<ConversationEntity> uw = new LambdaUpdateWrapper<ConversationEntity>()
                .eq(ConversationEntity::getId, conv.getId())
                .set(ConversationEntity::getUpdatedAt, updatedAt);
        if (newTitle != null) {
            uw.set(ConversationEntity::getTitle, newTitle);
            conv.setTitle(newTitle);
        }
        conversationMapper.update(null, uw);
        conv.setUpdatedAt(updatedAt);
    }

    private static String nullIfBlank(String s) {
        return s == null || s.isBlank() ? null : s;
    }

    /** 轨迹 -> JSON 字符串：null/空列表写 NULL；序列化异常防御性降级为 NULL + warn。 */
    private String writeToolTrace(List<ToolStep> trace) {
        if (trace == null || trace.isEmpty()) {
            return null;
        }
        try {
            return objectMapper.writeValueAsString(trace);
        } catch (JsonProcessingException e) {
            log.warn("工具轨迹序列化失败，降级为 NULL 入库: {}", e.getMessage());
            return null;
        }
    }

    /**
     * JSON 字符串 -> 轨迹：NULL/空白返回 null；
     * 库中 JSON 损坏（手工改库/历史脏数据）只 warn 并降级为 null，
     * 绝不阻断整条会话历史加载。
     */
    private List<ToolStep> readToolTrace(Long messageId, String json) {
        if (json == null || json.isBlank()) {
            return null;
        }
        try {
            List<ToolStep> trace = objectMapper.readValue(
                    json, new TypeReference<List<ToolStep>>() {});
            return trace.isEmpty() ? null : List.copyOf(trace);
        } catch (Exception e) {
            log.warn("消息 {} 的工具轨迹 JSON 损坏，已忽略: {}", messageId, e.getMessage());
            return null;
        }
    }

    /**
     * 按 Unicode 码点数截断标题，避免在 emoji/增补字符的 UTF-16 代理对中间
     * 切断产生替换乱码（如 "😀".length()==2，按 char 截一半会坏）。
     */
    private static String truncateTitle(String content, int maxCodePoints) {
        if (content.codePointCount(0, content.length()) <= maxCodePoints) {
            return content;
        }
        int end = content.offsetByCodePoints(0, maxCodePoints);
        return content.substring(0, end) + "...";
    }
}
