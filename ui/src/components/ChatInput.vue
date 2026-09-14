<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useToast } from '../composables/useToast.js'
import { compressImage } from '../utils/imageCompress.js'

const props = defineProps({
  sending: { type: Boolean, default: false }
})
const emit = defineEmits(['send', 'stop'])

const toast = useToast()

const MAX_IMAGES = 4
const MAX_SIZE = 10 * 1024 * 1024 // 10MB
const ACCEPTED = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']

const text = ref('')
const ta = ref(null)
const fileInput = ref(null)
const plusBtn = ref(null)
const menuOpen = ref(false)

// 待发送图片：{ file: File, url: objectURL }
const attachments = ref([])

// 深度思考开关（DeepSeek thinking 模式），选择持久化到 localStorage
const thinking = ref(localStorage.getItem('ai-thinking-enabled') === '1')
watch(thinking, (v) => localStorage.setItem('ai-thinking-enabled', v ? '1' : '0'))

const canSend = computed(
  () => !props.sending && (!!text.value.trim() || attachments.value.length > 0)
)

// 发送中按钮承担"停止生成"职责，任何时候发送流程进行中都可点击
const sendBtnDisabled = computed(() => !props.sending && !canSend.value)

function onSendClick() {
  if (props.sending) {
    emit('stop')
  } else {
    submit()
  }
}

async function autoGrow() {
  await nextTick()
  if (ta.value) {
    ta.value.style.height = 'auto'
    ta.value.style.height = Math.min(ta.value.scrollHeight, 200) + 'px'
  }
}

// ---------------- + 号菜单 ----------------

function toggleMenu() {
  if (props.sending) return
  menuOpen.value = !menuOpen.value
}

function onDocClick(e) {
  if (
    menuOpen.value &&
    plusBtn.value &&
    !plusBtn.value.contains(e.target)
  ) {
    menuOpen.value = false
  }
}

function pickImage() {
  menuOpen.value = false
  fileInput.value?.click()
}

function onFilesChosen(e) {
  const files = Array.from(e.target.files || [])
  addFiles(files)
  // 允许重复选择同一文件
  e.target.value = ''
}

/**
 * 添加待发送图片：格式校验 -> 串行压缩 -> 压缩后体积校验 -> 生成预览。
 * 串行 await 压缩避免多张图同时解码造成内存峰值。
 */
async function addFiles(files) {
  if (props.sending) return
  for (const raw of files) {
    if (attachments.value.length >= MAX_IMAGES) {
      toast.info(`最多添加 ${MAX_IMAGES} 张图片`)
      break
    }
    if (!ACCEPTED.includes(raw.type)) {
      toast.error(`不支持的图片格式：${raw.name}（仅支持 JPEG/PNG/GIF/WebP）`)
      continue
    }

    let file = raw
    try {
      file = await compressImage(raw)
    } catch {
      file = raw // 压缩异常不应阻断，回退原图
    }

    // 以压缩后的体积为准（GIF 不压缩，仍按原始大小校验）
    if (file.size > MAX_SIZE) {
      toast.error(`图片 ${file.name} 压缩后仍超过 10MB，无法发送`)
      continue
    }
    attachments.value.push({ file, url: URL.createObjectURL(file) })
  }
}

function removeAtt(index) {
  const [item] = attachments.value.splice(index, 1)
  if (item) URL.revokeObjectURL(item.url)
}

function clearAttachments() {
  attachments.value.forEach((a) => URL.revokeObjectURL(a.url))
  attachments.value = []
}

// 粘贴图片（拖拽由外层 ChatView 统一接收后调用 addFiles）
function onPaste(e) {
  const files = Array.from(e.clipboardData?.files || []).filter((f) =>
    f.type.startsWith('image/')
  )
  if (files.length) addFiles(files)
}

// 暴露给父组件：拖放到聊天区域的文件经 ChatView 转交
defineExpose({ addFiles })

// ---------------- 发送 ----------------

function submit() {
  const v = text.value.trim()
  if (!canSend.value) return
  emit('send', {
    text: v,
    files: attachments.value.map((a) => a.file),
    thinking: thinking.value
  })
  text.value = ''
  clearAttachments()
  autoGrow()
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  } else if (e.key === 'Escape') {
    menuOpen.value = false
  }
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  clearAttachments()
})
</script>

