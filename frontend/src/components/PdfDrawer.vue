<script setup lang="ts">
// The PDF is a side drawer, not the main stage (DESIGN.md, "Thesis"). A modal <dialog>:
// focus moves in and is trapped, Escape closes it, focus returns to the trigger. Browsers
// without an inline PDF viewer (most phones) get the link instead of an empty frame.
import { nextTick, ref, watch } from "vue";

const props = defineProps<{
  open: boolean;
  src: string;
  filename: string;
  markers: Array<{ number: number; title: string }>;
}>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLDialogElement | null>(null);
const inlineViewer = typeof navigator !== "undefined" && navigator.pdfViewerEnabled === true;
let opener: HTMLElement | null = null;

watch(
  () => props.open,
  async (open) => {
    const el = dialog.value;
    if (!el || typeof el.showModal !== "function") return;
    if (open && !el.open) {
      opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      el.showModal();
      await nextTick();
      el.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    } else if (!open && el.open) {
      el.close();
    }
  },
);

function onClose(): void {
  emit("close");
  opener?.focus();
  opener = null;
}

function onBackdrop(event: MouseEvent): void {
  if (event.target === dialog.value) dialog.value?.close();
}
</script>

<template>
  <dialog
    ref="dialog"
    class="drawer"
    aria-labelledby="pdf-drawer-title"
    data-testid="pdf-drawer"
    @close="onClose"
    @click="onBackdrop"
  >
    <div class="drawer-panel">
      <header class="drawer-head">
        <div class="drawer-title">
          <h2 id="pdf-drawer-title">The invoice PDF</h2>
          <p class="mono muted drawer-file">{{ filename }}</p>
        </div>
        <button type="button" class="btn" data-autofocus @click="dialog?.close()">Close</button>
      </header>
      <ol v-if="markers.length" class="drawer-key" aria-label="Evidence in this case">
        <li v-for="m in markers" :key="m.number">
          <span class="key-number mono" aria-hidden="true">{{ m.number }}</span>
          <span>{{ m.title }}</span>
        </li>
      </ol>
      <iframe v-if="open && inlineViewer" class="drawer-frame" :src="src" :title="`PDF of ${filename}`"></iframe>
      <p v-else-if="!inlineViewer" class="drawer-fallback">
        This browser does not show PDFs inside the page. Open it in its own tab instead.
      </p>
      <p class="drawer-open">
        <a class="btn" :href="src" target="_blank" rel="noopener noreferrer">Open the PDF in a new tab</a>
      </p>
    </div>
  </dialog>
</template>

<style scoped>
.drawer {
  position: fixed;
  inset: 0 0 0 auto;
  width: min(640px, 100vw);
  max-width: 100vw;
  height: 100dvh;
  max-height: 100dvh;
  margin: 0 0 0 auto;
  padding: 0;
  border: 0;
  border-left: 1px solid var(--rule);
  background: var(--ground);
  color: var(--ink);
  transform: translateX(0);
  transition: transform var(--dur-move) var(--ease-in-out);
}
@starting-style {
  .drawer[open] {
    transform: translateX(100%);
  }
}
.drawer::backdrop {
  background: rgba(20, 22, 31, 0.32);
}
.drawer-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-16);
  height: 100%;
  padding: calc(var(--space-16) + env(safe-area-inset-top)) var(--space-24)
    calc(var(--space-16) + env(safe-area-inset-bottom));
}
.drawer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-16);
}
.drawer-title {
  min-width: 0;
}
.drawer-file {
  overflow-wrap: anywhere;
}
.drawer-key {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8) var(--space-16);
  font-size: 14px;
}
.drawer-key li {
  display: inline-flex;
  align-items: center;
  gap: var(--space-8);
}
.key-number {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--ink);
  color: var(--ground);
  font-size: 12px;
}
.drawer-frame {
  flex: 1 1 auto;
  width: 100%;
  min-height: 0;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--tint);
}
.drawer-fallback {
  padding: var(--space-16);
  border-radius: var(--radius);
  background: var(--tint);
}
@media (max-width: 560px) {
  .drawer {
    border-left: 0;
  }
  .drawer-panel {
    padding-left: var(--space-16);
    padding-right: var(--space-16);
  }
  .drawer-open .btn {
    width: 100%;
  }
}
</style>
