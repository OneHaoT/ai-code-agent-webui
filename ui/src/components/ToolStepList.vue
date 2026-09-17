<script setup>
import { ref, watch } from 'vue'

// 只读工具轨迹时间线（与后端 ToolStep 字段对齐）：
// running 转圈 → success/error 终态 + 耗时；点击行折叠查看 output。
// 历史轨迹（刷新/切换对话）只有终态字段，同样渲染。
const props = defineProps({
  steps: { type: Array, default: () => [] }
})

const TOOL_META = {
  read_file: {
    label: '读取文件',
    icon: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M9 13h6 M9 17h6'
  },
  list_dir: {
    label: '浏览目录',
    icon: 'M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z'
  },
  glob: {
    label: '匹配文件',
    icon: 'M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1 M16 3h1a2 2 0 0 1 2 2v5a2 2 0 0 0 2 2 2 2 0 0 0-2 2v5a2 2 0 0 1-2 2h-1'
  },
  grep: {
    label: '内容检索',
    icon: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.35-4.35'
  },
  search_code: {
    label: '语义检索',
    icon: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.35-4.35 M8.3 11h5.4 M11 8.3v5.4'
  },
  write_file: {
    label: '写入文件',
    icon: 'M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z'
  },
  run_command: {
    label: '执行命令',
    icon: 'M4 17l6-6-6-6 M12 19h8'
  }
}
const FALLBACK_META = { label: '工具调用', icon: 'M12 2v4 M12 18v4 M2 12h4 M18 12h4' }
// 阶段4A：外部 MCP 工具（mcp_{server}_{tool}，动态注册、名称不可枚举）——
// 通用连接图标 + 原始工具名，其余折叠/展开/耗时/错误态复用既有实现
const MCP_META = {
  label: '外部工具',
  icon: 'M9 7H7a5 5 0 0 0 0 10h2 M15 7h2a5 5 0 0 1 0 10h-2 M8 12h8'
}

function metaOf(name) {
  if (name && name.startsWith('mcp_')) return { ...MCP_META, label: name }
  return TOOL_META[name] || { ...FALLBACK_META, label: name || '工具调用' }
}

// 参数摘要：仅取关键定位字段，一行内省略号截断
function argSummary(step) {
  const a = step.args
  if (!a || typeof a !== 'object') {
    return step.rawArgs ? '参数无法解析' : ''
  }
  const parts = []
  // 阶段3 写工具：路径是关键定位信息；内容在确认卡中预览，这里只显示路径
  if (step.name === 'write_file') {
    return a.path != null && a.path !== '' ? String(a.path) : ''
  }
  // 阶段3 执行工具：命令全文即摘要（超长由 CSS 截断，title 可见全文）
  if (step.name === 'run_command') {
    return a.command != null && a.command !== '' ? String(a.command) : ''
  }
  // 语义检索：query 是关键信息全量展示，path 仅辅助定位、超长截断
  if (step.name === 'search_code') {
    if (a.query != null && a.query !== '') parts.push(String(a.query))
    if (a.path && a.path !== '.') {
      const p = String(a.path)
      parts.push(p.length > 24 ? p.slice(0, 21) + '…' : p)
    }
    return parts.join('  ·  ')
  }
  // 阶段4A 外部 MCP 工具：参数结构不可枚举，取一层键值拼摘要（超长截断）
  if (step.name && step.name.startsWith('mcp_')) {
    const s = Object.entries(a)
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('  ·  ')
    return s.length > 60 ? s.slice(0, 57) + '…' : s
  }
  if (a.pattern != null && a.pattern !== '') parts.push(String(a.pattern))
  const target = a.path
  if (target) parts.push(target)
  if (typeof a.offset === 'number') {
    parts.push(a.limit != null ? `第 ${a.offset} 行起 · ${a.limit} 行` : `第 ${a.offset} 行起`)
  }
  if (a.file_glob) parts.push(String(a.file_glob))
  return parts.join('  ·  ')
}

