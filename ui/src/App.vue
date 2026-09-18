<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, computed } from 'vue'
import { api } from './api.js'
import Sidebar from './components/Sidebar.vue'
import ChatView from './components/ChatView.vue'
import WorkspacePanel from './components/WorkspacePanel.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import ToastHost from './components/ToastHost.vue'
import { useToast } from './composables/useToast.js'

const toast = useToast()

const conversations = ref([])
const currentId = ref(null)
const messages = ref([])
const loadingConvs = ref(false)
const sending = ref(false)
// 侧边栏折叠状态（持久化到 localStorage，默认展开）
const sidebarCollapsed = ref(localStorage.getItem('sidebarCollapsed') === '1')
// 右侧工作区面板折叠状态
const workspaceCollapsed = ref(localStorage.getItem('workspaceCollapsed') === '1')
// 全局有效工作区：新对话用它绑定；selectConversation 时用 conv.workspaceRoot 覆盖
// null 表示走 AI 默认工作区（ai/workspace_default/）
const activeWorkspace = ref(null)
// 图片上传阶段（与 AI 流式阶段区分，驱动"正在上传图片…"指示）
const uploading = ref(false)
// 本轮是否有任何进行中的请求（上传或流式），用于切对话/新建/删除守卫
const busy = computed(() => sending.value || uploading.value)
// 本轮是否在等待思考模式回复（驱动 typing 指示器文案）
const pendingThinking = ref(false)
const aiStatus = ref({})
// 当前流式请求的中断控制器（"停止生成"用）
let abortController = null
// AI 状态灯轮询句柄（挂载时探一次，之后每 30s 轻量轮询）
let statusTimer = null
// 本地乐观消息的稳定 key 序列（服务端消息用数字 id）
let localKeySeq = 0
function localKey() {
  return `local-${Date.now()}-${++localKeySeq}`
}

// 自定义确认弹窗状态（Promise 化，替代 window.confirm）
const dialog = reactive({
  show: false,
  title: '',
  message: '',
  confirmText: '确定',
  cancelText: '取消',
  variant: 'danger',
  _resolve: null
})

function askConfirm({
  title = '请确认',
  message = '',
  confirmText = '确定',
  cancelText = '取消',
  variant = 'danger'
} = {}) {
  return new Promise((resolve) => {
    Object.assign(dialog, {
      show: true,
      title,
      message,
      confirmText,
      cancelText,
      variant,
      _resolve: resolve
    })
  })
}

function closeDialog(result) {
  dialog.show = false
  dialog._resolve?.(result)
  dialog._resolve = null
}

const currentConv = computed(
  () => conversations.value.find((c) => c.id === currentId.value) || null
)

onMounted(async () => {
  // 进入界面只拉取历史列表，不自动创建/选中对话；
  // 用户发送第一条消息时才按需自动创建（见 sendMessage）
  await loadConversations()
  await refreshAiStatus()
  refreshFeatures()
  // AI 模块可能在使用中途宕机：周期轻量轮询让头部状态灯保持真实
  statusTimer = setInterval(() => {
    refreshAiStatus()
    // 功能开关加载失败（AI 宕机）时随轮询自动重试
    if (!features.value) refreshFeatures()
  }, 30000)
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('dragover', blockWindowFileDrop)
  window.addEventListener('drop', blockWindowFileDrop)
})

onBeforeUnmount(() => {
  if (statusTimer) clearInterval(statusTimer)
  window.removeEventListener('beforeunload', onBeforeUnload)
  window.removeEventListener('dragover', blockWindowFileDrop)
  window.removeEventListener('drop', blockWindowFileDrop)
})

// 健康检查永不抛异常（后端失败时也返回 200+unreachable），仅网络层错误需吞掉
async function refreshAiStatus() {
  try {
    aiStatus.value = await api.aiStatus()
  } catch (_) {}
}

// ---------------- 阶段4C：功能开关（MCP / Multi-Agent，状态以后端为准） ----------------

// null = 尚未加载成功或 AI 不可用（输入框 pills 显示禁用态）
const features = ref(null)

async function refreshFeatures() {
  try {
    features.value = await api.getFeatures()
  } catch (_) {
    features.value = null
  }
}

