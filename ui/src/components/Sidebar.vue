<script setup>
import { ref, nextTick } from 'vue'

const props = defineProps({
  conversations: { type: Array, default: () => [] },
  currentId: { type: String, default: null },
  loading: { type: Boolean, default: false },
  // 请求进行中（上传/流式）：禁止新建，避免与发送守卫产生幽灵对话
  busy: { type: Boolean, default: false }
})
const emit = defineEmits(['new', 'select', 'delete', 'rename'])

// 行内重命名状态
const editingId = ref(null)
const draft = ref('')
const editInput = ref(null)

async function startEdit(c) {
  editingId.value = c.id
  draft.value = c.title || ''
  await nextTick()
  // 该 ref 位于 v-for 内，Vue 会把元素收集为数组（即使同时只有一个编辑框）
  const el = Array.isArray(editInput.value) ? editInput.value[0] : editInput.value
  el?.focus()
  el?.select()
}

function commitEdit(c) {
  if (editingId.value !== c.id) return
  const title = draft.value.trim()
  editingId.value = null
  // 空标题或与现名相同：静默还原（空标题由后端自动命名维护）
  if (title && title !== (c.title || '')) {
    emit('rename', c.id, title)
  }
}

function cancelEdit() {
  editingId.value = null
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="logo">AI</div>
      <div class="brand-text">
        <div class="brand-title">智能辅助编程</div>
        <div class="brand-sub">DeepSeek 驱动</div>
      </div>
    </div>

    <button class="new-btn" :disabled="busy" @click="emit('new')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
      新建对话
    </button>

    <div class="list">
      <div v-if="loading && !conversations.length" class="empty">加载中…</div>
      <div v-else-if="!conversations.length" class="empty">暂无对话<br />点击上方新建，或直接发送消息</div>
      <div
        v-for="c in conversations"
        :key="c.id"
        class="conv"
        :class="{ active: c.id === currentId, editing: editingId === c.id }"
        @click="emit('select', c.id)"
      >
        <svg class="conv-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        <input
          v-if="editingId === c.id"
          ref="editInput"
          v-model="draft"
          class="conv-edit"
          maxlength="50"
          @click.stop
          @dblclick.stop
          @keydown.enter.prevent="commitEdit(c)"
          @keydown.esc.prevent="cancelEdit"
          @blur="commitEdit(c)"
        />
        <span
          v-else
          class="conv-title"
          title="双击重命名"
          @dblclick.stop="startEdit(c)"
        >{{ c.title || '新对话' }}</span>
        <button
          v-if="editingId !== c.id"
          class="del"
          title="删除对话"
          @click.stop="emit('delete', c.id)"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100%;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--header-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}
.logo {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), #7c3aed);
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  letter-spacing: 0.5px;
}
.brand-title {
  font-size: 14px;
  font-weight: 600;
}
.brand-sub {
  font-size: 11px;
  color: var(--muted);
}
.new-btn {
  margin: 12px 12px 8px;
  height: 40px;
  border: 1px dashed var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: var(--radius);
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  transition: all 0.15s;
}
.new-btn:hover {
  background: var(--accent);
  color: #fff;
  border-style: solid;
}
.new-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.new-btn:disabled:hover {
  background: var(--accent-soft);
  color: var(--accent);
  border-style: dashed;
}
.list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 8px;
}
.empty {
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  padding: 24px 12px;
}
.conv {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--text);
  position: relative;
  transition: background 0.12s;
}
.conv:hover {
  background: var(--surface-2);
}
.conv.active {
  background: var(--accent-soft);
  color: var(--accent);
}
.conv-icon {
  flex-shrink: 0;
  color: var(--muted);
}
.conv.active .conv-icon {
  color: var(--accent);
}
.conv-title {
  flex: 1;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-title:hover {
  cursor: text;
}
.conv-edit {
  flex: 1;
  min-width: 0;
  height: 24px;
  padding: 0 7px;
  border: 1px solid var(--accent);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  outline: none;
}
.conv.editing {
  background: var(--surface-2);
}
.del {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--muted);
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: all 0.12s;
}
.conv:hover .del {
  opacity: 1;
}
.del:hover {
  background: var(--danger-soft);
  color: var(--danger);
}
</style>
