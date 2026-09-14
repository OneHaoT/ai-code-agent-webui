package com.aiassist.storage;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import com.aiassist.config.StorageProperties;
import com.aiassist.exception.ResourceNotFoundException;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * 本地磁盘存储实现：图片保存到 app.storage.local.dir 配置的目录
 * （默认 ../image，即项目根目录下 image/）。
 *
 * 仅当 app.storage.type=local（或缺省）时装配；未来实现 OSS 版本后通过配置切换。
 */
@Service
@ConditionalOnProperty(name = "app.storage.type", havingValue = "local", matchIfMissing = true)
public class LocalStorageService implements StorageService {

    private static final Logger log = LoggerFactory.getLogger(LocalStorageService.class);

    /** 扩展名 -> MIME，DeepSeek 视觉仅支持 JPEG/PNG/GIF/WebP */
    private static final Map<String, String> CONTENT_TYPES = Map.of(
            "jpg", "image/jpeg",
            "jpeg", "image/jpeg",
            "png", "image/png",
            "gif", "image/gif",
            "webp", "image/webp"
    );
    private static final Set<String> ALLOWED_EXT = CONTENT_TYPES.keySet();

    private final Path baseDir;

    public LocalStorageService(StorageProperties properties) {
        this.baseDir = Paths.get(properties.local().dir()).toAbsolutePath().normalize();
    }

    @PostConstruct
    void init() {
        try {
            Files.createDirectories(baseDir);
            log.info("图片本地存储目录: {}", baseDir);
        } catch (IOException e) {
            throw new UncheckedIOException("创建图片存储目录失败: " + baseDir, e);
        }
    }

    @Override
    public StoredImage save(String originalFilename, String contentType, byte[] data) {
        String ext = resolveExt(originalFilename, contentType);
        if (!ALLOWED_EXT.contains(ext)) {
            throw new IllegalArgumentException(
                    "仅支持 JPEG / PNG / GIF / WebP 格式图片");
        }
        String id = UUID.randomUUID().toString().replace("-", "") + "." + ext;
        Path target = safeResolve(id);
        // 先写同目录临时文件再原子移动，避免写盘中途失败留下半个图片文件
        Path tmp = safeResolve("." + id + ".tmp");
        try {
            Files.write(tmp, data);
            try {
                Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFail) {
                // 个别文件系统不支持原子移动时退化为普通替换
                Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            deleteQuietly(tmp);
            throw new UncheckedIOException("保存图片失败", e);
        }
        String mime = CONTENT_TYPES.get(ext);
        log.info("图片已保存 id={} size={}B", id, data.length);
        return new StoredImage(id, StringUtils.hasText(originalFilename) ? originalFilename : id,
                mime, data.length, data);
    }

    @Override
    public void delete(String id) {
        if (id == null || id.isBlank()) {
            return;
        }
        Path target = safeResolve(id);
        try {
            boolean removed = Files.deleteIfExists(target);
            if (removed) {
                log.info("图片已删除 id={}", id);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("删除图片失败: " + id, e);
        }
    }

    @Override
    public List<String> cleanupUnreferenced(Set<String> referencedIds, Duration minAge) {
        Instant threshold = Instant.now().minus(minAge);
        Set<String> alive = referencedIds == null ? Set.of() : referencedIds;
        List<String> deleted = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(baseDir)) {
            for (Path p : stream) {
                if (!Files.isRegularFile(p)) {
                    continue;
                }
                String name = p.getFileName().toString();
                if (alive.contains(name)) {
                    continue; // 白名单：仍被消息引用
                }
                Instant modified = Files.getLastModifiedTime(p).toInstant();
                if (modified.isAfter(threshold)) {
                    continue; // 宽限期内（可能刚上传、请求进行中）
                }
                if (deleteQuietly(p)) {
                    deleted.add(name);
                }
            }
        } catch (IOException e) {
            log.warn("扫描图片存储目录失败: {}", e.getMessage());
        }
        if (!deleted.isEmpty()) {
            log.info("图片 GC：清理 {} 个无引用文件 {}", deleted.size(), deleted);
        }
        return deleted;
    }

    private boolean deleteQuietly(Path p) {
        try {
            return Files.deleteIfExists(p);
        } catch (IOException e) {
            log.warn("删除文件失败 {}: {}", p, e.getMessage());
            return false;
        }
    }

    @Override
    public StoredImage load(String id) {
        if (id == null || id.isBlank()) {
            throw new ResourceNotFoundException("图片不存在");
        }
        Path target = safeResolve(id);
        if (!Files.isReadable(target)) {
            throw new ResourceNotFoundException("图片不存在或已被删除: " + id);
        }
        try {
            byte[] data = Files.readAllBytes(target);
            String ext = extOf(id);
            String mime = CONTENT_TYPES.getOrDefault(ext, "application/octet-stream");
            return new StoredImage(id, id, mime, data.length, data);
        } catch (IOException e) {
            throw new UncheckedIOException("读取图片失败: " + id, e);
        }
    }

    /** 防止路径穿越：解析后的路径必须仍在 baseDir 内，且扩展名合法 */
    private Path safeResolve(String id) {
        String clean = Paths.get(id).getFileName().toString();
        Path resolved = baseDir.resolve(clean).normalize();
        if (!resolved.startsWith(baseDir)) {
            throw new IllegalArgumentException("非法的图片 ID");
        }
        return resolved;
    }

    private static String resolveExt(String filename, String contentType) {
        String ext = extOf(filename);
        if (ALLOWED_EXT.contains(ext)) {
            return ext;
        }
        // 文件名没有合法扩展名时，用 MIME 兜底
        if (contentType != null) {
            return switch (contentType.toLowerCase()) {
                case "image/jpeg", "image/jpg" -> "jpg";
                case "image/png" -> "png";
                case "image/gif" -> "gif";
                case "image/webp" -> "webp";
                default -> ext;
            };
        }
        return ext;
    }

    private static String extOf(String filename) {
        if (filename == null) {
            return "";
        }
        int dot = filename.lastIndexOf('.');
        return dot >= 0 && dot < filename.length() - 1
                ? filename.substring(dot + 1).toLowerCase()
                : "";
    }
}
