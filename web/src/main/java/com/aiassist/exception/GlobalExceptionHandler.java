package com.aiassist.exception;

import java.time.Instant;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import java.util.concurrent.RejectedExecutionException;

/**
 * 全局异常处理：统一错误响应结构，避免堆栈直接暴露给前端。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    /** Bean Validation 校验失败（如消息为空）→ 400，取第一条字段错误信息 */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, Object>> handleValidation(MethodArgumentNotValidException e) {
        String message = e.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(fe -> fe.getDefaultMessage())
                .orElse("请求参数校验失败");
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body(false, message));
    }

    /** 请求体缺失/JSON 格式错误（如 PATCH 重命名不带 body）→ 400，而非兜底 500 */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, Object>> handleUnreadable(HttpMessageNotReadableException e) {
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body(false, "请求体格式错误或为空"));
    }

    /** SSE 工作线程池/队列已满：服务暂时不可用 → 503（用户消息已先回滚） */
    @ExceptionHandler(RejectedExecutionException.class)
    public ResponseEntity<Map<String, Object>> handleRejected(RejectedExecutionException e) {
        log.warn("流式线程池已满，拒绝新请求: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(body(false, "服务器繁忙，请稍后重试"));
    }

    /**
     * 唯一约束冲突（如同会话消息 seq 并发撞号且重试 3 次仍失败）→ 409。
     * 正常重试机制已消化竞态，落到这里说明存在持续并发竞争，提示用户稍后重试即可。
     */
    @ExceptionHandler(DuplicateKeyException.class)
    public ResponseEntity<Map<String, Object>> handleDuplicateKey(DuplicateKeyException e) {
        log.warn("唯一约束冲突，重试后仍失败: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.CONFLICT)
                .body(body(false, "操作冲突，请稍后重试"));
    }

    /** 业务参数非法（对话不存在、消息为空等）→ 400 */
    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, Object>> handleIllegalArgument(IllegalArgumentException e) {
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body(false, e.getMessage()));
    }

    /** 资源不存在（如图片已删除）→ 404 */
    @ExceptionHandler(ResourceNotFoundException.class)
    public ResponseEntity<Map<String, Object>> handleNotFound(ResourceNotFoundException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(body(false, e.getMessage()));
    }

    /**
     * 路径无任何映射（含已下线的旧端点，如 POST /chat）：Boot 3.2 会尝试当静态资源
     * 解析并抛此异常，必须映射为 404，否则落入兜底变成误导性的 500
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<Map<String, Object>> handleNoResource(NoResourceFoundException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(body(false, "接口或资源不存在"));
    }

    /** 上传文件超出 multipart 大小限制 → 400 */
    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<Map<String, Object>> handleUploadSize(MaxUploadSizeExceededException e) {
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(body(false, "图片大小不能超过 10MB"));
    }

    /** AI 模块调用失败 → 502 */
    @ExceptionHandler(AiServiceException.class)
    public ResponseEntity<Map<String, Object>> handleAiService(AiServiceException e) {
        log.error("调用 AI 模块失败: {}", e.getMessage(), e);
        return ResponseEntity.status(HttpStatus.BAD_GATEWAY).body(body(false, e.getMessage()));
    }

    /** 兜底：其他未预期异常 → 500 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, Object>> handleOther(Exception e) {
        log.error("服务内部异常: {}", e.getMessage(), e);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(body(false, "服务内部错误，请稍后重试"));
    }

    private Map<String, Object> body(boolean success, String error) {
        return Map.of(
                "success", success,
                "error", error == null ? "unknown" : error,
                "timestamp", Instant.now().toString()
        );
    }
}
