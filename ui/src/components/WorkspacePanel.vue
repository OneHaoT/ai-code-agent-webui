<script setup>
import { ref } from 'vue'
import { api } from '../api.js'
import { useToast } from '../composables/useToast.js'

const toast = useToast()

const props = defineProps({
  /** 当前对话绑定的 workspace 绝对路径（null=AI 默认工作区） */
  workspaceRoot: { type: String, default: null },
  /** AI 模块默认工作区（从 /health.default_workspace 读取） */
  defaultWorkspace: { type: String, default: null },
  collapsed: { type: Boolean, default: false }
})
const emit = defineEmits(['toggle', 'workspaceChange'])

// ---- 拖拽覆盖层（整个面板都是 drop zone） ----
const dragState = ref({ active: false, isSingleFile: false, count: 0 })

// ---- 信任对话框（目录 / 多文件 → 取父目录作为 workspace） ----
const trustDialog = ref({
  show: false,
  hint: '',
  path: ''
})

// ---- "选择目录"隐藏 input ----
const dirInput = ref(null)

function getDefaultWs() {
  return props.defaultWorkspace || '(AI 模块默认)'
}

// ============ 拖拽 ============
function onDragEnter(e) {
  if (!e.dataTransfer?.types?.includes('Files')) return
  if (!e.currentTarget?.contains(e.target)) return
  e.preventDefault()
  dragState.value.active = true
  // 临时检查条目数：在 drop 时再精确判断是单文件/多文件/文件夹
  const items = Array.from(e.dataTransfer.items || [])
  dragState.value.count = items.length
  dragState.value.isSingleFile = items.length === 1
}

function onDragOver(e) {
  if (dragState.value.active) e.preventDefault()
}

function onDragLeave(e) {
  if (e.currentTarget === e.target) dragState.value.active = false
}

function onDrop(e) {
  if (!dragState.value.active) return
  dragState.value.active = false
  e.preventDefault()

  const files = e.dataTransfer?.files
  if (!files || !files.length) return

  const items = Array.from(e.dataTransfer.items || [])
  const dirEntry = items.find(
    (it) => it.kind === 'file' && it.webkitGetAsEntry?.()?.isDirectory
  )
  const hasDir = !!dirEntry
  const singleFile = files.length === 1 && !hasDir

  if (singleFile) {
    uploadSingleFile(files[0])
  } else {
    // 提前提取好 folderName / hint，避免在箭头函数里用 arguments
    let hint
    if (hasDir) {
      hint = dirEntry.webkitGetAsEntry()?.name || 'my-project'
    } else if (files.length > 1) {
      hint = `${files.length} 个文件（取父目录）`
    } else {
      hint = files[0]?.name || '项目目录'
    }
    openTrustDialog(hint)
  }
}

// ============ 单文件上传 ============
async function uploadSingleFile(file) {
  try {
    const result = await api.uploadWorkspaceFile(file)
    toast.success(`已保存到默认工作区：${result.name}（${(result.size / 1024).toFixed(1)} KB）`)
    emit('workspaceChange', null)
  } catch (e) {
    toast.error('上传失败：' + (e.message || '未知错误'))
  }
}

// ============ 信任对话框（目录/多文件） ============
function openTrustDialog(hint) {
  trustDialog.value.hint = hint
  trustDialog.value.path = ''
  trustDialog.value.show = true
}

function cancelTrust() {
  trustDialog.value.show = false
}

function confirmTrust() {
  const p = trustDialog.value.path.trim()
  if (!p) return
  emit('workspaceChange', p)
  trustDialog.value.show = false
}

// ============ 选择目录按钮 ============
async function triggerSelectDirectory() {
  // Chromium：File System Access API 可指定起始目录为"下载"，且无需枚举目录内文件
  if (window.showDirectoryPicker) {
    try {
      const handle = await window.showDirectoryPicker({ startIn: 'downloads' })
      trustDialog.value.hint = handle.name || 'my-project'
      trustDialog.value.path = ''
      trustDialog.value.show = true
    } catch {
      // 用户取消选择（AbortError）：静默返回
    }
    return
  }
  // 退化路径：Firefox/Safari 不支持 showDirectoryPicker，且无法控制起始目录
  dirInput.value?.click()
}

