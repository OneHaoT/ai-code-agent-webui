package com.aiassist.controller;

import com.aiassist.dto.ConfirmDecisionRequest;
import com.aiassist.exception.AiServiceException;
import com.aiassist.exception.ResourceNotFoundException;
import com.aiassist.service.AiClient;
import com.aiassist.service.ChatService;
import com.aiassist.service.ChatStreamService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * confirm 决策转发端点单测（直接调用方法，不起 MockMvc）。
 * 参数校验抛 IllegalArgumentException（全局映射 400）；
 * ai 404 透传为 ResourceNotFoundException（全局映射 404）；
 * 状态码映射本身已由 GlobalExceptionHandlerTest 覆盖。
 */
class ChatControllerTest {

    private AiClient aiClient;
    private ChatController controller;

    @BeforeEach
    void setUp() {
        aiClient = mock(AiClient.class);
        controller = new ChatController(
                mock(ChatService.class), mock(ChatStreamService.class), aiClient);
    }

    @Test
    void confirm_validRequest_forwardsToAiClient() {
        JsonNode aiResp = new ObjectMapper().createObjectNode()
                .put("success", true).put("confirm_id", "cf1").put("approved", true);
        when(aiClient.confirmExecution("cf1", true)).thenReturn(aiResp);

        JsonNode out = controller.confirm(
                "c1", new ConfirmDecisionRequest("cf1", true));
        assertThat(out).isSameAs(aiResp);
    }

    @Test
    void confirm_blankConfirmId_throwsBadRequest() {
        assertThatThrownBy(() -> controller.confirm(
                "c1", new ConfirmDecisionRequest("  ", true)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("confirmId");
    }

    @Test
    void confirm_nullConfirmId_throwsBadRequest() {
        assertThatThrownBy(() -> controller.confirm(
                "c1", new ConfirmDecisionRequest(null, true)))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void confirm_nullApproved_throwsBadRequest() {
        assertThatThrownBy(() -> controller.confirm(
                "c1", new ConfirmDecisionRequest("cf1", null)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("approved");
    }

    @Test
    void confirm_nullBody_throwsBadRequest() {
        assertThatThrownBy(() -> controller.confirm("c1", null))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void confirm_unknownConfirmId_propagatesNotFound() {
        when(aiClient.confirmExecution(eq("gone"), eq(false)))
                .thenThrow(new ResourceNotFoundException("确认请求不存在或已失效"));

        assertThatThrownBy(() -> controller.confirm(
                "c1", new ConfirmDecisionRequest("gone", false)))
                .isInstanceOf(ResourceNotFoundException.class);
    }

    @Test
    void confirm_aiDown_propagatesAiServiceException() {
        when(aiClient.confirmExecution(eq("cf1"), eq(true)))
                .thenThrow(new AiServiceException("无法连接 AI 模块"));

        assertThatThrownBy(() -> controller.confirm(
                "c1", new ConfirmDecisionRequest("cf1", true)))
                .isInstanceOf(AiServiceException.class);
    }

    // ---------------- 阶段3.5：GET /workspace/list 项目树透传 ----------------

    @Test
    void listWorkspace_forwardsRootAndPathToAiClient() {
        JsonNode aiResp = new ObjectMapper().createObjectNode()
                .put("root", "C:/proj").put("path", "src").put("total", 1)
                .put("truncated", false);
        when(aiClient.listWorkspace("C:/proj", "src")).thenReturn(aiResp);

        JsonNode out = controller.listWorkspace("C:/proj", "src");
        assertThat(out).isSameAs(aiResp);
    }

    @Test
    void listWorkspace_nullParams_forwardsNulls() {
        JsonNode aiResp = new ObjectMapper().createObjectNode().put("total", 0);
        when(aiClient.listWorkspace(null, null)).thenReturn(aiResp);

        // 前端不传 query 时 Spring 注入 null（required=false）
        JsonNode out = controller.listWorkspace(null, null);
        assertThat(out).isSameAs(aiResp);
    }

    @Test
    void listWorkspace_aiRejectedPath_propagatesBadRequest() {
        // ai 侧 400（越界/非法路径）在 AiClient 内转为 IllegalArgumentException，controller 不吞
        when(aiClient.listWorkspace("C:/proj", ".."))
                .thenThrow(new IllegalArgumentException("路径越出工作区边界，访问已拒绝"));

        assertThatThrownBy(() -> controller.listWorkspace("C:/proj", ".."))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("越出工作区");
    }
}
