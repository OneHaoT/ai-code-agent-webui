<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useToast } from '../composables/useToast.js'
import { compressImage } from '../utils/imageCompress.js'

const props = defineProps({
  sending: { type: Boolean, default: false },
  // 阶段4C 功能开关全量状态（App.vue 持有；null = 未加载/AI 不可用）
  features: { type: Object, default: null }
})
const emit = defineEmits(['send', 'stop', 'toggle-feature', 'save-mcp-servers'])

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

// ---------------- 阶段4C：功能开关 pills（状态以后端为准，不写 localStorage） ----------------

const featuresReady = computed(() => !!props.features)
const mcpOn = computed(() => !!props.features?.mcp_enabled)
const delegateOn = computed(() => !!props.features?.multi_agent_enabled)
const featureDisabled = computed(() => props.sending || !featuresReady.value)

// MCP 配置弹层（自绘，禁原生弹窗）：text 为 textarea 内容、error 为校验/后端错误
const mcpDialog = ref({ show: false, text: '', error: '', saving: false })

function openMcpDialog() {
  mcpDialog.value = {
    show: true,
    saving: false,
    error: '',
    // 预填当前生效配置（格式化缩进，便于直接编辑）
    text: JSON.stringify(props.features?.mcp_servers ?? [], null, 2)
  }
}

function closeMcpDialog() {
  if (mcpDialog.value.saving) return
  mcpDialog.value.show = false
}