function formatDuration(ms) {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function canExpand(step) {
  return step.status !== 'running' && (step.output || step.rawArgs)
}

// 折叠状态：错误步骤默认展开，其余默认收起；用户手动切换优先
const openIds = ref(new Set())
watch(
  () => props.steps,
  (steps) => {
    steps.forEach((s) => {
      if (s.status === 'error' && canExpand(s)) openIds.value.add(s.id)
    })
  },
  { deep: true, immediate: true }
)
function toggle(step) {
  if (!canExpand(step)) return
  const next = new Set(openIds.value)
  if (next.has(step.id)) next.delete(step.id)
  else next.add(step.id)
  openIds.value = next
}
function isOpen(step) {
  return openIds.value.has(step.id)
}

const runningCount = () => props.steps.filter((s) => s.status === 'running').length
</script>

<template>
  <div class="tools" :class="{ busy: runningCount() > 0 }">
    <div class="tools-head">
      <svg class="tools-head-icon" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-2.4z" />
      </svg>
      <span>工具调用 · {{ steps.length }}</span>
      <span v-if="runningCount()" class="tools-dot" aria-hidden="true"></span>
    </div>
    <div class="step-list">
      <div
        v-for="step in steps"
        :key="step.id"
        class="step"
        :class="[step.status || 'running', { 'open-output': canExpand(step) && isOpen(step) }]"
      >
        <button
          type="button"
          class="step-row"
          :disabled="!canExpand(step)"
          :aria-expanded="canExpand(step) ? isOpen(step) : undefined"
          @click="toggle(step)"
        >
          <span class="step-rail" aria-hidden="true">
            <svg class="step-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path :d="metaOf(step.name).icon" />
            </svg>
          </span>
          <span class="step-label">{{ metaOf(step.name).label }}</span>
          <span class="step-arg" :title="argSummary(step)">{{ argSummary(step) }}</span>
          <span class="step-state">
            <span v-if="step.status === 'running'" class="spinner" aria-label="执行中"></span>
            <svg
              v-else-if="step.status === 'success'"
              class="state-ok"
              viewBox="0 0 24 24"
              width="13"
              height="13"
              fill="none"
              stroke="currentColor"
              stroke-width="2.6"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-label="成功"
            >
              <path d="M20 6 9 17l-5-5" />
            </svg>
            <svg
              v-else
              class="state-err"
              viewBox="0 0 24 24"
              width="13"
              height="13"
              fill="none"
              stroke="currentColor"
              stroke-width="2.6"
              stroke-linecap="round"
              aria-label="失败"
            >
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
            <span v-if="step.status === 'running'" class="state-text">执行中…</span>
            <span v-else-if="step.durationMs != null" class="state-text">{{ formatDuration(step.durationMs) }}</span>
            <svg
              v-if="canExpand(step)"
              class="step-chev"
              viewBox="0 0 24 24"
              width="11"
              height="11"
              fill="none"
              stroke="currentColor"
              stroke-width="2.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
          </span>
        </button>
        <div v-if="canExpand(step) && isOpen(step)" class="step-output">
          <pre v-if="step.output">{{ step.output }}</pre>
          <template v-else-if="step.rawArgs">
            <div class="raw-tip">原始参数（JSON 解析失败）：</div>
            <pre>{{ step.rawArgs }}</pre>
          </template>
          <div v-if="step.truncated" class="truncated-tip">输出过长，已截断</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tools {
  margin-bottom: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2, #f5f6f8);
  overflow: hidden;
}
.tools-head {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 10px;
  font-size: 12.5px;
  color: var(--muted);
}
.tools-head-icon {
  color: var(--accent);
}
.tools-dot {
  width: 6px;
  height: 6px;
  margin-left: 2px;
  border-radius: 50%;
  background: var(--accent);
  animation: tools-pulse 1s ease-in-out infinite;
}
@keyframes tools-pulse {
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
.step-list {
  padding: 0 6px 6px;
}
.step {
  position: relative;
}
.step + .step::before {
  content: '';
  position: absolute;
  left: 15px;
  top: -2px;
  width: 1px;
  height: 8px;
  background: var(--border);
}
.step-row {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 4px 6px;
  border: none;
  border-radius: 7px;
  background: transparent;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text, #2d303c);
  text-align: left;
  cursor: default;
}
button.step-row:not(:disabled) {
  cursor: pointer;
}
button.step-row:not(:disabled):hover {
  background: rgba(0, 0, 0, 0.04);
}
.step-rail {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  flex-shrink: 0;
  background: var(--surface, #fff);
  border: 1px solid var(--border);
  color: var(--muted);
}
.step.success .step-rail {
  color: #16a34a;
  border-color: rgba(22, 163, 74, 0.35);
}
.step.error .step-rail {
  color: var(--danger);
  border-color: #f5c6c6;
}
.step.running .step-rail {
  color: var(--accent);
  border-color: rgba(79, 110, 247, 0.35);
}
.step-label {
  flex-shrink: 0;
  font-weight: 600;
}
.step-arg {
  flex: 1;
  min-width: 0;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: rtl;
  text-align: left;
}
.step-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.state-ok {
  color: #16a34a;
}
.state-err {
  color: var(--danger);
}
.step-chev {
  margin-left: 1px;
  transition: transform 0.18s ease;
}
.step.open-output .step-chev {
  transform: rotate(90deg);
}
.spinner {
  width: 11px;
  height: 11px;
  border: 1.8px solid rgba(79, 110, 247, 0.25);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: tools-spin 0.7s linear infinite;
}
@keyframes tools-spin {
  to {
    transform: rotate(360deg);
  }
}
.step-output {
  margin: 2px 0 4px 35px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface, #fff);
  overflow: hidden;
}
.step-output pre {
  margin: 0;
  padding: 8px 10px;
  max-height: 240px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text, #2d303c);
  white-space: pre-wrap;
  word-break: break-word;
}
.step.error .step-output {
  border-color: #f5c6c6;
}
.step.error .step-output pre {
  color: var(--danger);
}
.raw-tip {
  padding: 7px 10px 0;
  font-size: 11.5px;
  color: var(--danger);
}
.truncated-tip {
  padding: 5px 10px 7px;
  font-size: 11px;
  color: var(--muted);
  border-top: 1px dashed var(--border);
}
@media (prefers-reduced-motion: reduce) {
  .tools-dot,
  .spinner {
    animation: none;
  }
}
</style>
