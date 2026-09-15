package com.aiassist.store;

import java.time.Instant;
import java.util.List;

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
import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.dao.DuplicateKeyException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * ConversationStore 核心写路径单测（纯 Mockito，不需要数据库/Spring 容器）。
 * 重点覆盖 L-1 修复：seq = findMaxSeq+1 撞 UNIQUE(conv_id,seq) 时的事务外重试。
 */
class ConversationStoreTest {

    private static final String CONV_ID = "conv-1";

    private ConversationMapper conversationMapper;
    private MessageMapper messageMapper;
    private MessageImageMapper imageMapper;
    private ConversationStore store;

    /**
     * LambdaUpdateWrapper 解析方法引用依赖 MyBatis-Plus 启动时构建的 TableInfo/lambda 缓存；
     * 纯 Mockito 单测没有 SqlSessionFactory，需手动初始化三个实体的元数据。
     */
    @BeforeAll
    static void initTableMetadata() {
        MapperBuilderAssistant assistant =
                new MapperBuilderAssistant(new MybatisConfiguration(), "");
        TableInfoHelper.initTableInfo(assistant, ConversationEntity.class);
        TableInfoHelper.initTableInfo(assistant, MessageEntity.class);
        TableInfoHelper.initTableInfo(assistant, MessageImageEntity.class);
    }

    @SuppressWarnings("unchecked")
    @BeforeEach
    void setUp() {
        conversationMapper = mock(ConversationMapper.class);
        messageMapper = mock(MessageMapper.class);
        imageMapper = mock(MessageImageMapper.class);
        ObjectProvider<ConversationStore> selfProvider = mock(ObjectProvider.class);
        store = new ConversationStore(conversationMapper, messageMapper, imageMapper,
                selfProvider, new ObjectMapper().findAndRegisterModules());
        // 生产环境中 ObjectProvider 返回的是事务代理；单测里直接回自身，
        // 冲突时 appendMessageInTx 被重复调用，验证的是重试编排逻辑本身
        when(selfProvider.getObject()).thenReturn(store);
    }

    private ConversationEntity convWithTitle(String title) {
        return new ConversationEntity(CONV_ID, title, Instant.now(), Instant.now());
    }

    private ConversationEntity convWithTitleAndWs(String title, String ws) {
        ConversationEntity e = convWithTitle(title);
        e.setWorkspaceRoot(ws);
        return e;
    }

