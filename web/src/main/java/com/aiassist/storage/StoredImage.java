package com.aiassist.storage;

/**
 * 存储层读出的图片（含字节内容，用于 HTTP 回显或转 data URL 发给 AI 模块）。
 *
 * @param id              存储 ID（本地实现即文件名，未来 OSS 可为对象 key）
 * @param originalFilename 用户上传时的原始文件名
 * @param contentType     MIME 类型，如 image/png
 * @param size            字节数
 * @param data            图片二进制内容
 */
public record StoredImage(
        String id,
        String originalFilename,
        String contentType,
        long size,
        byte[] data
) {
}
