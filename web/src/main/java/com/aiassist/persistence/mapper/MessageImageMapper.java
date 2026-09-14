package com.aiassist.persistence.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import com.aiassist.persistence.entity.MessageImageEntity;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;

/**
 * 消息图片引用表 Mapper。
 */
@Mapper
public interface MessageImageMapper extends BaseMapper<MessageImageEntity> {

    /** 某对话下全部图片引用（按消息序号、图片序号排序），store 组装聚合时一次查出 */
    @Select("""
            select mi.id, mi.message_id, mi.seq, mi.image_id, mi.filename, mi.content_type
            from message_image mi
            join message m on mi.message_id = m.id
            where m.conversation_id = #{convId}
            order by m.seq, mi.seq
            """)
    List<MessageImageEntity> findByConversationId(@Param("convId") String convId);

    /** GC 用：全库仍被引用的图片文件 ID（走 idx_img_id 索引，一条 SQL） */
    @Select("select distinct image_id from message_image")
    List<String> findAllReferencedImageIds();
}
