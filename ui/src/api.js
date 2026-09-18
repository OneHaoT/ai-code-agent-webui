// 与后端通信的 API 封装
const BASE = '' // 走 vite 代理 /api -> http://localhost:8080

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  })
  if (!res.ok) {
    let msg = `请求失败 (${res.status})`
    try {
      const data = await res.json()
      if (data.error) msg = data.error
    } catch (_) {}
    const err = new Error(msg)
    err.status = res.status // 调用方按状态码分流（如 confirm 404 = 已失效）
    throw err
  }
  return res.json()
}

export const api = {
  listConversations: () => request('/api/conversations'),
  createConversation: (title, workspaceRoot) =>
    request('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({
        title: title || null,
        workspaceRoot: workspaceRoot || null
      })
    }),
  deleteConversation: (id) =>
    request(`/api/conversations/${id}`, { method: 'DELETE' }),
  getConversation: (id) => request(`/api/conversations/${id}`),
  renameConversation: (id, title) =>
    request(`/api/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title })
    }),
  updateConversation: (id, { title, workspaceRoot }) =>
    request(`/api/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        ...(title !== undefined && { title }),
        ...(workspaceRoot !== undefined && { workspaceRoot })
      })
    }),
  /** 前端上传单文件（multipart/form-data）到默认工作区 */
  uploadWorkspaceFile: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch('/api/workspace/files', {
      method: 'POST',
      body: fd
    }).then((r) => {
      if (!r.ok) throw new Error(`上传失败 ${r.status}`)
      return r.json()
    })
  },
  /** 在系统文件浏览器打开工作区：root 空 = 默认工作区，非空 = 绑定项目根 */
  openWorkspace: (root) =>
    request('/api/workspace/open' + (root ? `?root=${encodeURIComponent(root)}` : ''),
      { method: 'POST' }),

  /**
   * 只读浏览工作区单层目录（阶段3.5 项目树懒加载数据源）。
   * root = 绑定的工作区绝对路径；path = 相对子目录（空 = 根）。
   * 越界/非法路径后端 400，request() 抛 err（带 status）。
   */
  listWorkspaceFiles: (root, path) => {
    const qs = new URLSearchParams()
    if (root) qs.set('root', root)
    if (path) qs.set('path', path)
    const s = qs.toString()
    return request(`/api/workspace/list${s ? '?' + s : ''}`)
  },

  /**
   * 转发用户对 confirm 帧的决策（阶段3 写/执行工具人机确认）。
   * confirmId 未知/已失效时后端返回 404（err.status === 404）。
   */
  sendConfirm: (conversationId, confirmId, approved) =>
    request(`/api/conversations/${conversationId}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ confirmId, approved })
    }),

  /**
   * 上传图片（multipart 不能带 JSON Content-Type，浏览器自动设置 boundary）
   * @returns {Promise<{id:string,filename:string,contentType:string,size:number,url:string}>}
   */
  uploadImage: async (file) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(BASE + '/api/images/upload', { method: 'POST', body: fd })
    if (!res.ok) {
      let msg = `图片上传失败 (${res.status})`
      try {
        const data = await res.json()
        if (data.error) msg = data.error
      } catch (_) {}
      throw new Error(msg)
    }
    return res.json()
  },

  /**
   * 删除已上传图片（幂等）。多图上传部分失败时用于回滚已成功的图片，
   * 避免服务端留下无引用的孤儿文件。
   */
  deleteImage: (id) => request(`/api/images/${id}`, { method: 'DELETE' }),

  /**
   * 流式发送消息（SSE）。handlers 回调：
   *   onMeta({model}) / onToken(delta) / onReasoning(delta) /
   *   onToolCall({id,name,args,rawArgs}) / onToolResult({id,name,status,output,truncated}) /
   *   onConfirm({id,tool,summary}) /
   *   onDone({message})
   * 响应头提交前的失败（400/502 等）以普通 reject(Error) 抛出；
   * 流开始后的失败以 onError({message}) 事件返回（服务端会回滚用户消息）。
   * signal 用于"停止生成"：abort 后本函数静默返回（不触发 onError），
   * 由调用方决定本地 UI 收尾；服务端仍会把请求跑完并落库。
   */
  sendMessageStream: async (id, message, images = [], thinking = false, handlers = {}, signal) => {
    const res = await fetch(BASE + `/api/conversations/${id}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        message: message || '',
        images: images.map((im) => ({
          id: im.id,
          filename: im.filename,
          contentType: im.contentType
        })),
        thinking: !!thinking
      }),
      signal
    })

    // HTTP 状态异常：响应头尚未进入 SSE，按普通 JSON 错误处理
    if (!res.ok || !res.body) {
      let msg = `流式请求失败 (${res.status})`
      try {
        const data = await res.json()
        msg = data.error || data.message || msg
      } catch (_) {}
      throw new Error(msg)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    // 是否已收到终结帧（done/error）。连接被代理掐断或后端异常退出时，
    // reader 可能在没有任何终结帧的情况下关闭，必须按失败处理，
    // 否则界面会永久停留在“生成中”（光标闪烁、消息未落库）
    let terminalReceived = false

    const dispatchFrame = (rawFrame) => {
      let event = 'message'
      const dataLines = []
      rawFrame.split(/\r?\n/).forEach((line) => {
        if (!line || line.startsWith(':')) return
        const idx = line.indexOf(':')
        const field = idx === -1 ? line : line.slice(0, idx)
        let value = idx === -1 ? '' : line.slice(idx + 1)
        if (value.startsWith(' ')) value = value.slice(1)
        if (field === 'event') event = value
        else if (field === 'data') dataLines.push(value)
      })
      if (dataLines.length === 0) return
      let payload
      try {
        payload = JSON.parse(dataLines.join('\n'))
      } catch (_) {
        return // 坏帧忽略
      }
      switch (event) {
        case 'meta':
          handlers.onMeta?.(payload)
          break
        case 'token':
          handlers.onToken?.(payload.delta || '')
          break
        case 'reasoning':
          handlers.onReasoning?.(payload.delta || '')
          break
        case 'tool_call':
          // args 可能为 null（参数 JSON 非法时随 raw_args 原样下发）
          handlers.onToolCall?.({
            id: payload.id,
            name: payload.name,
            args: payload.args ?? null,
            rawArgs: payload.raw_args ?? null
          })
          break
        case 'tool_result':
          handlers.onToolResult?.({
            id: payload.id,
            name: payload.name,
            status: payload.status || 'error',
            output: payload.output ?? '',
            truncated: !!payload.truncated
          })
          break
        case 'confirm':
          // 阶段3：写/执行工具人机确认请求 {id, tool, summary}
          handlers.onConfirm?.({
            id: payload.id,
            tool: payload.tool,
            summary: payload.summary ?? ''
          })
          break
        case 'done':
          terminalReceived = true
          handlers.onDone?.(payload)
          break
        case 'error':
          terminalReceived = true
          handlers.onError?.(payload)
          break
      }
    }

    while (true) {
      let chunk
      try {
        chunk = await reader.read()
      } catch (readErr) {
        // 用户主动停止：静默退出，不触发"连接中断"错误
        if (readErr.name === 'AbortError') return
        throw readErr
      }
      const { done, value } = chunk
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let sep
      // SSE 帧以空行分隔
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const rawFrame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        dispatchFrame(rawFrame)
      }
    }
    if (buffer.trim()) dispatchFrame(buffer)

    if (!terminalReceived) {
      handlers.onError?.({ message: '连接中断，未收到完整响应，请重试' })
    }
  },

  aiStatus: () => request('/api/ai/status'),

  /**
   * 功能开关全量状态（阶段4C：MCP / Multi-Agent 前端热切换）。
   * 返回 {multi_agent_enabled, mcp_enabled, mcp_servers, mcp_servers_health}。
   */
  getFeatures: () => request('/api/ai/features'),

  /**
   * 热切换功能开关：body 三字段可选（multi_agent_enabled / mcp_enabled /
   * mcp_servers），后端保存 features.json 并热应用后返回全量状态。
   * mcp_servers 结构非法时后端 400（err.status === 400，err.message 为中文原因）。
   */
  updateFeatures: (body) =>
    request('/api/ai/features', { method: 'POST', body: JSON.stringify(body) })
}