    @Test
    void appendMessage_firstTextMessage_assignsSeqAndAutoTitle() {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle(null));
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);
        doAnswer(inv -> {
            ((MessageEntity) inv.getArgument(0)).setId(101L);
            return 1;
        }).when(messageMapper).insert(any(MessageEntity.class));

        Message saved = store.appendMessage(CONV_ID, new Message("user", "你好世界"));

        ArgumentCaptor<MessageEntity> captor = ArgumentCaptor.forClass(MessageEntity.class);
        verify(messageMapper).insert(captor.capture());
        assertThat(captor.getValue().getSeq()).isEqualTo(1);
        assertThat(captor.getValue().getContent()).isEqualTo("你好世界");
        assertThat(saved.getPersistId()).isEqualTo(101L);
        // 首条消息：自动标题落库（UpdateWrapper.set title）
        verify(conversationMapper).update(isNull(), any(LambdaUpdateWrapper.class));
    }

    @Test
    void appendMessage_imageOnly_setsPlaceholderTitle() {
        ConversationEntity conv = convWithTitle(null);
        when(conversationMapper.selectById(CONV_ID)).thenReturn(conv);
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);

        List<ImageRef> refs = List.of(new ImageRef("img-1", "a.png", "image/png"));
        store.appendMessage(CONV_ID, new Message("user", "  ", refs));

        assertThat(conv.getTitle()).isEqualTo("图片对话");
        verify(imageMapper).insert(any(com.aiassist.persistence.entity.MessageImageEntity.class));
    }

    @Test
    void appendMessage_longTitle_truncatesByCodePoint() {
        ConversationEntity conv = convWithTitle(null);
        when(conversationMapper.selectById(CONV_ID)).thenReturn(conv);
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);

        // 23 个 a + 2 个 emoji = 25 个码点（超过 24）；截断必须停在 emoji 完整码点边界
        String content = "a".repeat(23) + "😀😀";
        store.appendMessage(CONV_ID, new Message("user", content));

        assertThat(conv.getTitle()).isEqualTo("a".repeat(23) + "😀" + "...");
    }

    @Test
    void appendMessage_existingTitle_isNotOverridden() {
        ConversationEntity conv = convWithTitle("手工标题");
        when(conversationMapper.selectById(CONV_ID)).thenReturn(conv);
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(5);

        store.appendMessage(CONV_ID, new Message("user", "新的一条"));

        assertThat(conv.getTitle()).isEqualTo("手工标题");
    }

    @Test
    void appendMessage_seqConflict_retriesInNewAttemptWithRereadMaxSeq() {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("已有标题"));
        // 第一次读到 0（与并发事务撞号），冲突回滚后重读拿到 1
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0, 1);
        // 仅第一次 insert 撞唯一键
        doThrow(new DuplicateKeyException("Duplicate entry '1' for key 'uk_conv_seq'"))
                .doAnswer(inv -> {
                    ((MessageEntity) inv.getArgument(0)).setId(102L);
                    return 1;
                })
                .when(messageMapper).insert(any(MessageEntity.class));

        Message saved = store.appendMessage(CONV_ID, new Message("assistant", "回答"));

        ArgumentCaptor<MessageEntity> captor = ArgumentCaptor.forClass(MessageEntity.class);
        verify(messageMapper, times(2)).insert(captor.capture());
        assertThat(captor.getAllValues()).extracting(MessageEntity::getSeq).containsExactly(1, 2);
        assertThat(saved.getPersistId()).isEqualTo(102L);
    }

    @Test
    void appendMessage_conflictPersists_givesUpAfterThreeAttempts() {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("t"));
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);
        doThrow(new DuplicateKeyException("dup"))
                .when(messageMapper).insert(any(MessageEntity.class));

        assertThatThrownBy(() -> store.appendMessage(CONV_ID, new Message("user", "x")))
                .isInstanceOf(DuplicateKeyException.class);

        verify(messageMapper, times(3)).insert(any(MessageEntity.class));
        // 全部尝试失败：标题/时间不应更新
        verify(conversationMapper, never()).update(isNull(), any(LambdaUpdateWrapper.class));
    }

    @Test
    void appendMessage_missingConversation_throws() {
        when(conversationMapper.selectById("nope")).thenReturn(null);

        assertThatThrownBy(() -> store.appendMessage("nope", new Message("user", "x")))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("对话不存在");
    }

    @Test
    void removeMessage_lastMessage_resetsTitleToNull() {
        Message message = new Message("user", "x");
        message.setPersistId(9L);
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);

        store.removeMessage(CONV_ID, message);

        verify(messageMapper).deleteById(9L);
        verify(conversationMapper).update(isNull(), any(LambdaUpdateWrapper.class));
    }

    @Test
    void rename_missingConversation_returnsNull() {
        when(conversationMapper.selectById("nope")).thenReturn(null);
        assertThat(store.update("nope", "新标题", null)).isNull();
    }

    @Test
    void rename_existingConversation_updatesTitleAndTime() {
        when(conversationMapper.selectById(CONV_ID))
                .thenReturn(convWithTitle("旧标题"));

        var updated = store.update(CONV_ID, "新标题", null);

        assertThat(updated.getTitle()).isEqualTo("新标题");
        verify(conversationMapper).update(isNull(), any(LambdaUpdateWrapper.class));
    }

    @Test
    void update_workspaceRoot_clearsWhenEmptyString() {
        when(conversationMapper.selectById(CONV_ID))
                .thenReturn(convWithTitleAndWs("旧标题", "C:/tmp/proj"));

        var updated = store.update(CONV_ID, null, "");

        assertThat(updated.getWorkspaceRoot()).isNull();
    }

    @Test
    void update_workspaceRoot_setsWhenProvided() {
        when(conversationMapper.selectById(CONV_ID))
                .thenReturn(convWithTitle("旧标题"));

        var updated = store.update(CONV_ID, null, "D:/work/myproj");

        assertThat(updated.getWorkspaceRoot()).isEqualTo("D:/work/myproj");
    }

    // ---------- 阶段1：工具轨迹持久化 ----------

    @Test
    void appendMessage_toolTrace_serializedToJsonWithChineseAndArgs() throws Exception {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("t"));
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);
        ObjectMapper mapper = new ObjectMapper();
        JsonNode args = mapper.createObjectNode().put("path", "文件.txt");
        ToolStep step = ToolStep.started(1, "c1", "read_file", args, null)
                .finished("success", "1 | 内容", false, 7L);
        Message ai = new Message("assistant", "答案");
        ai.setToolTrace(List.of(step));

        Message saved = store.appendMessage(CONV_ID, ai);

        ArgumentCaptor<MessageEntity> captor = ArgumentCaptor.forClass(MessageEntity.class);
        verify(messageMapper).insert(captor.capture());
        String json = captor.getValue().getToolTrace();
        assertThat(json).isNotBlank();
        // 中文原样落盘（未转义）、关键字段齐全
        JsonNode root = mapper.readTree(json);
        assertThat(root).hasSize(1);
        assertThat(root.get(0).path("id").asText()).isEqualTo("c1");
        assertThat(root.get(0).path("name").asText()).isEqualTo("read_file");
        assertThat(root.get(0).path("status").asText()).isEqualTo("success");
        assertThat(root.get(0).path("output").asText()).isEqualTo("1 | 内容");
        assertThat(root.get(0).path("args").path("path").asText()).isEqualTo("文件.txt");
        assertThat(root.get(0).path("seq").asInt()).isEqualTo(1);
        assertThat(root.get(0).path("durationMs").asLong()).isEqualTo(7L);
        // NON_NULL：rawArgs 为 null 时不输出
        assertThat(root.get(0).has("rawArgs")).isFalse();
        // 返回给 done 帧的内存消息保留轨迹
        assertThat(saved.getToolTrace()).hasSize(1);
        assertThat(saved.getToolTrace().get(0).id()).isEqualTo("c1");
    }

    @Test
    void appendMessage_nullOrEmptyTrace_writesNullColumn() {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("t"));
        when(messageMapper.findMaxSeq(CONV_ID)).thenReturn(0);

        store.appendMessage(CONV_ID, new Message("assistant", "答案"));

        ArgumentCaptor<MessageEntity> captor = ArgumentCaptor.forClass(MessageEntity.class);
        verify(messageMapper).insert(captor.capture());
        assertThat(captor.getValue().getToolTrace()).isNull();
    }

    @Test
    void get_toolTrace_restoredFromJson() {
        String json = "[{\"id\":\"g1\",\"name\":\"glob\",\"args\":{\"pattern\":\"*.java\"},"
                + "\"status\":\"success\",\"output\":\"a.java\",\"truncated\":false,"
                + "\"seq\":1,\"durationMs\":3}]";
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("t"));
        when(messageMapper.selectList(any())).thenReturn(List.of(messageEntityWithTrace(json)));

        Conversation conv = store.get(CONV_ID);

        Message msg = conv.getMessages().get(0);
        assertThat(msg.getToolTrace()).hasSize(1);
        ToolStep step = msg.getToolTrace().get(0);
        assertThat(step.id()).isEqualTo("g1");
        assertThat(step.name()).isEqualTo("glob");
        assertThat(step.status()).isEqualTo("success");
        assertThat(step.args().path("pattern").asText()).isEqualTo("*.java");
        assertThat(step.seq()).isEqualTo(1);
        assertThat(step.durationMs()).isEqualTo(3L);
        assertThat(step.truncated()).isFalse();
        // 还原后做不可变拷贝
        assertThatThrownBy(() -> msg.getToolTrace().add(step))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void get_corruptedToolTraceJson_toleratesNullAndStillLoads() {
        when(conversationMapper.selectById(CONV_ID)).thenReturn(convWithTitle("t"));
        when(messageMapper.selectList(any()))
                .thenReturn(List.of(messageEntityWithTrace("broken-json{")));

        Conversation conv = store.get(CONV_ID);

        Message msg = conv.getMessages().get(0);
        assertThat(msg.getContent()).isEqualTo("答");
        assertThat(msg.getToolTrace()).isNull();
    }

    private MessageEntity messageEntityWithTrace(String toolTraceJson) {
        MessageEntity me = new MessageEntity();
        me.setId(9L);
        me.setConversationId(CONV_ID);
        me.setSeq(1);
        me.setRole("assistant");
        me.setContent("答");
        me.setToolTrace(toolTraceJson);
        me.setCreatedAt(Instant.now());
        return me;
    }
}
