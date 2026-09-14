package com.aiassist.exception;

/**
 * 请求的资源不存在（如图片已被删除），由全局异常处理映射为 404。
 */
public class ResourceNotFoundException extends RuntimeException {

    public ResourceNotFoundException(String message) {
        super(message);
    }
}
