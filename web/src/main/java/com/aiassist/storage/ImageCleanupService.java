package com.aiassist.storage;

import java.time.Duration;
import java.util.Set;

import com.aiassist.config.StorageProperties;
import com.aiassist.store.ConversationStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/**
 * 图片垃圾回收（GC）兜底：定时删除"不被任何对话消息引用"且超过宽限期的图片。
 *
 * 即时回滚（发送失败反删、删除对话级联）覆盖正常路径；
 * 本任务覆盖异常路径：上传后放弃发送、进程在中途崩溃、写盘残片、历史遗留等。
 *
 * 判定规则（白名单制，可审计）：
 * 1. 仍被任意对话消息引用的图片 ID —— 永不删除；
 * 2. 落盘时间在宽限期内（默认 60 分钟，防止删掉请求进行中的图片）—— 保留；
 * 3. 其余文件 —— 删除。
 *
 * 未来切换 OSS：OssStorageService.cleanupUnreferenced 对应
 * listObjects + deleteObjects（也可直接用 OSS 生命周期规则实现同等策略）。
 */
@Service
public class ImageCleanupService {

    private static final Logger log = LoggerFactory.getLogger(ImageCleanupService.class);

    private final StorageService storageService;
    private final ConversationStore conversationStore;
    private final StorageProperties properties;

    public ImageCleanupService(StorageService storageService,
                               ConversationStore conversationStore,
                               StorageProperties properties) {
        this.storageService = storageService;
        this.conversationStore = conversationStore;
        this.properties = properties;
    }

    /** 默认每小时整点执行一次，可用 app.storage.cleanup.cron 覆盖 */
    @Scheduled(cron = "${app.storage.cleanup.cron:0 0 * * * *}")
    public void cleanupOrphanImages() {
        // 一条 distinct SQL 取全库被引用的图片 ID（不再全量装载对话/消息）
        Set<String> referenced = conversationStore.findAllReferencedImageIds();

        StorageProperties.Cleanup cfg = properties.cleanup() != null
                ? properties.cleanup()
                : new StorageProperties.Cleanup(null, 0);
        Duration grace = Duration.ofMinutes(cfg.graceMinutes());

        var deleted = storageService.cleanupUnreferenced(referenced, grace);
        log.debug("图片 GC 完成，引用中 {} 张，本次清理 {} 张", referenced.size(), deleted.size());
    }
}
