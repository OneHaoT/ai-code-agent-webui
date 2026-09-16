package com.aiassist.service;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import com.aiassist.client.StreamHandler;
import com.aiassist.client.dto.AiChatRequest;
import com.aiassist.client.dto.AiHealthResponse;
import com.aiassist.client.dto.AiHistoryMessage;
import com.aiassist.config.AiServiceProperties;
import com.aiassist.exception.AiServiceException;
import com.aiassist.exception.ResourceNotFoundException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.util.StreamUtils;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 调用 AI 模块(FastAPI) 的客户端。
 *
 * 企业级实践要点：
 * 1. 使用 Spring 6 的 RestClient（同步、流式 API），由 Spring Boot 自动配置
 *    Jackson 消息转换器，请求/响应直接使用类型化 DTO，不再手工拼 JSON；
 * 2. 底层为 SimpleClientHttpRequestFactory（JDK HttpURLConnection，HTTP/1.1），
 *    不会产生 JDK HttpClient 默认的 h2c 明文升级握手，也就不会出现 body 被对端丢弃；
 *    连接/读取超时由 application.yml 统一配置；
 * 3. 非 2xx 响应通过 onStatus 读取错误体并抛 AiServiceException，
 *    由 GlobalExceptionHandler 统一转成 502，不向前端泄漏堆栈；
 * 4. /health 不抛异常，任何失败都降级为 unreachable 响应。
 */
@Component
public class AiClient {

    private static final Logger log = LoggerFactory.getLogger(AiClient.class);

    private final RestClient restClient;
    private final ObjectMapper objectMapper;

    public AiClient(RestClient.Builder restClientBuilder, AiServiceProperties properties,
                    ObjectMapper objectMapper) {
        // HTTP/1.1 + 显式超时（大模型生成较慢，读取超时单独放宽）
        SimpleClientHttpRequestFactory requestFactory = new SimpleClientHttpRequestFactory();
        requestFactory.setConnectTimeout(properties.connectTimeout());
        requestFactory.setReadTimeout(properties.readTimeout());

        this.restClient = restClientBuilder
                .baseUrl(properties.url())
                .requestFactory(requestFactory)
                .build();
        this.objectMapper = objectMapper;
    }

