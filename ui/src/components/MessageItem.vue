<script setup>
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import ToolStepList from './ToolStepList.vue'
import ConfirmCard from './ConfirmCard.vue'

const props = defineProps({
  message: { type: Object, required: true }
})
const emit = defineEmits(['retry', 'confirm-decide'])

const isUser = computed(() => props.message.role === 'user')

// 思考过程默认折叠，点击标题栏展开（非流式：到达时思考已完成）
const reasoningOpen = ref(false)
// 流式生成思维链期间强制展开，结束后恢复用户选择（默认收起）
const reasoningStreaming = computed(
  () => !!props.message.streaming && !!(props.message.reasoning && props.message.reasoning.length)
)
const reasoningOpenEffective = computed(() =>
  reasoningStreaming.value ? true : reasoningOpen.value
)
const reasoningBody = ref(null)
watch(
  () => props.message.reasoning?.length || 0,
  () => {
    // 生成中始终把最新一行滚到可视区底部
    if (reasoningStreaming.value && reasoningBody.value) {
      reasoningBody.value.scrollTop = reasoningBody.value.scrollHeight
    }
  }
)

// AI 回答渲染 Markdown(含代码块)，并做安全过滤
const rendered = computed(() => {
  if (isUser.value) return ''
  const html = marked.parse(props.message.content || '', { breaks: true })
  return DOMPurify.sanitize(html)
})

// 用户消息纯文本保留换行
const userText = computed(() =>
  (props.message.content || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>')
)

const images = computed(() => props.message.images || [])

// 气泡本体是否有内容（错误/正文/思维链/工具轨迹）；用户消息恒为 true
const showBubble = computed(
  () => isUser.value ||
    !!props.message.error ||
    !!(props.message.content && props.message.content.length) ||
    !!(props.message.reasoning && props.message.reasoning.length) ||
    !!(props.message.toolTrace && props.message.toolTrace.length)
)

// 整条消息是否渲染：主动停止但首 token 前（无气泡）时仍需显示"已停止"提示
const hasBubble = computed(() => showBubble.value || !!props.message.stopped)

// 正文是否还在流式输出（用于末尾光标）
const contentStreaming = computed(
  () => !!props.message.streaming && !!(props.message.content && props.message.content.length)
)

// 服务端返回的图片走 /api/images/{id}（Vite 代理同源）；
// 刚发送、上传完成前的乐观渲染用本地 objectURL（img.url）
function imgSrc(img) {
  return img.url || `/api/images/${img.id}`
}

// ---------------- 图片灯箱（替代新开浏览器标签） ----------------
const lightboxSrc = ref(null)
function openLightbox(img) {
  lightboxSrc.value = imgSrc(img)
  window.addEventListener('keydown', onLightboxKey)
}
function closeLightbox() {
  lightboxSrc.value = null
  window.removeEventListener('keydown', onLightboxKey)
}
function onLightboxKey(e) {
  if (e.key === 'Escape') closeLightbox()
}

// ---------------- 消息时间戳 ----------------
function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const p = (n) => String(n).padStart(2, '0')
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`
  return sameDay ? hm : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`
}
const timeText = computed(() => formatTime(props.message.createdAt))

// ---------------- 代码块复制按钮（事件节点为 marked 注入，需 post 渲染后补） ----------------
const markdownBody = ref(null)
function injectCopyButtons() {
  const root = markdownBody.value
  if (!root) return
  root.querySelectorAll('pre').forEach((pre) => {
    if (pre.querySelector(':scope > .code-copy')) return
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'code-copy'
    btn.textContent = '复制'
    btn.addEventListener('click', async () => {
      const code = pre.querySelector('code')
      const text = code ? code.innerText : pre.innerText
      try {
        await navigator.clipboard.writeText(text)
        btn.textContent = '已复制'
        btn.classList.add('ok')
        setTimeout(() => {
          btn.textContent = '复制'
          btn.classList.remove('ok')
        }, 1500)
      } catch (_) {
        // 剪贴板权限/非安全上下文：静默失败，用户仍可手动框选
      }
    })
    pre.appendChild(btn)
  })
}
// 流式期间 rendered 每 token 变化，innerHTML 重建后需要重新注入。
// immediate 必须有：历史对话加载（刷新/切换）时组件创建即带完整正文，
// rendered 没有"变化"过程，非 immediate 的 watcher 永远不会触发
watch(
  rendered,
  async () => {
    await nextTick()
    injectCopyButtons()
  },
  { flush: 'post', immediate: true }
)
onBeforeUnmount(() => window.removeEventListener('keydown', onLightboxKey))
</script>