function onDirSelected(e) {
  const files = e.target.files
  if (!files || !files.length) return
  // webkitdirectory 会把目录内所有文件扁平列出；用第一个文件的 webkitRelativePath 取目录名
  let folderName = 'my-project'
  for (const f of files) {
    const rel = f.webkitRelativePath || ''
    if (rel) {
      folderName = rel.split('/')[0]
      break
    }
  }
  trustDialog.value.hint = folderName
  trustDialog.value.path = ''
  trustDialog.value.show = true
  // 重置 input 以便下次选同一个目录
  e.target.value = ''
}

// ============ 打开工作区 ============
async function openInExplorer() {
  try {
    await api.openWorkspace()
    toast.info('已请求在系统文件浏览器中打开默认工作区')
  } catch (e) {
    toast.error('打开失败：' + e.message)
  }
}

// ============ 重置 ============
function resetToDefault() {
  emit('workspaceChange', null)
}

function copyToClipboard(text) {
  navigator.clipboard?.writeText(text)
}
</script>

<template>
  <aside
    class="workspace-panel"
    :class="{ collapsed, dragging: dragState.active }"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 拖拽覆盖层 -->
    <Transition name="fade">
      <div v-if="dragState.active" class="drag-overlay">
        <div class="drag-hint">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <div class="drag-title">{{ dragState.isSingleFile ? '松开上传此文件' : '松开以信任该项目' }}</div>
          <div class="drag-sub">{{ dragState.isSingleFile ? '将复制到默认工作区' : '在对话框中确认路径' }}</div>
        </div>
      </div>
    </Transition>

    <div class="panel-header">
      <span class="panel-title">工作区</span>
      <button class="toggle-panel" title="隐藏工作区" @click="emit('toggle')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2"/>
          <path d="M9 3v18"/>
        </svg>
      </button>
    </div>

    <div class="panel-body">
      <!-- 当前工作区状态 -->
      <div class="ws-current">
        <div class="ws-label">当前工作区</div>
        <div v-if="workspaceRoot" class="ws-path">
          <span class="ws-path-text" :title="workspaceRoot">{{ workspaceRoot }}</span>
          <button class="ws-copy" title="复制路径" @click="copyToClipboard(workspaceRoot)">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
          <button class="ws-reset" title="切回默认工作区" @click="resetToDefault">重置</button>
        </div>
        <div v-else class="ws-path ws-default">
          <span class="ws-default-label">默认</span>
          <span class="ws-default-path" :title="getDefaultWs()">{{ getDefaultWs() }}</span>
          <button class="ws-open" title="在系统文件浏览器中打开" @click="openInExplorer">打开</button>
        </div>
      </div>

      <!-- 操作按钮区 -->
      <div class="action-buttons">
        <button class="btn-action btn-primary" @click="triggerSelectDirectory">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          选择目录
        </button>
        <button class="btn-action" @click="openInExplorer">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
            <polyline points="15 3 21 3 21 9"/>
            <line x1="10" y1="14" x2="21" y2="3"/>
          </svg>
          打开工作区
        </button>
      </div>

      <div class="panel-tip">
        拖拽整个面板都可以接收文件/文件夹。
        拖单个文件（pdf、md 等）会自动复制到默认工作区；
        拖文件夹或多个文件会让你填写真实路径作为工作区。
      </div>
    </div>

    <!-- 隐藏的目录选择器 -->
    <input
      ref="dirInput"
      type="file"
      webkitdirectory
      directory
      multiple
      style="display:none"
      @change="onDirSelected"
    />

    <!-- 信任对话框 -->
    <Transition name="fade">
      <div v-if="trustDialog.show" class="trust-backdrop" @click.self="cancelTrust">
        <div class="trust-dialog" @click.stop>
          <div class="trust-header">
            <div class="trust-icon">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
            </div>
            <div>
              <div class="trust-title">信任该项目？</div>
              <div class="trust-folder">{{ trustDialog.hint }}</div>
            </div>
          </div>
          <div class="trust-body">
            <p class="trust-desc">
              AI 将可以读取该项目内所有文件作为工作区。请确认是可信的本地项目。
            </p>
            <label class="trust-label">该项目的绝对路径</label>
            <input
              v-model="trustDialog.path"
              class="trust-input"
              :placeholder="'例如 C:/Users/xxx/Desktop/my-project'"
              @keydown.enter.prevent="confirmTrust"
              @keydown.esc.prevent="cancelTrust"
              autofocus
            />
            <p class="trust-hint">
              浏览器安全限制无法直接获取真实路径，请手动粘贴（建议正斜杠）。
            </p>
          </div>
          <div class="trust-actions">
            <button class="btn-cancel" @click="cancelTrust">取消</button>
            <button class="btn-confirm" :disabled="!trustDialog.path.trim()" @click="confirmTrust">
              信任并使用
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </aside>
</template>

