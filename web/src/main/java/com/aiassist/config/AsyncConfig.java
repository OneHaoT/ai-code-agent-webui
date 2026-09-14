package com.aiassist.config;

import java.util.concurrent.ThreadPoolExecutor;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

/**
 * SSE 流式对话专用线程池。
 *
 * 每个流式请求在一个工作线程内同步读取 AI 模块的 SSE 流（最长约 300s），
 * 不能占用 Tomcat 请求线程（会迅速耗尽默认 200 个工作线程）；
 * 池有界（核心 2 / 最大 8 / 队列 100），满载后 AbortPolicy 抛
 * RejectedExecutionException，由全局异常处理转 503，避免请求无限堆积拖垮进程。
 */
@Configuration
public class AsyncConfig {

    @Bean("sseExecutor")
    public ThreadPoolTaskExecutor sseExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(2);
        executor.setMaxPoolSize(8);
        executor.setQueueCapacity(100);
        executor.setThreadNamePrefix("sse-chat-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy());
        executor.initialize();
        return executor;
    }
}