/** pill 切换：乐观更新 → POST → 回读后端全量态；失败回滚 + toast.error */
async function toggleFeature(key, next) {
  const prev = features.value
  if (!prev || prev[key] === next) return
  features.value = { ...prev, [key]: next }
  try {
    features.value = await api.updateFeatures({ [key]: next })
    toast.info(key === 'mcp_enabled'
      ? (next ? 'MCP 工具已开启，可点击齿轮配置 server' : 'MCP 工具已关闭')
      : (next ? '任务委派已开启，AI 可拆解委派子任务' : '任务委派已关闭'))
  } catch (e) {
    features.value = prev
    toast.error(e.message || '切换失败，请稍后重试')
  }
}

/**
 * MCP 配置弹层保存（mcp_servers 变更即热重连）。
 * onDone/onFail 由弹层回调：成功关弹层、失败保留内容并显示后端 400 原因。
 */
async function saveMcpServers({ servers, onDone, onFail }) {
  const prev = features.value
  try {
    features.value = await api.updateFeatures({ mcp_servers: servers })
    onDone?.()
    const health = features.value.mcp_servers_health || []
    const ok = health.filter((h) => h.status === 'connected').length
    toast.info(health.length
      ? `MCP 配置已保存：${ok}/${health.length} 个 server 已连接`
      : 'MCP 配置已保存（当前无 server）')
  } catch (e) {
    features.value = prev
    onFail?.(e.message)
  }
}

// 生成/上传中刷新或关闭页面：提示用户（请求可能仍在后台完成并落库）
function onBeforeUnload(e) {
  if (busy.value) {
    e.preventDefault()
    e.returnValue = ''
  }
}

// 兜底：文件拖到聊天区以外（侧边栏/窗口边缘）松手时，
// 阻止浏览器默认行为（直接打开本地文件导致整个应用页面被替换）。
// 但右侧 WorkspacePanel 需要接受文件夹拖拽，放行它的 drop zone。
function blockWindowFileDrop(e) {
  if (Array.from(e.dataTransfer?.types || []).includes('Files')) {
    // 如果拖到了工作区面板区域，让它自己的 handler 接管
    const target = e.target
    if (target?.closest?.('.workspace-panel')) return
    e.preventDefault()
  }
}

async function loadConversations() {
  loadingConvs.value = true
  try {
    conversations.value = await api.listConversations()
    if (currentId.value && !conversations.value.find((c) => c.id === currentId.value)) {
      currentId.value = null
      messages.value = []
    }
  } catch (e) {
    // 侧栏列表是整个应用的入口数据，加载失败不能静默（后端未启动时侧栏空白无任何提示）
    toast.error('对话列表加载失败，请确认后端服务已启动')
  } finally {
    loadingConvs.value = false
  }
}

async function createConversation() {
  // 发送流程中禁止新建：否则列表插入空对话项但无法切换，产生幽灵条目
  if (busy.value) return
  try {
    const c = await api.createConversation('', activeWorkspace.value || null)
    conversations.value.unshift(c)
    await selectConversation(c.id)
  } catch (e) {
    toast.error(e.message)
  }
}

async function selectConversation(id) {
  if (busy.value) return
  currentId.value = id
  try {
    const c = await api.getConversation(id)
    messages.value = c.messages || []
    // 同步该对话的工作区
    activeWorkspace.value = c.workspaceRoot || null
    // 更新列表中该对话(可能标题已更新)
    const idx = conversations.value.findIndex((x) => x.id === id)
    if (idx >= 0) conversations.value[idx] = c
  } catch (e) {
    toast.error(e.message)
  }
}

async function deleteConversation(id) {
  if (busy.value) return
  const ok = await askConfirm({
    title: '删除对话',
    message: '确定删除该对话？此操作不可恢复。',
    confirmText: '删除',
    cancelText: '取消',
    variant: 'danger'
  })
  if (!ok) return
  try {
    await api.deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentId.value === id) {
      currentId.value = null
      messages.value = []
      // 工作区保留不变——用户可能想基于同一工作区新建下一个对话
      if (conversations.value.length) {
        await selectConversation(conversations.value[0].id)
      }
    }
  } catch (e) {
    toast.error(e.message)
  }
}

