package com.aiassist.dto;

/**
 * 图片上传成功响应。
 *
 * @param id          图片 ID（后续发消息、显示图片都用它）
 * @param filename    原始文件名
 * @param contentType MIME 类型
 * @param size        字节数
 * @param url         可直接访问的相对地址（经 Vite 代理同源访问）
 */
public record UploadImageResponse(
        String id,
        String filename,
        String contentType,
        long size,
        String url
) {

    public static UploadImageResponse of(String id, String filename, String contentType, long size) {
        return new UploadImageResponse(
                id, filename, contentType, size, "/api/images/" + id);
    }
}
