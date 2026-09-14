package com.aiassist.controller;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.time.Duration;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;

import javax.imageio.ImageIO;
import javax.imageio.ImageReader;
import javax.imageio.stream.ImageInputStream;

import com.aiassist.config.StorageProperties;
import com.aiassist.dto.UploadImageResponse;
import com.aiassist.storage.StoredImage;
import com.aiassist.storage.StorageService;
import com.aiassist.store.ConversationStore;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/**
 * 图片上传/读取接口。前端通过 /api/images/{id} 同源（Vite 代理）展示图片；
 * ChatService 在调用 AI 模块前经 StorageService 读出字节并转 data URL。
 */
@RestController
@RequestMapping("/api/images")
public class ImageController {

    /** 单张图片大小上限（与 application.yml 的 multipart 限制保持一致） */
    private static final long MAX_IMAGE_SIZE = 10L * 1024 * 1024;
    private static final Set<String> ALLOWED_MIME =
            Set.of("image/jpeg", "image/png", "image/gif", "image/webp");

    private final StorageService storageService;
    private final StorageProperties storageProperties;
    private final ConversationStore conversationStore;

    public ImageController(StorageService storageService, StorageProperties storageProperties,
                           ConversationStore conversationStore) {
        this.storageService = storageService;
        this.storageProperties = storageProperties;
        this.conversationStore = conversationStore;
    }

    /** 上传图片（multipart/form-data，字段名 file） */
    @PostMapping("/upload")
    public UploadImageResponse upload(@RequestParam("file") MultipartFile file) throws IOException {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("图片内容为空");
        }
        if (file.getSize() > MAX_IMAGE_SIZE) {
            throw new IllegalArgumentException("图片大小不能超过 10MB");
        }
        String contentType = file.getContentType();
        if (contentType == null || !ALLOWED_MIME.contains(contentType.toLowerCase())) {
            // LocalStorageService 还会按扩展名再校验一次
            throw new IllegalArgumentException("仅支持 JPEG / PNG / GIF / WebP 格式图片");
        }

        byte[] bytes = file.getBytes();
        validateDimensions(bytes);

        StoredImage saved = storageService.save(
                file.getOriginalFilename(), contentType, bytes);
        return UploadImageResponse.of(
                saved.id(), saved.originalFilename(), saved.contentType(), saved.size());
    }

    /**
     * 尺寸兜底校验：最长边超过 app.storage.local.max-dimension（默认 4096px）拒绝。
     * 仅读取图片头中的宽高，不完整解码；WebP 在 JDK ImageIO 下无内置 reader 时跳过
     * （仍有 10MB 体积限制兜底）。
     */
    private void validateDimensions(byte[] bytes) {
        int max = storageProperties.local() != null
                ? storageProperties.local().maxDimension() : 0;
        if (max <= 0) {
            return;
        }
        try (ImageInputStream iis = ImageIO.createImageInputStream(new ByteArrayInputStream(bytes))) {
            if (iis == null) {
                return;
            }
            Iterator<ImageReader> readers = ImageIO.getImageReaders(iis);
            if (!readers.hasNext()) {
                return; // 无法解析维度（如 WebP），交给体积限制兜底
            }
            ImageReader reader = readers.next();
            try {
                reader.setInput(iis, true);
                int width = reader.getWidth(0);
                int height = reader.getHeight(0);
                if (Math.max(width, height) > max) {
                    throw new IllegalArgumentException(
                            "图片尺寸过大：最长边 " + Math.max(width, height)
                                    + "px，上限 " + max + "px");
                }
            } finally {
                reader.dispose();
            }
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (IOException e) {
            // 读取维度失败不阻断上传（体积/格式校验已在前面完成）
        }
    }

    /** 按 ID 读取图片（用于聊天界面缩略图/大图） */
    @GetMapping("/{id}")
    public ResponseEntity<byte[]> getImage(@PathVariable String id) {
        StoredImage image = storageService.load(id);
        MediaType mediaType = MediaType.parseMediaType(image.contentType());
        return ResponseEntity.ok()
                .contentType(mediaType)
                // 图片 ID 是一次性 UUID，内容永不变（删除即 404，ID 不会复用），
                // 可放心强缓存一年：切回历史对话不再反复读盘/传图
                .header(HttpHeaders.CACHE_CONTROL,
                        CacheControl.maxAge(Duration.ofDays(365)).immutable()
                                .getHeaderValue())
                .body(image.data());
    }

    /**
     * 删除图片（幂等：不存在也返回成功）。
     * 用途：多图上传部分失败时，前端回滚已上传成功的图片；
     * 正常发送的消息图片由后端在发送失败/删除对话时内部级联清理，不走此接口。
     * 保护：图片若已被某条消息引用则拒绝删除，否则历史消息会裂图、
     * 甚至因文件缺失导致该会话后续消息无法发送。
     */
    @DeleteMapping("/{id}")
    public Map<String, Object> deleteImage(@PathVariable String id) {
        if (conversationStore.isImageReferenced(id)) {
            throw new IllegalArgumentException("图片已被消息引用，无法删除");
        }
        storageService.delete(id);
        return Map.of("success", true, "id", id);
    }
}