/** 侧边栏双击重命名：成功后本地直接替换，列表顺序不变（updatedAt 变化下次刷新自然排序） */
async function renameConversation(id, title) {
  try {
    const updated = await api.renameConversation(id, title)
    const idx = conversations.value.findIndex((c) => c.id === id)
    if (idx >= 0) conversations.value[idx] = updated
  } catch (e) {
    toast.error(e.message)
  }
}

async function sendMessage(payload) {
  const text = payload.text || ''
  const files = payload.files || []
  const thinking = !!payload.thinking
  const content = text.trim()
  if ((!content && !files.length) || busy.value) return
  // 无当前会话（首次进入或已删完所有对话）时，发送第一条消息自动新建对话
  if (!currentId.value) {
    await createConversation()
    if (!currentId.value) return
  }

  // 乐观渲染：图片先用本地 objectURL 占位
  const localUrls = files.map((f) => URL.createObjectURL(f))
  const optimistic = {
    _key: localKey(),
    role: 'user',
    content,
    images: localUrls.map((url) => ({ url })),
    createdAt: new Date().toISOString()
  }
  messages.value.push(optimistic)
  // 取响应式代理引用：直接改原始对象不会触发 proxy 的 set trap，
  // 视图（如图片占位替换）不会更新
  let userMsg = messages.value[messages.value.length - 1]
  sending.value = true
  // 流式 AI 消息占位（首 token 到达前由 TypingIndicator 指示，气泡留空）
  let assistant = null
  let userStopped = false
  // 每次发送使用独立中断控制器（停止生成只影响本轮）
  abortController = new AbortController()
  try {
    // 1. 并行上传图片；任一张失败都不发送，并回滚已上传成功的图片（避免孤儿文件）
    uploading.value = true
    const results = await Promise.allSettled(files.map((f) => api.uploadImage(f)))
    uploading.value = false
    const uploaded = results
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value)
    if (uploaded.length < files.length) {
      await Promise.allSettled(uploaded.map((u) => api.deleteImage(u.id)))
      throw new Error(
        `${files.length - uploaded.length} 张图片上传失败，已取消发送，请检查网络后重试`
      )
    }
    // 2. 用真实引用替换本地占位图。objectURL 暂不释放：
    //    若后续流式失败，后端会回滚并删除图片文件，需要退回本地预览防裂图；
    //    成功收到 done 帧后再释放
    userMsg.images = uploaded.map((u) => ({
      id: u.id,
      filename: u.filename,
      contentType: u.contentType
    }))

    // 3. 流式发送：边收 token/reasoning 边渲染，done 帧用服务端最终消息校正
    assistant = {
      _key: localKey(),
      role: 'assistant',
      content: '',
      reasoning: null,
      // 只读工具轨迹（tool_call/tool_result 帧实时累积；done 帧以服务端
      // payload.message.toolTrace 为准整体校正，含 seq/durationMs）
      toolTrace: [],
      streaming: true,
      createdAt: new Date().toISOString()
    }
    messages.value.push(assistant)
    // 关键：取回数组内的响应式代理，流式回调必须通过代理修改，
    // 否则直接改原始对象不触发依赖通知，MessageItem 的 computed/渲染不会刷新
    assistant = messages.value[messages.value.length - 1]
    pendingThinking.value = thinking

    let streamError = null
    // 本轮工具起始时间戳（按 tool_call id 配对，供流式期间显示耗时；
    // done 帧后统一使用服务端 toolTrace 的 durationMs）
    const toolStartTimes = new Map()
    await api.sendMessageStream(currentId.value, content, uploaded, thinking, {
      onToken(delta) {
        assistant.content += delta
      },
      onReasoning(delta) {
        assistant.reasoning = (assistant.reasoning || '') + delta
      },
      onToolCall(call) {
        if (!call || !call.id) return
        toolStartTimes.set(call.id, performance.now())
        assistant.toolTrace.push({
          id: call.id,
          name: call.name,
          args: call.args,
          rawArgs: call.rawArgs,
          status: 'running',
          output: null,
          truncated: false
        })
      },
      onToolResult(result) {
        if (!result || !result.id) return
        let step = assistant.toolTrace.find((s) => s.id === result.id)
        if (!step) {
          // 防御坏帧：result 先于/脱离 call 到达时补占位（与 web
          // ChatStreamService 的无配对补全策略对称），避免轨迹永久卡在 running
          step = {
            id: result.id,
            name: result.name,
            args: null,
            rawArgs: null,
            status: 'running',
            output: null,
            truncated: false
          }
          assistant.toolTrace.push(step)
        }
        step.status = result.status
        step.output = result.output
        step.truncated = result.truncated
        const startedAt = toolStartTimes.get(result.id)
        if (startedAt != null) {
          step.durationMs = Math.max(0, Math.round(performance.now() - startedAt))
          toolStartTimes.delete(result.id)
        }
        // 同名工具已回填结果：确认卡使命完成（确认→已执行 / 拒绝→已回填 error），收起
        if (assistant.confirmRequest && assistant.confirmRequest.tool === result.name) {
          assistant.confirmRequest = null
        }
      },
      onConfirm(call) {
        // 阶段3：写/执行工具确认请求，挂在当前流式消息上展示确认卡
        if (!assistant) return
        assistant.confirmRequest = {
          id: call.id,
          tool: call.tool,
          summary: call.summary,
          state: 'pending'
        }
      },
      onDone(payload) {
        // 用落库后的完整消息（含 id/createdAt/最终全文/服务端 toolTrace）校正本地累积
        if (payload && payload.message) {
          Object.assign(assistant, payload.message, { streaming: false })
        } else {
          assistant.streaming = false
        }
        // 确认状态不落库：done 后无论卡片处于何种状态一律收起
        assistant.confirmRequest = null
        // 消息已确认落库，本地图片预览可以安全释放
        localUrls.forEach((u) => URL.revokeObjectURL(u))
      },
      onError(payload) {
        streamError = payload?.message || 'AI 服务异常，请稍后重试'
      }
    }, abortController.signal)
    // api.js 对 AbortError 是静默返回（不当作流错误），这里补查：
    // 主动中断必须进入 catch 的"已停止"分支，否则会误走成功路径
    if (abortController.signal.aborted) {
      throw new DOMException('Aborted', 'AbortError')
    }
    if (streamError) throw new Error(streamError)

    // 刷新对话列表顺序与标题
    await loadConversations()
  } catch (e) {
    userStopped = abortController?.signal.aborted === true
    // streaming 已在 onDone 置 false（done 与 abort 竞态时以成功为准）
    if (userStopped && assistant && assistant.streaming !== false) {
      // 主动停止：保留已生成的半截回复并标注；用户消息大概率已在后台落库，
      // 不标失败、不弹错误，刷新后可看到后台跑完的完整回复
      assistant.streaming = false
      assistant.stopped = true
    } else {
      // 失败时移除未完成的流式占位（若已插入）
      if (assistant) {
        const idx = messages.value.indexOf(assistant)
        if (idx >= 0) messages.value.splice(idx, 1)
      }
      // 后端失败会回滚用户消息并删除已上传的图片文件，前端这条乐观消息
      // 实际未落库：标记 failed 明确提示，并把图片退回本地 objectURL 预览防裂图
      if (userMsg) {
        userMsg.failed = true
        userMsg.images = localUrls.map((url) => ({ url }))
      }
      messages.value.push({
        _key: localKey(),
        role: 'assistant',
        content: `> 请求失败：${userStopped ? '已取消发送' : e.message}`,
        createdAt: new Date().toISOString(),
        error: true
      })
      // 失败很可能意味着 AI 模块已宕机：立即重探一次状态灯，
      // 不必等 30s 轮询（连接被拒类错误最典型）
      if (!userStopped) refreshAiStatus()
    }
  } finally {
    uploading.value = false
    sending.value = false
    pendingThinking.value = false
    abortController = null
  }
}

