<script setup>
import { ref, watch, nextTick, computed } from 'vue'
import MessageItem from './MessageItem.vue'
import ChatInput from './ChatInput.vue'
import TypingIndicator from './TypingIndicator.vue'
import { useToast } from '../composables/useToast.js'

const props = defineProps({
  conv: { type: Object, default: null },
  messages: { type: Array, default: () => [] },
  sending: { type: Boolean, default: false },
  uploading: { type: Boolean, default: false },
  pendingThinking: { type: Boolean, default: false },
  aiStatus: { type: Object, default: () => ({}) },
  // 侧边栏折叠状态（控制 toggle 按钮图标）
  sidebarCollapsed: { type: Boolean, default: false },
  // 右侧工作区面板折叠状态
  workspaceCollapsed: { type: Boolean, default: false }
})
const emit = defineEmits(['send', 'stop', 'retry', 'new', 'toggle-sidebar', 'toggle-workspace'])

const toast = useToast()

const scroller = ref(null)
const chatInput = ref(null)

// ---------------- 拖拽图片（整个聊天区域均可放置） ----------------
const dragActive = ref(false)
// enter/leave 在子元素间会冒泡成对触发，用计数器避免遮罩闪烁
let dragDepth = 0

function dragHasFiles(e) {
  return Array.from(e.dataTransfer?.types || []).includes('Files')
}

function onDragEnter(e) {
  if (props.sending || !dragHasFiles(e)) return
  e.preventDefault()
  dragDepth += 1
  dragActive.value = true
}

function onDragOver(e) {
  if (props.sending || !dragHasFiles(e)) return
  // 必须 preventDefault 才能触发 drop
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}

function onDragLeave(e) {
  if (!dragActive.value) return
  // relatedTarget 为 null 表示拖出了窗口
  if (e.relatedTarget === null || !e.currentTarget.contains(e.relatedTarget)) {
    dragDepth = 0
    dragActive.value = false
    return
  }
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragActive.value = false
}

function onDrop(e) {
  if (props.sending || !dragHasFiles(e)) return
  e.preventDefault()
  dragDepth = 0
  dragActive.value = false
  const files = Array.from(e.dataTransfer?.files || []).filter((f) =>
    f.type.startsWith('image/')
  )
  if (!files.length) {
    toast.error('仅支持拖入图片文件（JPEG / PNG / GIF / WebP）')
    return
  }
  chatInput.value?.addFiles(files)
}

const keyMissing = computed(
  () => props.aiStatus && props.aiStatus.key_configured === false
)
// AI 模块在线状态：health 接口本身降级返回 200+unreachable，网络全断时 aiStatus 为空对象
const aiOnline = computed(() => props.aiStatus?.status === 'ok')

async function scrollToBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

// 流式进行中、占位消息尚未收到任何正文/思维链/工具步骤时，显示打字指示器。
// 工具帧到达后气泡内的工具时间线本身即活动指示，不再重复显示指示器
const waitingFirstToken = computed(() => {
  if (!props.sending) return false
  const last = props.messages[props.messages.length - 1]
  if (!last || last.role !== 'assistant' || last.error) return true
  return !(last.content && last.content.length) &&
    !(last.reasoning && last.reasoning.length) &&
    !(last.toolTrace && last.toolTrace.length)
})

function isNearBottom() {
  const el = scroller.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120
}

watch(
  () => props.messages.length,
  () => scrollToBottom()
)
watch(
  () => props.conv,
  () => scrollToBottom()
)
// 流式增量：用户停在底部附近时自动跟随，手动上翻阅读时不打断。
// 依赖含 toolTrace 步骤数——纯工具阶段（模型长时间不吐正文）新步骤出现也要跟随
watch(
  () => {
    const last = props.messages[props.messages.length - 1]
    return last
      ? (last.content?.length || 0) +
          (last.reasoning?.length || 0) +
          (last.toolTrace?.length || 0) * 10000
      : 0
  },
  async () => {
    if (props.sending && isNearBottom()) await scrollToBottom()
  }
)
</script>

