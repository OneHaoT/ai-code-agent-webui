package com.aiassist.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 图片存储配置。
 *
 * 对应 application.yml:
 * <pre>
 * app:
 *   storage:
 *     type: local          # local=本地磁盘（当前实现）；oss=阿里云OSS等（未来扩展）
 *     local:
 *       dir: ../image      # 本地存储目录（相对 web 工作目录即项目根目录下 image/）
 *     cleanup:
 *       cron: "0 0 * * * *"   # 孤儿图片定时 GC（默认每小时整点）
 *       grace-minutes: 60     # 宽限期：无引用且落盘超过该时长才清理
 * </pre>
 *
 * 新增 OSS 时：实现 OssStorageService 并用
 * {@code @ConditionalOnProperty(name="app.storage.type", havingValue="oss")} 装配，
 * 业务代码（ImageController/ChatService）只依赖 StorageService 接口，无需改动。
 */
@ConfigurationProperties(prefix = "app.storage")
public record StorageProperties(
        String type,
        Local local,
        Cleanup cleanup
) {

    public StorageProperties {
        if (type == null || type.isBlank()) {
            type = "local";
        }
    }

    public record Local(String dir, int maxDimension) {
        public Local {
            if (dir == null || dir.isBlank()) {
                dir = "../image";
            }
            // 后端兜底：图片最长边像素上限（前端已压缩到 2048，这里放宽到 4096
            // 防绕过前端直传超大图；<=0 表示不限制）
            if (maxDimension <= 0) {
                maxDimension = 4096;
            }
        }
    }

    public record Cleanup(String cron, long graceMinutes) {
        public Cleanup {
            if (cron == null || cron.isBlank()) {
                cron = "0 0 * * * *"; // 每小时整点
            }
            if (graceMinutes <= 0) {
                graceMinutes = 60;
            }
        }
    }
}
