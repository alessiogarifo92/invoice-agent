<script setup lang="ts">
import { computed } from "vue";

import { stageLabel } from "../lib/status";

const props = defineProps<{ stage: string | null }>();
const STAGES = ["extract", "rules", "agent", "propose"];
const current = computed(() => (props.stage ? STAGES.indexOf(props.stage) : -1));
</script>

<template>
  <ol class="stages">
    <li
      v-for="(stage, i) in STAGES"
      :key="stage"
      class="stage"
      :class="{ done: i < current, current: i === current }"
      :aria-current="i === current ? 'step' : undefined"
    >
      <span class="stage-dot" aria-hidden="true"></span>
      <span>{{ stageLabel(stage) }}</span>
      <span v-if="i < current" class="visually-hidden">(done)</span>
    </li>
  </ol>
</template>

<style scoped>
.stages {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-8);
}
.stage {
  display: flex;
  align-items: center;
  gap: var(--space-12);
  color: var(--muted);
  font-size: 15px;
  transition: color var(--dur-base) var(--ease-out);
}
.stage-dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid var(--rule);
  transition:
    background-color var(--dur-base) var(--ease-out),
    border-color var(--dur-base) var(--ease-out);
}
.stage.done .stage-dot {
  background: var(--accent);
  border-color: var(--accent);
}
.stage.current {
  color: var(--ink);
  font-weight: 600;
}
.stage.current .stage-dot {
  border-color: var(--accent);
}
</style>