/** 停止生成：中断前端 SSE 读取（后端仍会跑完并落库，token 照常计费） */
function stopGeneration() {
  abortController?.abort()
}

/**
 * confirm 帧用户决策（阶段3 写/执行工具人机确认）：
 * POST 决策 → 卡片转终态；404（confirmId 失效：超时/断连/AI 重启）→ expired + toast；
 * 其他失败保持 pending 允许重试。busy 守卫已覆盖等待确认期间禁发新消息。
 */
async function decideConfirm(message, approved) {
  const req = message?.confirmRequest
  if (!req || req.state !== 'pending') return
  try {
    await api.sendConfirm(currentId.value, req.id, approved)
    req.state = approved ? 'approved' : 'rejected'
  } catch (e) {
    if (e.status === 404) {
      req.state = 'expired'
      toast.error('确认请求已失效，该操作已被拒绝')
    } else {
      toast.error('确认决策发送失败：' + e.message)
    }
  }
}

/** 切换侧边栏折叠状态（持久化到 localStorage） */
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem('sidebarCollapsed', sidebarCollapsed.value ? '1' : '0')
}

/** 切换右侧工作区面板折叠状态 */
function toggleWorkspacePanel() {
  workspaceCollapsed.value = !workspaceCollapsed.value
  localStorage.setItem('workspaceCollapsed', workspaceCollapsed.value ? '1' : '0')
}

