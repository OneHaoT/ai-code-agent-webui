import { reactive } from 'vue'

/**
 * 全局轻提示（Toast）—— 模块级单例队列，任意组件调用 useToast() 均可触发。
 * 替代浏览器原生 alert()：不阻塞页面、样式与应用统一。
 *
 * 用法:
 *   const toast = useToast()
 *   toast.error('删除失败')
 *   toast.success('已保存')
 */
let _seq = 0
const toasts = reactive([])

function push(type, message, duration = 4000) {
  const id = ++_seq
  toasts.push({ id, type, message })
  if (duration > 0) {
    setTimeout(() => dismiss(id), duration)
  }
  // 最多同时保留 4 条，避免堆积
  while (toasts.length > 4) toasts.shift()
  return id
}

function dismiss(id) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

export function useToast() {
  return {
    toasts,
    dismiss,
    error: (msg, duration) => push('error', msg, duration),
    success: (msg, duration) => push('success', msg, duration ?? 2500),
    info: (msg, duration) => push('info', msg, duration)
  }
}
