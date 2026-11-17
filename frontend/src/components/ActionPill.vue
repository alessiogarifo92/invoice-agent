<script setup lang="ts">
// The action is never colour alone: the pill always carries its word (DESIGN.md, "Palette").
import type { Action } from "../api/types";
import { ACTION_LABEL } from "../lib/status";

defineProps<{ action: Action | null; fallback?: string }>();
</script>

<template>
  <span class="pill" :class="action ? `pill-${action}` : 'pill-none'">
    {{ action ? ACTION_LABEL[action] : (fallback ?? "No action yet") }}
  </span>
</template>

<style scoped>
.pill {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 2px 12px;
  border-radius: var(--radius-pill);
  font: 600 13px/1.2 var(--font-body);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.pill-approve {
  background: var(--approve-bg);
  color: var(--approve);
}
.pill-clarify {
  background: var(--clarify-bg);
  color: var(--clarify);
}
.pill-block {
  background: var(--block-bg);
  color: var(--block);
}
.pill-none {
  background: var(--ground);
  color: var(--muted);
  box-shadow: inset 0 0 0 1px var(--rule);
}
</style>