/** 工作区变更（WorkspacePanel 拖入文件夹确认后触发） */
async function onWorkspaceChange(rootPath) {
  activeWorkspace.value = rootPath || null
  // 持久化到后端：PATCH 当前对话的 workspace_root
  if (currentId.value) {
    try {
      const updated = await api.updateConversation(currentId.value, {
        workspaceRoot: rootPath || ''
      })
      // 同步侧栏/列表中该对话的 workspaceRoot
      const idx = conversations.value.findIndex((c) => c.id === currentId.value)
      if (idx >= 0 && updated) conversations.value[idx] = updated
      toast.success(rootPath
        ? `工作区已切换：${rootPath}`
        : '已切回默认工作区')
    } catch (e) {
      toast.error('工作区切换失败：' + e.message)
    }
  }
}

/**
 * 失败消息重试：失败气泡充当草稿——文本直接复用，图片从本地 objectURL
 * 还原为 File 重新上传；移除失败气泡和紧随其后的错误气泡后重新发送。
 */
async function retrySend(failedMsg) {
  if (busy.value) return
  const idx = messages.value.indexOf(failedMsg)
  if (idx < 0) return

  const files = []
  const localUrls = (failedMsg.images || [])
    .map((im) => im.url)
    .filter(Boolean)
  for (const url of localUrls) {
    try {
      const blob = await (await fetch(url)).blob()
      files.push(new File([blob], 'pasted-image', { type: blob.type || 'image/png' }))
    } catch {
      // 个别本地预览失效则跳过该图，不阻断文本重试
    }
  }
  const text = failedMsg.content || ''

  messages.value.splice(idx, 1)
  // 移除配对的错误气泡
  if (messages.value[idx] && messages.value[idx].role === 'assistant' && messages.value[idx].error) {
    messages.value.splice(idx, 1)
  }
  // 旧 objectURL 已被新 File 取代，立即释放
  localUrls.forEach((u) => URL.revokeObjectURL(u))

  await sendMessage({ text, files, thinking: false })
}
</script>

<template>
  <div class="app-shell">
    <Sidebar
      :conversations="conversations"
      :current-id="currentId"
      :loading="loadingConvs"
      :busy="busy"
      :collapsed="sidebarCollapsed"
      @new="createConversation"
      @select="selectConversation"
      @delete="deleteConversation"
      @rename="renameConversation"
    />
    <ChatView
      :conv="currentConv"
      :messages="messages"
      :sending="sending"
      :uploading="uploading"
      :pending-thinking="pendingThinking"
      :ai-status="aiStatus"
      :features="features"
      :sidebar-collapsed="sidebarCollapsed"
      :workspace-collapsed="workspaceCollapsed"
      @toggle-sidebar="toggleSidebar"
      @toggle-workspace="toggleWorkspacePanel"
      @send="sendMessage"
      @stop="stopGeneration"
      @retry="retrySend"
      @confirm-decide="decideConfirm"
      @toggle-feature="toggleFeature"
      @save-mcp-servers="saveMcpServers"
      @new="createConversation"
    />
    <WorkspacePanel
      :workspace-root="activeWorkspace"
      :default-workspace="aiStatus.default_workspace"
      :collapsed="workspaceCollapsed"
      @toggle="toggleWorkspacePanel"
      @workspace-change="onWorkspaceChange"
    />

    <ConfirmDialog
      :show="dialog.show"
      :title="dialog.title"
      :message="dialog.message"
      :confirm-text="dialog.confirmText"
      :cancel-text="dialog.cancelText"
      :variant="dialog.variant"
      @confirm="closeDialog(true)"
      @cancel="closeDialog(false)"
    />
    <ToastHost />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100%;
  width: 100%;
}
</style>
