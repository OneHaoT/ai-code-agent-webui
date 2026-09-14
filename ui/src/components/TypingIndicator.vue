<script setup>
// AI 正在输入的加载指示器，自包含样式，避免 scoped 样式隔离问题
defineProps({
  // 是否处于深度思考等待（仅文案差异）
  thinking: { type: Boolean, default: false },
  // 自定义等待文案（如"正在上传图片…"），优先于 thinking 文案
  label: { type: String, default: '' }
})
</script>

<template>
  <div class="typing-row">
    <div class="avatar">AI</div>
    <div class="content">
      <div class="who">AI 助手</div>
      <div class="bubble">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
        <span v-if="label" class="thinking-label">{{ label }}</span>
        <span v-else-if="thinking" class="thinking-label">深度思考中，复杂问题可能需要几十秒…</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.typing-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 22px;
  padding-left: 10%;
  padding-right: 10%;
}
.avatar {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #4f6ef7, #7c3aed);
}
.content {
  display: flex;
  flex-direction: column;
  max-width: 78%;
}
.who {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 5px;
}
.bubble {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 14px 18px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  border-top-left-radius: 4px;
  box-shadow: var(--shadow);
  width: fit-content;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #b6bac2;
  animation: wave 1.3s ease-in-out infinite;
}
.dot:nth-child(2) {
  animation-delay: 0.18s;
}
.dot:nth-child(3) {
  animation-delay: 0.36s;
}
.thinking-label {
  margin-left: 8px;
  font-size: 13px;
  color: var(--muted);
  white-space: nowrap;
}
@keyframes wave {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.45;
  }
  30% {
    transform: translateY(-5px);
    opacity: 1;
  }
}
@media (prefers-reduced-motion: reduce) {
  .dot {
    animation: none;
    opacity: 0.7;
  }
}
</style>
