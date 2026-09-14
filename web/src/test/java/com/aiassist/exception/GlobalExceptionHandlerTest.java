package com.aiassist.exception;

import java.util.concurrent.RejectedExecutionException;

import org.junit.jupiter.api.Test;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.http.converter.HttpMessageNotReadableException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 全局异常映射状态码单测（直接调用 handler 方法，不起 MockMvc）。
 * 状态码语义是前后端契约的一部分（400/404/409/502/503/500），防止后续改动落错兜底。
 */
class GlobalExceptionHandlerTest {

    private final GlobalExceptionHandler handler = new GlobalExceptionHandler();

    @Test
    void duplicateKey_conflict() {
        assertThat(handler.handleDuplicateKey(new DuplicateKeyException("dup")).getStatusCode())
                .isEqualTo(HttpStatus.CONFLICT);
    }

    @Test
    void rejectedExecution_serviceUnavailable() {
        assertThat(handler.handleRejected(new RejectedExecutionException("full")).getStatusCode())
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    }

    @Test
    void unreadableBody_badRequest() {
        assertThat(handler.handleUnreadable(new HttpMessageNotReadableException("bad json")).getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
    }

    @Test
    void illegalArgument_badRequest() {
        assertThat(handler.handleIllegalArgument(new IllegalArgumentException("x")).getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
    }

    @Test
    void resourceNotFound_notFound() {
        assertThat(handler.handleNotFound(new ResourceNotFoundException("x")).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
    }

    @Test
    void aiServiceError_badGateway() {
        assertThat(handler.handleAiService(new AiServiceException("down")).getStatusCode())
                .isEqualTo(HttpStatus.BAD_GATEWAY);
    }

    @Test
    void unexpectedError_internalServerError() {
        assertThat(handler.handleOther(new RuntimeException("boom")).getStatusCode())
                .isEqualTo(HttpStatus.INTERNAL_SERVER_ERROR);
    }
}
