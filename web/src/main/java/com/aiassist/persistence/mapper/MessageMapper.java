package com.aiassist.persistence.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import com.aiassist.persistence.entity.MessageEntity;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;

/**
 * 消息表 Mapper。删除消息时 message_image 由数据库外键 ON DELETE CASCADE 级联清理。
 */
@Mapper
public interface MessageMapper extends BaseMapper<MessageEntity> {

    /** 当前会话最大消息序号；无消息时返回 0（coalesce 为标准 SQL，跨库可移植） */
    @Select("select coalesce(max(seq), 0) from message where conversation_id = #{convId}")
    int findMaxSeq(@Param("convId") String convId);
}
