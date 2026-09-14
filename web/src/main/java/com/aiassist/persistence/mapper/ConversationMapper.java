package com.aiassist.persistence.mapper;

import com.aiassist.persistence.entity.ConversationEntity;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Mapper;

/**
 * 对话表 Mapper。查询均走 BaseMapper + Wrapper（见 ConversationStore），无自定义 SQL。
 */
@Mapper
public interface ConversationMapper extends BaseMapper<ConversationEntity> {
}
