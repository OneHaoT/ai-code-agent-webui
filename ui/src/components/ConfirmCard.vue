<script setup>
import { computed } from 'vue'

// 阶段3：写/执行工具的人机确认卡（confirm 帧驱动）。
// 挂在流式 assistant 气泡内；summary 为后端参数摘要，纯文本插值（禁 v-html 防 XSS）。
// 状态机：pending（等待决策，显示按钮）→ approved / rejected / expired（终态文案）。
const props = defineProps({
  request: { type: Object, required: true }
})
const emit = defineEmits(['decide'])

const TOOL_LABEL = {
  write_file: '写入文件',
  run_command: '执行命令'
}

const toolLabel = computed(
  () => TOOL_LABEL[props.request.tool] || props.request.tool || '工具调用'
)

// 终态展示文案（expired：confirmId 失效——超时/断连/AI 重启）
const STATE_TEXT = {
  approved: '已确认执行',
  rejected: '已拒绝',
  expired: '确认已失效（超时或连接已中断）'
}
</script>

<template>
  <div class="confirm-card" :class="[`is-${request.state}`]">
    <div class="confirm-head">
      <svg class="confirm-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      </svg>
      <span>AI 请求{{ toolLabel }}，需要你的确认</span>
    </div>
    <div class="confirm-summary">{{ request.summary }}</div>
    <div v-if="request.state === 'pending'" class="confirm-actions">
      <button type="button" class="btn-approve" @click="emit('decide', true)">确认执行</button>
      <button type="button" class="btn-reject" @click="emit('decide', false)">拒绝</button>
    </div>
    <div v-else class="confirm-state-text" :class="request.state">
      {{ STATE_TEXT[request.state] || request.state }}
    </div>
  </div>
</template>

<style scoped>
.confirm-card {
  margin-bottom: 10px;
  padding: 9px 11px;
  border: 1px solid #f5d9a8;
  border-radius: 10px;
  background: #fffbeb;
  overflow: hidden;
}
.confirm-card.is-approved {
  border-color: rgba(22, 163, 74, 0.35);
  background: #f0fdf4;
}
.confirm-card.is-rejected,
.confirm-card.is-expired {
  border-color: var(--border);
  background: var(--surface-2, #f5f6f8);
}
.confirm-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: #92400e;
}
.is-approved .confirm-head {
  color: #16a34a;
}
.is-rejected .confirm-head,
.is-expired .confirm-head {
  color: var(--muted);
}
.confirm-icon {
  flex-shrink: 0;
}
.confirm-summary {
  margin-top: 6px;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.75);
  border: 1px dashed #f5d9a8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text, #2d303c);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}
.confirm-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.confirm-actions button {
  height: 26px;
  padding: 0 14px;
  border-radius: 7px;
  font-size: 12.5px;
  font-family: inherit;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-approve {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #fff;
}
.btn-approve:hover {
  filter: brightness(1.08);
}
.btn-reject {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
}
.btn-reject:hover {
  border-color: var(--danger);
  color: var(--danger);
}
.confirm-state-text {
  margin-top: 6px;
  font-size: 12px;
  font-weight: 600;
}
.confirm-state-text.approved {
  color: #16a34a;
}
.confirm-state-text.rejected {
  color: var(--danger);
}
.confirm-state-text.expired {
  color: var(--muted);
  font-weight: 400;
}
</style>
