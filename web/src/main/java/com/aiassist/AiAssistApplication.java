package com.aiassist;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * AI 智能辅助编程 - 后端服务启动类
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableScheduling
public class AiAssistApplication {

    public static void main(String[] args) {
        SpringApplication.run(AiAssistApplication.class, args);
    }
}
