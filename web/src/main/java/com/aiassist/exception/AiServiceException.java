package com.aiassist.exception;

/**
 * 调用 AI 模块失败时抛出（网络异常 / 非 2xx 响应 / 响应解析失败）。
 * 由 GlobalExceptionHandler 统一转为 502 响应。
 */
public class AiServiceException extends RuntimeException {

    public AiServiceException(String message) {
        super(message);
    }

    public AiServiceException(String message, Throwable cause) {
        super(message, cause);
    }
}
