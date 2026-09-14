package com.aiassist.storage;

import java.time.Duration;
import java.util.List;
import java.util.Set;

/**
 * 图片存储抽象。
 *
 * 当前实现：{@link LocalStorageService}（本地磁盘）。
 * 未来扩展：新增 OssStorageService（阿里云 OSS / MinIO / S3），
 * 通过 {@code app.storage.type=oss} 切换，业务层代码不变。
 */
public interface StorageService {

    /**
     * 保存图片。
     *
     * @param originalFilename 原始文件名（用于推断扩展名）
     * @param contentType      MIME 类型
     * @param data             图片字节
     * @return 含存储 ID 的图片信息（不包含字节内容的写入回显）
     */
    StoredImage save(String originalFilename, String contentType, byte[] data);

    /**
     * 按 ID 读取图片（含字节内容）。
     *
     * @throws com.aiassist.exception.ResourceNotFoundException 图片不存在
     */
    StoredImage load(String id);

    /**
     * 按 ID 删除图片。幂等：文件不存在时视为删除成功（不上抛），
     * 便于发送失败回滚、删除对话级联清理时安全重试。
     *
     * @throws IllegalArgumentException id 非法（如路径穿越）
     */
    void delete(String id);

    /**
     * 垃圾回收兜底：删除所有"不被任何消息引用"且"落盘时间早于 minAge"的图片，
     * 覆盖上传后放弃发送、进程崩溃、写盘残片等无法即时回滚的场景。
     *
     * @param referencedIds 仍被对话消息引用的图片 ID 集合（白名单，绝不删除）
     * @param minAge        宽限期：文件至少存活该时长才允许被清理，
     *                      避免删掉"刚上传、请求尚在进行中"的图片
     * @return 实际被删除的文件名/ID 列表（供日志审计）
     */
    List<String> cleanupUnreferenced(Set<String> referencedIds, Duration minAge);
}