<style scoped>
.workspace-panel {
  width: 280px;
  flex-shrink: 0;
  background: var(--surface);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  transition: width 0.25s ease, border-color 0.25s ease;
  position: relative;
}
.workspace-panel.collapsed {
  width: 0;
  border-left-color: transparent;
}

/* 拖拽覆盖层 */
.drag-overlay {
  position: absolute;
  inset: 0;
  background: rgba(var(--accent-rgb, 59,130,246), 0.08);
  border: 2px dashed var(--accent);
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  backdrop-filter: blur(1px);
}
.drag-hint {
  text-align: center;
  color: var(--accent);
}
.drag-title {
  font-weight: 600;
  font-size: 14px;
  margin-top: 8px;
}
.drag-sub {
  font-size: 12px;
  opacity: 0.7;
  margin-top: 2px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  height: var(--header-h);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
}
.toggle-panel {
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--muted);
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.toggle-panel:hover {
  background: var(--surface-2);
  color: var(--accent);
}

.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

/* 当前工作区 */
.ws-current {
  margin-bottom: 14px;
}
.ws-label {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
  font-weight: 500;
}
.ws-path {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  padding: 8px 10px;
  background: var(--surface-2);
  border-radius: 6px;
  word-break: break-all;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  line-height: 1.5;
}
.ws-path-text {
  flex: 1;
  color: var(--text);
}
.ws-copy, .ws-reset, .ws-open {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--muted);
  padding: 2px 4px;
  border-radius: 4px;
  font-size: 11px;
  transition: all 0.12s;
}
.ws-copy:hover, .ws-reset:hover, .ws-open:hover {
  background: rgba(0,0,0,0.06);
  color: var(--accent);
}
.ws-default {
  flex-direction: column;
  background: var(--accent-soft);
  border: 1px dashed var(--accent);
  position: relative;
}
.ws-default-label {
  font-weight: 600;
  color: var(--accent);
  font-family: inherit;
}
.ws-default-path {
  color: var(--muted);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
}
.ws-open {
  position: absolute;
  top: 4px;
  right: 4px;
  font-size: 11px;
  padding: 2px 8px;
}

/* 操作按钮 */
.action-buttons {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}
.btn-action {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-action:hover {
  background: var(--surface-2);
  border-color: var(--accent);
}
.btn-action.btn-primary {
  background: var(--accent);
  color: #fff;
  border-color: transparent;
}
.btn-action.btn-primary:hover {
  background: var(--accent-hover);
}

.panel-tip {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.7;
}

/* ---------- 信任对话框 ---------- */
.trust-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}
.trust-dialog {
  background: var(--surface);
  border-radius: 12px;
  box-shadow: var(--shadow-lg);
  width: 440px;
  max-width: calc(100vw - 40px);
  overflow: hidden;
}
.trust-header {
  display: flex;
  gap: 12px;
  padding: 20px 20px 12px;
}
.trust-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.trust-title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 2px;
}
.trust-folder {
  font-size: 13px;
  color: var(--muted);
  font-family: 'SFMono-Regular', Consolas, monospace;
}
.trust-body {
  padding: 0 20px 16px;
}
.trust-desc {
  font-size: 13px;
  color: var(--text);
  line-height: 1.6;
  margin: 0 0 12px;
}
.trust-label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
  margin-bottom: 6px;
}
.trust-input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 13px;
  box-sizing: border-box;
  background: var(--surface);
  color: var(--text);
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.trust-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.trust-hint {
  font-size: 11px;
  color: var(--muted);
  margin: 8px 0 0;
  line-height: 1.5;
}
.trust-hint code {
  font-family: 'SFMono-Regular', Consolas, monospace;
  background: var(--surface-2);
  padding: 1px 5px;
  border-radius: 3px;
}
.trust-actions {
  padding: 14px 20px 20px;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  border-top: 1px solid var(--border);
}
.btn-cancel {
  padding: 8px 18px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: all 0.15s;
}
.btn-cancel:hover {
  background: var(--surface-2);
}
.btn-confirm {
  padding: 8px 18px;
  border: none;
  background: var(--accent);
  color: #fff;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 600;
  transition: all 0.15s;
}
.btn-confirm:hover:not(:disabled) {
  background: var(--accent-hover);
}
.btn-confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.15s;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
