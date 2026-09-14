package com.aiassist.persistence.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 消息图片引用实体，对应表 message_image（扁平行映射）。
 * 只存引用（StorageService 文件 ID/文件名/MIME），不存图片字节。
 * 独立关联表而非 JSON 数组列，保证跨数据库可移植。
 */
@TableName("message_image")
public class MessageImageEntity {

    /** 数据库自增主键 */
    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    private Long messageId;

    private Integer seq;

    private String imageId;

    private String filename;

    private String contentType;

    public MessageImageEntity() {
    }

    public MessageImageEntity(Long messageId, int seq, String imageId,
                              String filename, String contentType) {
        this.messageId = messageId;
        this.seq = seq;
        this.imageId = imageId;
        this.filename = filename;
        this.contentType = contentType;
    }

    public Long getId() { return id; }
    public Long getMessageId() { return messageId; }
    public void setMessageId(Long messageId) { this.messageId = messageId; }
    public Integer getSeq() { return seq; }
    public String getImageId() { return imageId; }
    public String getFilename() { return filename; }
    public String getContentType() { return contentType; }
}
