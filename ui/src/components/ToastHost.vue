<script setup>
import { useToast } from '../composables/useToast.js'

const { toasts, dismiss } = useToast()

const icons = {
  error: 'M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  success: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
  info: 'M12 16v-4m0-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z'
}
</script>

<template>
  <Teleport to="body">
    <div class="toast-host" aria-live="polite" aria-atomic="false">
      <TransitionGroup name="toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          class="toast"
          :class="`is-${t.type}`"
          role="alert"
        >
          <span class="toast-icon">
            <svg
              viewBox="0 0 24 24"
              width="17"
              height="17"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path :d="icons[t.type] || icons.info" />
            </svg>
          </span>
          <span class="toast-msg">{{ t.message }}</span>
          <button
            type="button"
            class="toast-close"
            aria-label="关闭提示"
            @click="dismiss(t.id)"
          >
            <svg
              viewBox="0 0 24 24"
              width="14"
              height="14"
              fill="none"
              stroke="currentColor"
              stroke-width="2.2"
              stroke-linecap="round"
            >
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-host {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 1100;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  pointer-events: none;
  width: min(440px, calc(100vw - 32px));
}

.toast {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 11px 12px 11px 14px;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 12px;
  box-shadow: 0 10px 30px -8px rgba(15, 23, 42, 0.22),
    0 2px 6px -2px rgba(15, 23, 42, 0.08);
  font-size: 13.5px;
  line-height: 1.5;
  color: var(--text, #1f2328);
}

.toast-icon {
  flex-shrink: 0;
  display: flex;
}
.toast.is-error .toast-icon {
  color: var(--danger, #e5484d);
}
.toast.is-success .toast-icon {
  color: #22c55e;
}
.toast.is-info .toast-icon {
  color: var(--accent, #4f6ef7);
}

/* 左侧色条 */
.toast::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 3px;
  background: transparent;
}
.toast {
  position: relative;
}
.toast.is-error::before {
  background: var(--danger, #e5484d);
}
.toast.is-success::before {
  background: #22c55e;
}
.toast.is-info::before {
  background: var(--accent, #4f6ef7);
}

.toast-msg {
  flex: 1;
  word-break: break-word;
}

.toast-close {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: #9aa3af;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.toast-close:hover {
  background: var(--surface-2, #f0f2f5);
  color: #5b6472;
}

/* 动画：从顶部滑入 */
.toast-enter-active,
.toast-leave-active {
  transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1),
    opacity 0.2s ease;
}
.toast-enter-from {
  opacity: 0;
  transform: translateY(-14px) scale(0.98);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(24px);
}
.toast-move {
  transition: transform 0.25s ease;
}

@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active,
  .toast-move {
    transition: none;
  }
}
</style>
