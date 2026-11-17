<script setup lang="ts">
// DESIGN.md "Gate line": the rule floor, the agent's action and the proposed max of both.
import type { Action } from "../api/types";
import ActionPill from "./ActionPill.vue";

defineProps<{ ruleFloor: Action; agentAction: Action | null; finalAction: Action }>();
</script>

<template>
  <div class="gate">
    <ol class="gate-line" data-testid="gate-line">
      <li class="gate-step">
        <span class="gate-label">Rule checks</span>
        <ActionPill :action="ruleFloor" />
      </li>
      <li class="gate-step" aria-hidden="true"><span class="gate-arrow">→</span></li>
      <li class="gate-step">
        <span class="gate-label">Agent</span>
        <ActionPill :action="agentAction" fallback="No action given" />
      </li>
      <li class="gate-step" aria-hidden="true"><span class="gate-arrow">→</span></li>
      <li class="gate-step gate-final">
        <span class="gate-label">Proposed</span>
        <ActionPill :action="finalAction" />
      </li>
    </ol>
    <p class="gate-note">
      The agent can raise the action but never lower it below what the rules require.
    </p>
  </div>
</template>

<style scoped>
.gate {
  display: grid;
  gap: var(--space-12);
}
.gate-line {
  list-style: none;
  margin: 0;
  padding: var(--space-16);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8) var(--space-12);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
}
.gate-step {
  display: inline-flex;
  align-items: center;
  gap: var(--space-8);
}
.gate-label {
  font-size: 15px;
  font-weight: 500;
}
.gate-arrow {
  color: var(--muted);
  font-family: var(--font-mono);
}
.gate-final .gate-label {
  font-weight: 700;
}
.gate-note {
  color: var(--muted);
  font-size: 15px;
}
</style>