<template>
  <div class="input-bar">
    <!-- 待发送图片预览 -->
    <div v-if="attachments.length" class="attach-row">
      <div v-for="(a, i) in attachments" :key="a.url" class="attach">
        <img :src="a.url" :alt="a.file.name" />
        <button
          type="button"
          class="attach-x"
          title="移除图片"
          :disabled="sending"
          @click="removeAtt(i)"
        >
          <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
    </div>

    <!-- 模式工具行：深度思考开关 -->
    <div class="toolbar">
      <button
        type="button"
        class="think-chip"
        :class="{ on: thinking }"
        :disabled="sending"
        :aria-pressed="thinking"
        :title="thinking ? '深度思考已开启（复杂推理更准，但响应更慢）' : '开启深度思考'"
        @click="thinking = !thinking"
      >
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0L14.06 8.5A2 2 0 0 0 15.5 9.94l6.14 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z" />
        </svg>
        <span>深度思考</span>
      </button>
      <Transition name="hint-fade">
        <span v-if="thinking" class="think-hint" aria-live="polite">
          已开启：AI 会先推理再作答，复杂问题更准，但耗时更长
        </span>
      </Transition>
    </div>

    <div class="input-wrap">
      <!-- + 号按钮（集成附件菜单） -->
      <div class="plus-wrap" ref="plusBtn">
        <button
          type="button"
          class="plus"
          :class="{ active: menuOpen }"
          :disabled="sending"
          title="添加内容"
          @click="toggleMenu"
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.2"
            stroke-linecap="round"
            :style="{ transform: menuOpen ? 'rotate(45deg)' : 'none' }"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
        <Transition name="menu">
          <div v-if="menuOpen" class="plus-menu">
            <button type="button" class="menu-item" @click="pickImage">
              <span class="menu-ic">
                <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <circle cx="9" cy="9" r="1.6" />
                  <path d="m21 15-4.5-4.5L6 21" />
                </svg>
              </span>
              <span class="menu-text">
                <b>上传图片</b>
                <small>JPEG / PNG / GIF / WebP，最多 {{ MAX_IMAGES }} 张，自动压缩</small>
              </span>
            </button>
          </div>
        </Transition>
      </div>

      <input
        ref="fileInput"
        type="file"
        accept="image/jpeg,image/png,image/gif,image/webp"
        multiple
        hidden
        @change="onFilesChosen"
      />

      <textarea
        ref="ta"
        v-model="text"
        class="ta"
        rows="1"
        placeholder="输入问题，可上传图片，Enter 发送 / Shift+Enter 换行"
        @input="autoGrow"
        @keydown="onKeydown"
        @paste="onPaste"
      ></textarea>

      <button
        class="send"
        :class="{ stopping: sending }"
        :disabled="sendBtnDisabled"
        :title="sending ? '停止生成（后台仍会完成本次回答）' : '发送'"
        @click="onSendClick"
      >
        <span v-if="sending" class="stop-icon" aria-hidden="true"></span>
        <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      </button>
    </div>
    <div class="hint">AI 辅助编程 · 基于 DeepSeek，回答仅供参考 · 支持拖拽 / 粘贴图片</div>
  </div>
</template>

<style scoped>
.input-bar {
  border-top: 1px solid var(--border);
  background: var(--surface);
  padding: 14px 0 12px;
}

/* 待发送图片缩略图 */
.attach-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 4px 10px;
}

/* 模式工具行 */
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 4px 8px;
}
.think-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}
.think-chip:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.think-chip.on {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.think-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.think-hint {
  font-size: 11.5px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.hint-fade-enter-active,
.hint-fade-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.hint-fade-enter-from,
.hint-fade-leave-to {
  opacity: 0;
  transform: translateX(-4px);
}
.attach {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--border);
  background: var(--surface-2, #f0f2f5);
}
.attach img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.attach-x {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: rgba(17, 24, 39, 0.62);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s;
}
.attach-x:hover {
  background: rgba(17, 24, 39, 0.85);
}
.attach-x:disabled {
  cursor: not-allowed;
}

.input-wrap {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 8px 8px 8px 10px;
  box-shadow: var(--shadow);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.input-wrap:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

/* + 号与弹出菜单 */
.plus-wrap {
  position: relative;
  flex-shrink: 0;
}
.plus {
  width: 38px;
  height: 38px;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface);
  color: #5b6472;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.18s;
}
.plus svg {
  transition: transform 0.2s ease;
}
.plus:hover:not(:disabled),
.plus.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.plus:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.plus-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 10px);
  width: 248px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 32px -8px rgba(15, 23, 42, 0.22),
    0 2px 8px -2px rgba(15, 23, 42, 0.08);
  padding: 6px;
  z-index: 50;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: none;
  border-radius: 9px;
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: background 0.13s;
}
.menu-item:hover {
  background: var(--surface-2, #f0f2f5);
}
.menu-ic {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
}
.menu-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.menu-text b {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}
.menu-text small {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}

.menu-enter-active,
.menu-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
  transform-origin: bottom left;
}
.menu-enter-from,
.menu-leave-to {
  opacity: 0;
  transform: translateY(6px) scale(0.96);
}

.ta {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  font-family: inherit;
  font-size: 14.5px;
  line-height: 1.5;
  max-height: 200px;
  background: transparent;
  color: var(--text);
  padding: 6px 0;
}
.ta::placeholder {
  color: var(--muted);
}
.send {
  flex-shrink: 0;
  width: 38px;
  height: 38px;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.send:disabled {
  background: #d4d7dd;
  cursor: not-allowed;
}
.send:not(:disabled):hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
/* 发送中：发送钮变为停止钮（实心方块） */
.stop-icon {
  width: 13px;
  height: 13px;
  border-radius: 3px;
  background: #fff;
}
.send.stopping {
  background: #ef4444;
}
.send.stopping:not(:disabled):hover {
  background: #dc2626;
}
.hint {
  text-align: center;
  font-size: 11.5px;
  color: var(--muted);
  margin-top: 8px;
}

@media (prefers-reduced-motion: reduce) {
  .plus svg,
  .menu-enter-active,
  .menu-leave-active,
  .hint-fade-enter-active,
  .hint-fade-leave-active {
    transition: none;
  }
}
</style>
