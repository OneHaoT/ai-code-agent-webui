package com.aiassist.service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

import com.aiassist.client.dto.AiHistoryMessage;
import com.aiassist.config.AiServiceProperties;
import com.aiassist.exception.ResourceNotFoundException;
import com.aiassist.model.Conversation;
import com.aiassist.model.ImageRef;
import com.aiassist.model.Message;
import com.aiassist.model.ToolStep;
import com.aiassist.service.ChatService.PreparedChat;
import com.aiassist.storage.StoredImage;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.aiassist.storage.StorageService;
import com.aiassist.store.ConversationStore;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * ChatService 单测：prepare/complete/rollback 三段式编排、历史图片窗口、
 * 坏图跳过、回滚 best-effort、重命名校验。阶段1重构 ChatService 时的回归护栏。
 */
class ChatServiceTest {

    private ConversationStore store;
    private StorageService storage;
    private ChatService chatService;

    private static ImageRef ref(String id) {
        return new ImageRef(id, id + ".png", "image/png");
    }

    private static StoredImage stored(String id) {
        return new StoredImage(id, id + ".png", "image/png", 3, new byte[]{1, 2, 3});
    }

    @BeforeEach
    void setUp() {
        store = mock(ConversationStore.class);
        storage = mock(StorageService.class);
        AiServiceProperties props =
                new AiServiceProperties("http://ai", Duration.ofSeconds(5), Duration.ofSeconds(120), 10);
        chatService = new ChatService(store, storage, props);
    }

    private Conversation conversationWith(Message... history) {
        Conversation conv = new Conversation("c1", "已有标题");
        conv.setMessages(new ArrayList<>(List.of(history)));
        return conv;
    }

