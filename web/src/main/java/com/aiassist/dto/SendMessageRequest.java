package com.aiassist.dto;

import java.util.List;

import com.aiassist.model.ImageRef;

/**
 * 发送消息请求体。
 * 文本与图片至少有一个非空（校验在 ChatService 中做，
 * 因为允许"只发图片不写字"的纯图片提问）。
 *
 * @param message  文本内容（可为空字符串）
 * @param images   本次消息附带的图片引用
 * @param thinking 是否开启深度思考模式
 */
public record SendMessageRequest(
        String message,
        List<ImageRef> images,
        Boolean thinking
) {
}
