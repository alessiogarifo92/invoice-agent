<script setup lang="ts">
import type { Evidence } from "../api/types";
import { toolLabel } from "../lib/flags";
import PlainProse from "./PlainProse.vue";

defineProps<{ rationale: string; evidence: Evidence[] }>();
</script>

<template>
  <div class="agent">
    <p class="agent-caveat small muted">
      Written by the agent from the invoice's own text, which the vendor controls. Read it as an
      opinion: the rule evidence above comes first.
    </p>
    <PlainProse class="agent-prose" :text="rationale" empty="The agent left no reading for this case." data-testid="agent-reading" />
    <div v-if="evidence.length" class="agent-sources">
      <p class="small"><strong>It looked at</strong></p>
      <ul>
        <li v-for="item in evidence" :key="item.ref" class="small">
          {{ toolLabel(item.tool) }}<template v-if="item.records.length">
            · <span class="mono">{{ item.records.join(", ") }}</span></template
          >
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.agent {
  display: grid;
  gap: var(--space-16);
}
.agent-prose {
  padding: var(--space-16) var(--space-24);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--ground);
  font-size: 16px;
}
@media (max-width: 560px) {
  .agent-prose {
    padding: var(--space-12) var(--space-16);
  }
}
.agent-sources ul {
  margin: var(--space-4) 0 0;
  padding-left: var(--space-24);
  display: grid;
  gap: 2px;
}
.agent-sources li {
  overflow-wrap: anywhere;
}
</style>
