package com.aiassist.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Web 配置: 允许前端 (Vue3, Vite 5173) 跨域访问后端。
 * 生产环境建议将 allowedOriginPatterns 收敛为明确域名。
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
                .allowedOriginPatterns("*")
                // 注意：新增任何 HTTP 方法（如 PATCH 重命名）都必须同步到此白名单，
                // 否则浏览器带 Origin 的请求会被 Spring 以 "Invalid CORS request" 403 拒绝
                // （curl 不带 Origin 不会触发，极易漏测）
                .allowedMethods("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
                .allowedHeaders("*")
                .allowCredentials(true)
                .maxAge(3600);
    }
}
