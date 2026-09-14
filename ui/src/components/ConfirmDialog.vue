<script setup>
import { watch, nextTick, ref } from 'vue'

const props = defineProps({
  show: { type: Boolean, default: false },
  title: { type: String, default: '请确认' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: '确定' },
  cancelText: { type: String, default: '取消' },
  // danger：红色主按钮（删除等破坏性操作）；primary：品牌蓝
  variant: { type: String, default: 'danger' }
})

const emit = defineEmits(['confirm', 'cancel'])

const confirmBtn = ref(null)

function onKeydown(e) {
  if (!props.show) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('cancel')
  } else if (e.key === 'Enter') {
    e.preventDefault()
    emit('confirm')
  }
}

// 打开时锁定背景滚动、绑定 ESC、自动聚焦主按钮
watch(
  () => props.show,
  async (open) => {
    if (open) {
      document.addEventListener('keydown', onKeydown)
      document.body.style.overflow = 'hidden'
      await nextTick()
      confirmBtn.value?.focus()
    } else {
      document.removeEventListener('keydown', onKeydown)
      document.body.style.overflow = ''
    }
  }
)
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="show"
        class="dialog-overlay"
        @click.self="emit('cancel')"
      >
        <div
          class="dialog-card"
          role="alertdialog"
          aria-modal="true"
          :aria-label="title"
        >
          <div class="dialog-icon" :class="`is-${variant}`">
            <!-- 删除/危险图标 -->
            <svg
              v-if="variant === 'danger'"
              viewBox="0 0 24 24"
              width="22"
              height="22"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M3 6h18" />
              <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
              <line x1="10" y1="11" x2="10" y2="17" />
              <line x1="14" y1="11" x2="14" y2="17" />
            </svg>
            <!-- 通用提示图标 -->
            <svg
              v-else
              viewBox="0 0 24 24"
              width="22"
              height="22"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>

          <div class="dialog-body">
            <h3 class="dialog-title">{{ title }}</h3>
            <p v-if="message" class="dialog-message">{{ message }}</p>
          </div>

          <div class="dialog-actions">
            <button
              type="button"
              class="btn btn-cancel"
              @click="emit('cancel')"
            >
              {{ cancelText }}
            </button>
            <button
              ref="confirmBtn"
              type="button"
              class="btn"
              :class="variant === 'danger' ? 'btn-danger' : 'btn-primary'"
              @click="emit('confirm')"
            >
              {{ confirmText }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(15, 23, 42, 0.42);
  backdrop-filter: blur(3px);
  -webkit-backdrop-filter: blur(3px);
}

.dialog-card {
  width: 100%;
  max-width: 420px;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 16px;
  box-shadow:
    0 20px 50px -12px rgba(15, 23, 42, 0.25),
    0 4px 12px -4px rgba(15, 23, 42, 0.1);
  padding: 24px;
}

.dialog-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 16px;
}
.dialog-icon.is-danger {
  background: var(--danger-soft, #fdecec);
  color: var(--danger, #e5484d);
}
.dialog-icon.is-primary {
  background: var(--accent-soft, #eef1ff);
  color: var(--accent, #4f6ef7);
}

.dialog-title {
  margin: 0 0 8px;
  font-size: 17px;
  font-weight: 650;
  color: var(--text, #1f2328);
  line-height: 1.4;
}

.dialog-message {
  margin: 0;
  font-size: 14px;
  line-height: 1.65;
  color: #5b6472;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
}

.btn {
  min-width: 84px;
  padding: 9px 18px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 550;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s,
    transform 0.08s;
}
.btn:active {
  transform: scale(0.97);
}
.btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px rgba(79, 110, 247, 0.25);
}

.btn-cancel {
  background: var(--surface, #fff);
  border-color: var(--border, #e5e7eb);
  color: #4b5563;
}
.btn-cancel:hover {
  background: var(--surface-2, #f0f2f5);
}

.btn-danger {
  background: var(--danger, #e5484d);
  color: #fff;
}
.btn-danger:hover {
  background: #d1343a;
  box-shadow: 0 4px 12px -2px rgba(229, 72, 77, 0.45);
}

.btn-primary {
  background: var(--accent, #4f6ef7);
  color: #fff;
}
.btn-primary:hover {
  background: var(--accent-hover, #3f5be0);
  box-shadow: 0 4px 12px -2px rgba(79, 110, 247, 0.45);
}

/* 进入/离开动画：遮罩淡入 + 卡片缩放上浮 */
.dialog-enter-active,
.dialog-leave-active {
  transition: opacity 0.2s ease;
}
.dialog-enter-active .dialog-card,
.dialog-leave-active .dialog-card {
  transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1),
    opacity 0.2s ease;
}
.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
.dialog-enter-from .dialog-card,
.dialog-leave-to .dialog-card {
  opacity: 0;
  transform: translateY(12px) scale(0.96);
}

@media (prefers-reduced-motion: reduce) {
  .dialog-enter-active,
  .dialog-leave-active,
  .dialog-enter-active .dialog-card,
  .dialog-leave-active .dialog-card {
    transition: none;
  }
}
</style>