    @Test
    void prepare_blankContentWithoutImages_rejected() {
        assertThatThrownBy(() -> chatService.prepare("c1", "   ", List.of(), false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("消息不能为空");
        verify(store, never()).appendMessage(anyString(), any());
    }

    @Test
    void prepare_moreThanFourImages_rejected() {
        List<ImageRef> five = List.of(ref("a"), ref("b"), ref("c"), ref("d"), ref("e"));
        assertThatThrownBy(() -> chatService.prepare("c1", "x", five, false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("最多上传 4 张");
        verify(store, never()).appendMessage(anyString(), any());
    }

    @Test
    void prepare_missingConversation_rejected() {
        when(store.get("ghost")).thenReturn(null);
        assertThatThrownBy(() -> chatService.prepare("ghost", "x", List.of(), false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("对话不存在");
    }

    @Test
    void prepare_happyPath_persistsUserMessageAndEncodesImages() {
        ImageRef historyRef = ref("h-img");
        when(store.get("c1")).thenReturn(conversationWith(new Message("user", "历史", List.of(historyRef))));
        when(storage.load("h-img")).thenReturn(stored("h-img"));
        ImageRef currentRef = ref("c-img");
        when(storage.load("c-img")).thenReturn(stored("c-img"));

        Message persisted = new Message("user", "hi", List.of(currentRef));
        persisted.setPersistId(7L);
        when(store.appendMessage(eq("c1"), any())).thenReturn(persisted);

        PreparedChat p = chatService.prepare("c1", "  hi  ", List.of(currentRef), true);

        assertThat(p.conversationId()).isEqualTo("c1");
        assertThat(p.content()).isEqualTo("hi");
        assertThat(p.thinking()).isTrue();
        assertThat(p.userMessage().getPersistId()).isEqualTo(7L);
        assertThat(p.history()).hasSize(1);
        assertThat(p.history().get(0).images()).hasSize(1)
                .first().asString().startsWith("data:image/png;base64,");
        assertThat(p.currentImages()).hasSize(1)
                .first().asString().startsWith("data:image/png;base64,");
    }

    @Test
    void prepare_oldImagesOutsideWindow_keepCountOnly() {
        AiServiceProperties window2 =
                new AiServiceProperties("http://ai", Duration.ofSeconds(5), Duration.ofSeconds(120), 2);
        chatService = new ChatService(store, storage, window2);

        List<Message> history = new ArrayList<>();
        for (int i = 0; i < 4; i++) {
            history.add(new Message("user", "m" + i, List.of(ref("img-" + i))));
            when(storage.load("img-" + i)).thenReturn(stored("img-" + i));
        }
        when(store.get("c1")).thenReturn(conversationWith(history.toArray(new Message[0])));

        PreparedChat p = chatService.prepare("c1", "new", List.of(), false);

        assertThat(p.history()).extracting(AiHistoryMessage::imageCount)
                .containsExactly(1, 1, 1, 1);
        assertThat(p.history().get(0).images()).isEmpty();
        assertThat(p.history().get(1).images()).isEmpty();
        assertThat(p.history().get(2).images()).hasSize(1);
        assertThat(p.history().get(3).images()).hasSize(1);
        verify(storage, never()).load("img-0");
        verify(storage, never()).load("img-1");
        verify(storage).load("img-2");
        verify(storage).load("img-3");
    }

    @Test
    void prepare_missingImageFile_isSkippedNotFatal() {
        when(store.get("c1")).thenReturn(conversationWith());
        when(storage.load("lost")).thenThrow(new ResourceNotFoundException("文件已丢失"));

        PreparedChat p = chatService.prepare("c1", "看图", List.of(ref("lost")), false);

        assertThat(p.currentImages()).isEmpty();
        // 用户消息仍正常落库（图引用保留，字节缺失不阻断对话）
        verify(store).appendMessage(eq("c1"), any());
    }

    @Test
    void complete_savesAssistantMessageWithReasoning() {
        PreparedChat p = new PreparedChat("c1", "hi", List.of(),
                new Message("user", "hi"), List.of(), List.of(), true, null);

        chatService.complete(p, "答案正文", "思维链内容", null);

        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(store).appendMessage(eq("c1"), captor.capture());
        assertThat(captor.getValue().getRole()).isEqualTo("assistant");
        assertThat(captor.getValue().getContent()).isEqualTo("答案正文");
        assertThat(captor.getValue().getReasoning()).isEqualTo("思维链内容");
    }

    @Test
    void complete_blankReasoning_notPersisted() {
        PreparedChat p = new PreparedChat("c1", "hi", List.of(),
                new Message("user", "hi"), List.of(), List.of(), false, null);

        chatService.complete(p, "答案", "   ", null);

        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(store).appendMessage(eq("c1"), captor.capture());
        assertThat(captor.getValue().getReasoning()).isNull();
    }

    @Test
    void complete_toolTrace_setOnMessageAndSerializes() throws Exception {
        PreparedChat p = new PreparedChat("c1", "hi", List.of(),
                new Message("user", "hi"), List.of(), List.of(), false, null);
        ObjectMapper mapper = new ObjectMapper();
        JsonNode args = mapper.createObjectNode().put("path", "a.txt");
        ToolStep step = ToolStep.started(1, "t1", "read_file", args, null)
                .finished("success", "1 | 内容", false, 12L);

        chatService.complete(p, "答案", null, List.of(step));

        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(store).appendMessage(eq("c1"), captor.capture());
        Message saved = captor.getValue();
        assertThat(saved.getToolTrace()).hasSize(1);
        assertThat(saved.getToolTrace().get(0).id()).isEqualTo("t1");
        assertThat(saved.getToolTrace().get(0).status()).isEqualTo("success");
        // 落库前做防御性不可变拷贝
        assertThatThrownBy(() -> saved.getToolTrace().add(step))
                .isInstanceOf(UnsupportedOperationException.class);
        // JSON 输出含轨迹字段与中文内容
        ObjectMapper out = new ObjectMapper().findAndRegisterModules();
        String json = out.writeValueAsString(saved);
        assertThat(json).contains("toolTrace", "read_file", "答案", "a.txt");
    }

    @Test
    void complete_nullOrEmptyTrace_jsonOmitsToolTraceField() throws Exception {
        PreparedChat p = new PreparedChat("c1", "hi", List.of(),
                new Message("user", "hi"), List.of(), List.of(), false, null);
        ObjectMapper out = new ObjectMapper().findAndRegisterModules();

        chatService.complete(p, "答案", null, null);
        Message withNull = captureLast();
        assertThat(withNull.getToolTrace()).isNull();
        assertThat(out.writeValueAsString(withNull)).doesNotContain("toolTrace");

        chatService.complete(p, "答案2", null, List.of());
        Message withEmpty = captureLast();
        assertThat(withEmpty.getToolTrace()).isNull();
        assertThat(out.writeValueAsString(withEmpty)).doesNotContain("toolTrace");
    }

    @Test
    void historyMessage_neverCarriesToolTraceFields() throws Exception {
        AiHistoryMessage dto = new AiHistoryMessage("assistant", "用过工具", List.of(), 0);
        String json = new ObjectMapper().writeValueAsString(dto);
        assertThat(json).doesNotContain("toolTrace").doesNotContain("tool_trace");
    }

    private Message captureLast() {
        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(store, org.mockito.Mockito.atLeastOnce()).appendMessage(eq("c1"), captor.capture());
        return captor.getValue();
    }

    @Test
    void rollback_removesMessageAndDeletesRefs_storageFailureSwallowed() {
        ImageRef ref = ref("to-delete");
        PreparedChat p = new PreparedChat("c1", "hi", List.of(ref),
                new Message("user", "hi", List.of(ref)), List.of(), List.of(), false, null);
        doThrow(new RuntimeException("磁盘只读")).when(storage).delete("to-delete");

        assertThatCode(() -> chatService.rollback(p)).doesNotThrowAnyException();

        verify(store).removeMessage(eq("c1"), any());
        verify(storage).delete("to-delete");
    }

    @Test
    void rename_invalidTitles_rejected() {
        assertThatThrownBy(() -> chatService.updateConversation("c1", "  ", null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> chatService.updateConversation("c1", "x".repeat(51), null))
                .isInstanceOf(IllegalArgumentException.class);
        verify(store, never()).update(anyString(), any(), any());
    }

    @Test
    void rename_missingConversation_is404Semantic() {
        when(store.update(eq("ghost"), any(), any())).thenReturn(null);
        assertThatThrownBy(() -> chatService.updateConversation("ghost", "新标题", null))
                .isInstanceOf(ResourceNotFoundException.class);
    }

    @Test
    void deleteConversation_bestEffortImageCleanupStillDeletes() {
        Message withImage = new Message("user", "x", List.of(ref("img-1")));
        when(store.get("c1")).thenReturn(conversationWith(withImage));
        doThrow(new RuntimeException("磁盘故障")).when(storage).delete("img-1");

        assertThatCode(() -> chatService.deleteConversation("c1")).doesNotThrowAnyException();

        verify(storage).delete("img-1");
        verify(store).delete("c1");
    }
}