<template>
  <main
    class="chat"
    :class="{ 'drag-active': dragActive }"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <header class="chat-head">
      <div class="head-left">
        <button class="toggle-sidebar" title="切换侧边栏" @click="emit('toggle-sidebar')">
          <svg v-if="sidebarCollapsed" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <path d="M15 3v18"/>
          </svg>
          <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <path d="M9 3v18"/>
          </svg>
        </button>
        <div class="title">
        <span
          class="dot"
          :class="{ on: aiOnline && !keyMissing, off: !aiOnline, warn: aiOnline && keyMissing }"
          :title="aiOnline
            ? (keyMissing ? 'AI 在线，但未配置 API Key' : 'AI 模块在线')
            : 'AI 模块不可达（每 30 秒自动重试）'"
        ></span>
        {{ conv ? conv.title || '新对话' : 'AI 智能辅助编程' }}
        </div>
      </div>
      <div class="head-right">
        <button class="head-new" :disabled="sending || uploading" @click="emit('new')">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          新对话
        </button>
        <button class="toggle-sidebar" title="切换工作区面板" @click="emit('toggle-workspace')">
          <svg v-if="workspaceCollapsed" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <path d="M9 3v18"/>
          </svg>
          <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <path d="M15 3v18"/>
          </svg>
        </button>
      </div>
    </header>

    <div v-if="!aiOnline" class="warn warn-offline">
      ⚠️ AI 服务当前不可达，发送消息将会失败。请稍后重试，或联系管理员检查 AI 服务是否已启动。
    </div>

    <div ref="scroller" class="messages">
      <div v-if="!messages.length" class="empty-state">
        <div class="empty-logo">AI</div>
        <h2>AI 智能辅助编程</h2>
        <p>在下方输入你的编程问题，AI 将为你解答并提供可运行代码。</p>
      </div>
      <template v-else>
        <MessageItem
          v-for="(m, i) in messages"
          :key="m._key ?? m.id ?? i"
          :message="m"
          @retry="emit('retry', $event)"
        />
        <!-- 图片上传阶段：与"AI 思考中"区分，避免用户误以为模型无响应 -->
        <TypingIndicator v-if="uploading" label="正在上传图片…" />
        <TypingIndicator
          v-else-if="sending && waitingFirstToken"
          :thinking="pendingThinking"
        />
      </template>
    </div>

    <ChatInput
      ref="chatInput"
      :sending="sending"
      @send="(t) => emit('send', t)"
      @stop="emit('stop')"
    />

    <!-- 拖拽图片进入聊天区时的全屏放置提示 -->
    <Transition name="drop-fade">
      <div v-if="dragActive" class="drop-overlay">
        <div class="drop-card">
          <span class="drop-ic">
            <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <path d="m17 8-5-5-5 5" />
              <path d="M12 3v12" />
            </svg>
          </span>
          <b>松开鼠标，添加图片</b>
          <small>JPEG / PNG / GIF / WebP，最多 4 张，自动压缩至最长边 2048px</small>
        </div>
      </div>
    </Transition>
  </main>
</template>

<style scoped>
.chat {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg);
}

/* 拖拽图片放置提示遮罩（整个聊天区均可放置） */
.drop-overlay {
  position: absolute;
  inset: 10px;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px dashed var(--accent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--accent) 8%, var(--surface));
  backdrop-filter: blur(2px);
  /* 不拦截拖拽事件，enter/leave 计数不会因遮罩自身而抖动 */
  pointer-events: none;
}
.drop-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 28px 40px;
  border-radius: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-lg, 0 12px 32px -8px rgba(15, 23, 42, 0.25));
  color: var(--accent);
  animation: drop-pulse 1.1s ease-in-out infinite;
}
.drop-ic {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 60px;
  height: 60px;
  border-radius: 50%;
  background: var(--accent-soft);
}
.drop-card b {
  font-size: 16px;
  color: var(--text);
}
.drop-card small {
  font-size: 12.5px;
  color: var(--muted);
}
@keyframes drop-pulse {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.04);
  }
}
.drop-fade-enter-active,
.drop-fade-leave-active {
  transition: opacity 0.15s ease;
}
.drop-fade-enter-active .drop-card,
.drop-fade-leave-active .drop-card {
  transition: transform 0.15s ease;
}
.drop-fade-enter-from,
.drop-fade-leave-to {
  opacity: 0;
}
.drop-fade-enter-from .drop-card,
.drop-fade-leave-to .drop-card {
  transform: scale(0.95);
}
@media (prefers-reduced-motion: reduce) {
  .drop-card {
    animation: none;
  }
}
.chat-head {
  height: var(--header-h);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 22px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  gap: 12px;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.toggle-sidebar {
  width: 32px;
  height: 32px;
  border: none;
  background: transparent;
  color: var(--muted);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all 0.15s;
}
.toggle-sidebar:hover {
  background: var(--surface-2);
  color: var(--accent);
}
.title {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #d4d7dd;
  flex-shrink: 0;
}
.dot.on {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.18);
}
.dot.off {
  background: #ef4444;
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.16);
}
.dot.warn {
  background: #f59e0b;
  box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.18);
}
.head-new {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--accent);
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  transition: all 0.15s;
}
.head-new:hover {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.head-new:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.warn {
  margin: 12px 22px 0;
  padding: 10px 14px;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  color: #9a3412;
  border-radius: var(--radius-sm);
  font-size: 13px;
}
.warn-offline {
  background: #fef2f2;
  border-color: #fecaca;
  color: #991b1b;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 26px 0 10px;
}
.messages :deep(.msg) {
  padding-left: 10%;
  padding-right: 10%;
}
.empty-state {
  text-align: center;
  padding: 90px 20px;
  color: var(--muted);
}
.empty-logo {
  width: 64px;
  height: 64px;
  margin: 0 auto 18px;
  border-radius: 18px;
  background: linear-gradient(135deg, #4f6ef7, #7c3aed);
  color: #fff;
  font-weight: 700;
  font-size: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-lg);
}
.empty-state h2 {
  margin: 0 0 8px;
  font-size: 22px;
  color: var(--text);
}
.empty-state p {
  margin: 0;
  font-size: 14px;
}
</style>