    /**
     * 流式对话：请求 AI 模块 /ai/chat/stream，边读 SSE 帧边回调 handler。
     *
     * 实现要点：
     * 1. RestClient.exchange 在响应体 InputStream 上直接读取，不缓冲整段回答
     *    （SimpleClientHttpRequestFactory / HTTP/1.1 分块传输）；
     * 2. 极简 SSE 解析：按行读取，空行分帧，只认 event/data 两个字段；
     * 3. AI 模块的 error 帧在此转成 AiServiceException 抛出，调用方按失败处理；
     * 4. HTTP 非 2xx（如 400 视觉校验）同样在读取流之前抛出并带上错误体。
     */
    public void streamChat(String conversationId, String message,
                           List<String> images, List<AiHistoryMessage> history,
                           boolean thinking, String workspaceRoot, StreamHandler handler) {
        AiChatRequest request =
                new AiChatRequest(conversationId, message, images, history, thinking,
                        workspaceRoot == null || workspaceRoot.isBlank() ? null : workspaceRoot);

        try {
            restClient.post()
                    .uri("/ai/chat/stream")
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.TEXT_EVENT_STREAM)
                    .body(request)
                    .exchange((req, res) -> {
                        if (res.getStatusCode().isError()) {
                            String errBody = StreamUtils.copyToString(
                                    res.getBody(), StandardCharsets.UTF_8);
                            throw new AiServiceException(
                                    "AI 模块返回 " + res.getStatusCode().value() + ": " + errBody);
                        }
                        parseSseStream(res.getBody(), handler);
                        return null;
                    });
        } catch (AiServiceException e) {
            throw e;
        } catch (ResourceAccessException e) {
            throw new AiServiceException("无法连接 AI 模块或读取超时: " + e.getMessage(), e);
        } catch (RestClientException e) {
            // 读流过程中的 IOException 会被 RestClient 包装到这里
            throw new AiServiceException("调用 AI 模块流式接口失败: " + e.getMessage(), e);
        }
    }

    /**
     * 极简 SSE 帧解析器：空行分帧；支持 event/data 字段与多行 data。
     * 包可见以便同包单测直接以构造的 SSE InputStream 驱动帧分发（不依赖 HTTP）。
     */
    void parseSseStream(InputStream body, StreamHandler handler) throws IOException {
        BufferedReader reader =
                new BufferedReader(new InputStreamReader(body, StandardCharsets.UTF_8));
        String event = null;
        StringBuilder data = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (line.isEmpty()) {
                if (event != null || data.length() > 0) {
                    dispatchSseFrame(event == null ? "message" : event, data.toString(), handler);
                }
                event = null;
                data.setLength(0);
                continue;
            }
            if (line.startsWith(":")) {
                continue; // SSE 注释/心跳
            }
            int colon = line.indexOf(':');
            String field = colon < 0 ? line : line.substring(0, colon);
            String value;
            if (colon < 0) {
                value = "";
            } else {
                value = line.substring(colon + 1);
                if (value.startsWith(" ")) {
                    value = value.substring(1); // SSE 规范：值前可选的单个空格
                }
            }
            switch (field) {
                case "event" -> event = value;
                case "data" -> {
                    if (data.length() > 0) {
                        data.append('\n');
                    }
                    data.append(value);
                }
                default -> {
                    // id/retry 等字段本协议不使用
                }
            }
        }
        // 兜底：对端若漏发结尾空行，仍处理最后一帧
        if (event != null || data.length() > 0) {
            dispatchSseFrame(event == null ? "message" : event, data.toString(), handler);
        }
    }

    private void dispatchSseFrame(String event, String rawData, StreamHandler handler) {
        try {
            switch (event) {
                case "meta" -> {
                    JsonNode node = objectMapper.readTree(rawData);
                    handler.onMeta(node.path("model").asText(""));
                }
                case "token" -> handler.onToken(
                        objectMapper.readTree(rawData).path("delta").asText(""));
                case "reasoning" -> handler.onReasoning(
                        objectMapper.readTree(rawData).path("delta").asText(""));
                case "tool_call" -> {
                    JsonNode node = objectMapper.readTree(rawData);
                    JsonNode argsNode = node.get("args");
                    // 显式 null（参数 JSON 非法）与字段缺失统一为 Java null
                    if (argsNode != null && argsNode.isNull()) {
                        argsNode = null;
                    }
                    JsonNode rawNode = node.get("raw_args");
                    String rawArgs = rawNode != null && !rawNode.isNull()
                            ? rawNode.asText() : null;
                    handler.onToolCall(node.path("id").asText(""),
                            node.path("name").asText(""), argsNode, rawArgs);
                }
                case "confirm" -> {
                    // 阶段3：写/执行工具人机确认请求，web 只透传不理解语义
                    JsonNode node = objectMapper.readTree(rawData);
                    handler.onConfirm(node.path("id").asText(""),
                            node.path("tool").asText(""),
                            node.path("summary").asText(""));
                }
                case "tool_result" -> {
                    JsonNode node = objectMapper.readTree(rawData);
                    // status 缺失按 error 处理（帧异常时不得伪装成功）
                    handler.onToolResult(
                            node.path("id").asText(""),
                            node.path("name").asText(""),
                            node.path("status").asText("error"),
                            node.path("output").asText(""),
                            node.path("truncated").asBoolean(false));
                }
                case "done" -> {
                    // 正常结束标记；落库由调用方基于累积内容完成
                }
                case "error" -> {
                    String msg = objectMapper.readTree(rawData)
                            .path("message").asText("AI 模块流式调用失败");
                    throw new AiServiceException(msg);
                }
                default -> log.debug("忽略未知 SSE 事件: {}", event);
            }
        } catch (AiServiceException e) {
            throw e;
        } catch (IOException e) {
            // data 是合法 JSON（由 AI 模块保证），坏帧直接忽略而非中断整条流
            log.warn("解析 SSE 帧失败 event={}: {}", event, e.getMessage());
        }
    }

    /**
     * 转发用户对 confirm 帧的决策到 AI 模块 POST /ai/confirm。
     * ai 返回 404（未知/已失效 confirmId）时转抛 ResourceNotFoundException（→ 404 透传），
     * 其余失败统一 AiServiceException（→ 502）。
     */
    public JsonNode confirmExecution(String confirmId, boolean approved) {
        try {
            return restClient.post()
                    .uri("/ai/confirm")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(Map.of("confirm_id", confirmId, "approved", approved))
                    .accept(MediaType.APPLICATION_JSON)
                    .retrieve()
                    .onStatus(status -> status.value() == 404, (req, res) -> {
                        throw new ResourceNotFoundException("确认请求不存在或已失效");
                    })
                    .onStatus(HttpStatusCode::isError, (req, res) -> {
                        throw new AiServiceException("确认决策转发失败: " + res.getStatusCode());
                    })
                    .body(JsonNode.class);
        } catch (ResourceNotFoundException e) {
            throw e;
        } catch (ResourceAccessException e) {
            throw new AiServiceException("无法连接 AI 模块: " + e.getMessage(), e);
        } catch (RestClientException e) {
            throw new AiServiceException("转发确认决策失败: " + e.getMessage(), e);
        }
    }

    /**
     * 通知 AI 模块清理某会话的摘要缓存（对话被删除时调用）。
     * best-effort：任何失败只记日志，不影响删除主流程。
     */
    public void forgetMemory(String conversationId) {
        try {
            restClient.delete()
                    .uri("/ai/memory/{id}", conversationId)
                    .retrieve()
                    .toBodilessEntity();
        } catch (Exception e) {
            log.warn("清理 AI 摘要缓存失败（不影响删除）conversationId={}: {}",
                    conversationId, e.getMessage());
        }
    }

    /**
     * 健康检查：永不抛异常，失败时返回 unreachable 降级响应。
     */
    public AiHealthResponse health() {
        try {
            AiHealthResponse resp = restClient.get()
                    .uri("/health")
                    .accept(MediaType.APPLICATION_JSON)
                    .retrieve()
                    .onStatus(HttpStatusCode::isError, (req, res) -> {
                        throw new AiServiceException("AI 模块状态异常: " + res.getStatusCode());
                    })
                    .body(AiHealthResponse.class);
            return resp != null ? resp : AiHealthResponse.unreachable("空响应");
        } catch (Exception e) {
            log.warn("AI 模块健康检查失败: {}", e.getMessage());
            return AiHealthResponse.unreachable(e.getMessage());
        }
    }

    /**
     * 把前端上传的文件转发给 AI 模块 POST /workspace/files，保存到默认工作区。
     */
    public JsonNode uploadWorkspaceFile(String filename, byte[] data) {
        Resource fileResource = new ByteArrayResource(data) {
            @Override public String getFilename() { return filename; }
        };
        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("file", fileResource);
        return restClient.post()
                .uri("/workspace/files")
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(body)
                .accept(MediaType.APPLICATION_JSON)
                .retrieve()
                .onStatus(HttpStatusCode::isError, (req, res) -> {
                    throw new AiServiceException("上传失败: " + res.getStatusCode());
                })
                .body(JsonNode.class);
    }

    /** 让 AI 模块在本机文件浏览器中打开工作区目录（root 空 = 默认工作区）。 */
    public JsonNode openWorkspace(String root) {
        return restClient.post()
                .uri(uriBuilder -> {
                    var b = uriBuilder.path("/workspace/open");
                    if (root != null && !root.isBlank()) {
                        b.queryParam("root", root);
                    }
                    return b.build();
                })
                .accept(MediaType.APPLICATION_JSON)
                .retrieve()
                .onStatus(HttpStatusCode::isError, (req, res) -> {
                    throw new AiServiceException("打开工作区失败: " + res.getStatusCode());
                })
                .body(JsonNode.class);
    }

    /**
     * 只读浏览工作区单层目录（阶段3.5 项目树懒加载数据源）。
     * root 为空 = AI 默认工作区；path 为相对 root 的子目录（空 = 根）。
     * ai 侧 400（路径越界/非法工作区等请求本身非法）→ IllegalArgumentException
     * 透传 400 并携带 ai 的 detail 信息；其余错误 → AiServiceException（502）。
     */
    public JsonNode listWorkspace(String root, String path) {
        return restClient.get()
                .uri(uriBuilder -> {
                    var b = uriBuilder.path("/workspace/list");
                    if (root != null && !root.isBlank()) {
                        b.queryParam("root", root);
                    }
                    if (path != null && !path.isBlank()) {
                        b.queryParam("path", path);
                    }
                    return b.build();
                })
                .accept(MediaType.APPLICATION_JSON)
                .retrieve()
                .onStatus(status -> status.value() == 400, (req, res) -> {
                    throw new IllegalArgumentException(aiErrorDetail(res));
                })
                .onStatus(HttpStatusCode::isError, (req, res) -> {
                    throw new AiServiceException("浏览工作区失败: " + res.getStatusCode());
                })
                .body(JsonNode.class);
    }

    /** 读取 ai 错误响应体中的 detail 字段（FastAPI HTTPException 格式），失败退回状态码文本。 */
    private String aiErrorDetail(org.springframework.http.client.ClientHttpResponse res) throws IOException {
        try (InputStream in = res.getBody()) {
            String body = new String(StreamUtils.copyToByteArray(in), StandardCharsets.UTF_8);
            JsonNode node = new ObjectMapper().readTree(body);
            if (node != null && node.hasNonNull("detail")) {
                return node.get("detail").asText();
            }
        } catch (RestClientException | com.fasterxml.jackson.core.JsonProcessingException ignored) {
            // 保底走状态码文本
        }
        return "AI 模块拒绝了该请求";
    }
}
