package com.aiassist.config;

import java.time.Duration;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * AI 模块（FastAPI）连接配置。
 *
 * 对应 application.yml 中:
 * ai.service.url / connect-timeout / read-timeout
 */
@ConfigurationProperties(prefix = "ai.service")
public record AiServiceProperties(
        String url,
        Duration connectTimeout,
        Duration readTimeout,
        Integer historyImageWindow
) {
    public AiServiceProperties {
        if (url == null || url.isBlank()) {
            url = "http://127.0.0.1:8001";
        }
        if (connectTimeout == null) {
            connectTimeout = Duration.ofSeconds(5);
        }
        if (readTimeout == null) {
            readTimeout = Duration.ofSeconds(120);
        }
        if (historyImageWindow == null || historyImageWindow < 0) {
            // 需 >= AI 模块 MEMORY_WINDOW_SIZE（默认 10）：窗口之外的历史图片
            // AI 只会折叠成“附带 N 张图片”标记，不传字节不影响效果
            historyImageWindow = 10;
        }
    }
}