<template>
  <!-- 空流式占位（首 token 前、无错误）：整条隐藏，由 ChatView 的 TypingIndicator 唯一占位，
       避免出现两组头像+昵称 -->
  <div v-if="hasBubble" class="msg" :class="isUser ? 'user' : 'assistant'">
    <div class="avatar" :class="isUser ? 'user' : 'ai'">
      <span v-if="isUser">我</span>
      <span v-else>AI</span>
    </div>
    <div class="bubble-wrap">
      <div class="who">
        <span>{{ isUser ? '你' : 'AI 助手' }}</span>
        <span v-if="timeText" class="time">{{ timeText }}</span>
      </div>
      <div
        v-if="showBubble"
        class="bubble"
        :class="{
          err: message.error,
          failed: isUser && message.failed,
          'img-only': isUser && images.length && !message.content
        }"
      >
        <div v-if="isUser && images.length" class="img-grid" :class="`n${Math.min(images.length, 4)}`">
          <button
            v-for="(img, i) in images"
            :key="img.id || img.url || i"
            type="button"
            class="img-cell"
            :title="img.filename ? `查看大图：${img.filename}` : '查看大图'"
            @click="openLightbox(img)"
          >
            <img :src="imgSrc(img)" :alt="img.filename || '图片'" loading="lazy" />
          </button>
        </div>
        <!-- 深度思考过程（可折叠；流式生成中自动展开并标注"生成中"） -->
        <div
          v-if="!isUser && message.reasoning && !message.error"
          class="reasoning"
          :class="{ open: reasoningOpenEffective, streaming: reasoningStreaming }"
        >
          <button
            type="button"
            class="reasoning-head"
            :aria-expanded="reasoningOpenEffective"
            @click="reasoningOpen = !reasoningOpen"
          >
            <svg class="chev" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
            <svg class="spark" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0L14.06 8.5A2 2 0 0 0 15.5 9.94l6.14 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z" />
            </svg>
            <span>{{ reasoningStreaming ? '深度思考（生成中…）' : '深度思考' }}</span>
            <span v-if="reasoningStreaming" class="reasoning-dot" aria-hidden="true"></span>
          </button>
          <div
            v-show="reasoningOpenEffective"
            ref="reasoningBody"
            class="reasoning-body"
          >{{ message.reasoning }}</div>
        </div>
        <!-- 只读工具调用轨迹（流式实时更新；历史消息由 toolTrace 持久化还原） -->
        <ToolStepList
          v-if="!isUser && !message.error && message.toolTrace && message.toolTrace.length"
          :steps="message.toolTrace"
        />
        <!-- 阶段3：写/执行工具人机确认卡（confirm 帧驱动，仅流式期间存在，不落库） -->
        <ConfirmCard
          v-if="!isUser && !message.error && message.confirmRequest"
          :request="message.confirmRequest"
          @decide="(approved) => emit('confirm-decide', message, approved)"
        />
        <div v-if="isUser && message.content" class="markdown" v-html="userText"></div>
        <div v-else-if="!isUser" ref="markdownBody" class="markdown">
          <span v-html="rendered"></span><span
            v-if="contentStreaming"
            class="stream-caret"
            aria-hidden="true"
          ></span>
        </div>
      </div>
      <!-- 乐观消息发送失败：后端已回滚，本条未落库（刷新后消失）；可一键重试 -->
      <div v-if="isUser && message.failed" class="send-failed-row">
        <span class="send-failed-tip">⚠ 发送失败，本条内容未保存</span>
        <button
          type="button"
          class="retry-btn"
          title="按原内容（含图片）重新发送"
          @click="emit('retry', message)"
        >
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-2.64-6.36" />
            <path d="M21 3v6h-6" />
          </svg>
          重试
        </button>
      </div>
      <!-- 用户主动停止：保留半截回复，提示后台仍会完成 -->
      <div v-if="!isUser && message.stopped" class="stopped-tip">
        已停止生成（后台仍会完成本次回答，重新打开对话可查看完整内容）
      </div>
    </div>

    <!-- 大图灯箱：点遮罩/按 Esc 关闭（Teleport 脱离气泡 overflow 裁剪） -->
    <Teleport to="body">
      <div
        v-if="lightboxSrc"
        class="lightbox-mask"
        @click.self="closeLightbox"
      >
        <img class="lightbox-img" :src="lightboxSrc" alt="大图预览" @click.stop />
        <button
          type="button"
          class="lightbox-close"
          title="关闭（Esc）"
          @click.stop="closeLightbox"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.msg {
  display: flex;
  gap: 12px;
  margin-bottom: 22px;
  align-items: flex-start;
}
.msg.user {
  flex-direction: row-reverse;
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
}
.avatar.user {
  background: var(--accent-soft);
  color: var(--accent);
}
.avatar.ai {
  background: linear-gradient(135deg, #4f6ef7, #7c3aed);
  color: #fff;
}
.bubble-wrap {
  max-width: 78%;
  min-width: 0;
}
.msg.user .bubble-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}
.who {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 5px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.who .time {
  font-size: 10.5px;
  opacity: 0.75;
  font-variant-numeric: tabular-nums;
}
.bubble {
  padding: 12px 16px;
  border-radius: 14px;
  font-size: 14.5px;
  line-height: 1.65;
  box-shadow: var(--shadow);
}
.msg.assistant .bubble {
  background: var(--surface);
  border: 1px solid var(--border);
  border-top-left-radius: 4px;
}
.msg.user .bubble {
  background: var(--accent);
  color: #fff;
  border-top-right-radius: 4px;
}
.msg.user .bubble :deep(.markdown code) {
  background: rgba(255, 255, 255, 0.18);
}
.bubble.err {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: #f5c6c6;
}

/* 深度思考过程（assistant 气泡内，独立可折叠块） */
.reasoning {
  margin-bottom: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2, #f5f6f8);
  overflow: hidden;
}
.reasoning-head {
  display: flex;
  align-items: center;
  gap: 5px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  background: transparent;
  color: var(--muted);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  text-align: left;
}
.reasoning-head:hover {
  color: var(--accent);
}
.reasoning-head .chev {
  transition: transform 0.18s ease;
}
.reasoning.open .reasoning-head .chev {
  transform: rotate(90deg);
}
.reasoning-head .spark {
  color: var(--accent);
}
.reasoning-body {
  padding: 0 12px 10px 28px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow-y: auto;
}

/* "深度思考（生成中…）"标题后的呼吸圆点 */
.reasoning-dot {
  width: 6px;
  height: 6px;
  margin-left: 2px;
  border-radius: 50%;
  background: var(--accent);
  animation: reasoning-pulse 1s ease-in-out infinite;
}
@keyframes reasoning-pulse {
  0%,
  100% {
    opacity: 0.25;
    transform: scale(0.85);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

/* 流式正文末尾的闪烁光标 */
.stream-caret {
  display: inline-block;
  width: 7px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: text-bottom;
  border-radius: 1px;
  background: var(--accent);
  animation: caret-blink 0.9s steps(1) infinite;
}
@keyframes caret-blink {
  0%,
  50% {
    opacity: 1;
  }
  50.01%,
  100% {
    opacity: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .reasoning-dot,
  .stream-caret {
    animation: none;
  }
}

/* 用户消息中的图片 */
.img-grid {
  display: grid;
  gap: 6px;
  margin-bottom: 8px;
  max-width: 280px;
  grid-template-columns: 1fr;
}
.img-grid.n2,
.img-grid.n4 {
  grid-template-columns: 1fr 1fr;
  max-width: 240px;
}
.img-grid.n3 {
  grid-template-columns: 1fr 1fr;
  max-width: 240px;
}
.img-cell {
  display: block;
  width: 100%;
  padding: 0;
  border: none;
  font: inherit;
  border-radius: 10px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.16);
  line-height: 0;
  cursor: zoom-in;
}
.img-cell img {
  width: 100%;
  height: 116px;
  object-fit: cover;
  display: block;
  transition: transform 0.18s ease;
}
.img-grid.n1 .img-cell img {
  height: 170px;
}
.img-cell:hover img {
  transform: scale(1.03);
}
/* 发送失败的用户气泡：红色警示（覆盖正常蓝色；放在 img-only 之后保证优先级） */
.msg.user .bubble.failed {
  background: var(--danger-soft);
  color: var(--danger);
  border: 1px solid #f5c6c6;
}
.send-failed-row {
  margin-top: 5px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.send-failed-tip {
  font-size: 11.5px;
  color: var(--danger);
}
.retry-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 9px;
  border: 1px solid #f5c6c6;
  border-radius: 999px;
  background: var(--surface);
  color: var(--danger);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}
.retry-btn:hover {
  background: var(--danger);
  color: #fff;
  border-color: var(--danger);
}
.stopped-tip {
  margin-top: 5px;
  font-size: 11.5px;
  color: var(--muted);
}

/* 纯图片气泡：去掉多余内边距与文字间距 */
.bubble.img-only {
  padding: 6px;
  background: transparent;
  box-shadow: none;
}
.bubble.img-only .img-grid {
  margin-bottom: 0;
}
.bubble.img-only .img-cell {
  border: 1px solid rgba(255, 255, 255, 0.35);
}

/* 代码块复制按钮：节点由 JS 注入 marked 产物，无 scope hash，必须走 :deep */
.markdown :deep(pre) {
  position: relative;
}
.markdown :deep(.code-copy) {
  position: absolute;
  top: 7px;
  right: 7px;
  height: 24px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 6px;
  background: rgba(45, 48, 60, 0.85);
  color: rgba(255, 255, 255, 0.82);
  font-size: 11.5px;
  font-family: inherit;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s;
}
.markdown :deep(pre:hover .code-copy),
.markdown :deep(.code-copy.ok) {
  opacity: 1;
}
.markdown :deep(.code-copy:hover) {
  background: rgba(80, 84, 100, 0.95);
}
.markdown :deep(.code-copy.ok) {
  border-color: #34d399;
  color: #34d399;
}

/* 图片灯箱 */
.lightbox-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(15, 17, 22, 0.82);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  animation: lightbox-fade 0.15s ease;
}
@keyframes lightbox-fade {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}
.lightbox-img {
  max-width: 100%;
  max-height: 100%;
  border-radius: 8px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
  cursor: default;
}
.lightbox-close {
  position: absolute;
  top: 20px;
  right: 24px;
  width: 40px;
  height: 40px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s;
}
.lightbox-close:hover {
  background: rgba(255, 255, 255, 0.25);
}
</style>
