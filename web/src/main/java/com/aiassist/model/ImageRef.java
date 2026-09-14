package com.aiassist.model;

/**
 * 消息中引用的图片（只存引用，不存字节）。
 * 字节由 StorageService 管理：本地磁盘或未来的 OSS。
 *
 * @param id          存储 ID（本地实现即文件名）
 * @param filename    原始文件名（展示用）
 * @param contentType MIME 类型
 */
public record ImageRef(String id, String filename, String contentType) {
}
