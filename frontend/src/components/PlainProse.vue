<script setup lang="ts">
// Agent prose is attacker-influenced (phase 3, "Agent"): it is only ever interpolated as
// text, one paragraph per blank-line block. Never v-html (eslint enforces vue/no-v-html).
import { computed } from "vue";

import { paragraphs } from "../lib/format";

const props = withDefaults(defineProps<{ text: string; empty?: string }>(), { empty: "" });
const blocks = computed(() => paragraphs(props.text));
</script>

<template>
  <div class="prose">
    <p v-for="(block, i) in blocks" :key="i">{{ block }}</p>
    <p v-if="blocks.length === 0" class="prose-empty">{{ empty }}</p>
  </div>
</template>

<style scoped>
.prose p {
  margin: 0;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
.prose p + p {
  margin-top: var(--space-12);
}
.prose-empty {
  color: var(--muted);
}
</style>