function saveMcpDialog() {
  if (mcpDialog.value.saving) return
  let parsed
  try {
    parsed = JSON.parse(mcpDialog.value.text || '[]')
  } catch (e) {
    mcpDialog.value.error = `JSON 格式错误：${e.message}`
    return
  }
  if (!Array.isArray(parsed)) {
    mcpDialog.value.error = '必须是 JSON 数组（每条为 {name, command, args, env} 对象），空配置填 []'
    return
  }
  mcpDialog.value.error = ''
  mcpDialog.value.saving = true
  // 保存结果由 App.vue 回调：成功关弹层，失败保留内容并显示后端 400 原因
  emit('save-mcp-servers', {
    servers: parsed,
    onDone: () => {
      mcpDialog.value.saving = false
      mcpDialog.value.show = false
    },
    onFail: (msg) => {
      mcpDialog.value.saving = false
      mcpDialog.value.error = msg || '保存失败，请稍后重试'
    }
  })
}

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

    <!-- 模式工具行：深度思考 / MCP 工具 / 任务委派 -->
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

      <button
        type="button"
        class="think-chip"
        :class="{ on: mcpOn }"
        :disabled="featureDisabled"
        :aria-pressed="mcpOn"
        :title="!featuresReady
          ? '功能状态加载中或 AI 模块不可用'
          : (mcpOn ? 'MCP 工具已开启（点击齿轮配置 server，点击关闭）' : '开启后 AI 可调用外部 MCP server 工具')"
        @click="emit('toggle-feature', 'mcp_enabled', !mcpOn)"
      >
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 6h-8l-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2z" />
          <path d="M14 13a2 2 0 1 0 4 0 2 2 0 0 0-4 0z" />
        </svg>
        <span>MCP 工具</span>
      </button>
      <!-- 配置入口：仅 MCP 开启时显示 -->
      <button
        v-if="mcpOn"
        type="button"
        class="chip-gear"
        :disabled="featureDisabled"
        title="配置 MCP server（JSON）"
        aria-label="配置 MCP server"
        @click="openMcpDialog"
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>

      <button
        type="button"
        class="think-chip"
        :class="{ on: delegateOn }"
        :disabled="featureDisabled"
        :aria-pressed="delegateOn"
        :title="!featuresReady
          ? '功能状态加载中或 AI 模块不可用'
          : (delegateOn ? '任务委派已开启（复杂任务 AI 可委派只读子代理调研）' : '开启后 AI 可将调研类子任务委派给内部子代理')"
        @click="emit('toggle-feature', 'multi_agent_enabled', !delegateOn)"
      >
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="6" cy="4" r="2.2" />
          <circle cx="18" cy="9" r="2.2" />
          <circle cx="12" cy="19" r="2.2" />
          <path d="M7.8 5.2 16 8.2M16.8 10.8 13 17.2M4.6 6.1 10.8 17.2" />
        </svg>
        <span>任务委派</span>
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

    <!-- MCP server 配置弹层（自绘，trustDialog 同款遮罩模式，禁原生弹窗） -->
    <Transition name="fade">
      <div v-if="mcpDialog.show" class="mcp-backdrop" @click.self="closeMcpDialog">
        <div class="mcp-dialog" role="dialog" aria-label="MCP server 配置" @click.stop>
          <div class="mcp-title">MCP server 配置</div>
          <p class="mcp-desc">
            JSON 数组，每条：<code>{name, command, args, env}</code>（name/command 必填）。
            保存后立即热连接；server 以当前用户权限运行，请只配置可信来源。
          </p>
          <textarea
            v-model="mcpDialog.text"
            class="mcp-textarea"
            rows="10"
            spellcheck="false"
            :disabled="mcpDialog.saving"
            @keydown.esc.prevent="closeMcpDialog"
          ></textarea>
          <p v-if="mcpDialog.error" class="mcp-error" role="alert">{{ mcpDialog.error }}</p>
          <div class="mcp-actions">
            <button type="button" class="btn-cancel" :disabled="mcpDialog.saving" @click="closeMcpDialog">
              取消
            </button>
            <button type="button" class="btn-confirm" :disabled="mcpDialog.saving" @click="saveMcpDialog">
              {{ mcpDialog.saving ? '保存中…' : '保存并连接' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
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
/* MCP「配置」齿轮小圆钮（与 pill 同高，仅开启时显示） */
.chip-gear {
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.15s;
}
.chip-gear:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  transform: rotate(30deg);
}
.chip-gear:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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

/* ---------------- MCP server 配置弹层（自绘） ---------------- */
.mcp-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}
.mcp-dialog {
  background: var(--surface);
  border-radius: 12px;
  box-shadow: var(--shadow-lg, 0 20px 50px -12px rgba(15, 23, 42, 0.3));
  width: 560px;
  max-width: calc(100vw - 40px);
  padding: 20px 20px 16px;
}
.mcp-title {
  font-size: 15.5px;
  font-weight: 600;
  color: var(--text);
}
.mcp-desc {
  margin: 8px 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}
.mcp-desc code {
  background: var(--surface-2, #f0f2f5);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 11.5px;
}
.mcp-textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font-family: Consolas, Monaco, monospace;
  font-size: 12.5px;
  line-height: 1.55;
  padding: 10px 12px;
  outline: none;
  transition: border-color 0.15s;
}
.mcp-textarea:focus {
  border-color: var(--accent);
}
.mcp-textarea:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.mcp-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: #dc2626;
  word-break: break-all;
}
.mcp-actions {
  margin-top: 14px;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}
.btn-cancel {
  padding: 8px 18px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  border-radius: var(--radius-sm, 8px);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-cancel:hover:not(:disabled) {
  background: var(--surface-2, #f0f2f5);
}
.btn-confirm {
  padding: 8px 18px;
  border: none;
  background: var(--accent);
  color: #fff;
  border-radius: var(--radius-sm, 8px);
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-confirm:hover:not(:disabled) {
  background: var(--accent-hover, var(--accent));
}
.btn-confirm:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 弹层过渡 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.16s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .plus svg,
  .chip-gear,
  .menu-enter-active,
  .menu-leave-active,
  .hint-fade-enter-active,
  .hint-fade-leave-active,
  .fade-enter-active,
  .fade-leave-active {
    transition: none;
  }
}
</style>
