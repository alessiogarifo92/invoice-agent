<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";

import type { InvoiceCard } from "../api/types";
import { formatDateTime, formatMoney } from "../lib/format";
import { statusView } from "../lib/status";
import ActionPill from "./ActionPill.vue";

const props = defineProps<{ card: InvoiceCard }>();

const view = computed(() => statusView(props.card.status, props.card.final_action));
const title = computed(() => props.card.vendor_name ?? props.card.filename);
// The filename is the title until a vendor is known; then the printed number sits beside it.
const reference = computed(() => (props.card.vendor_name ? (props.card.invoice_number ?? props.card.filename) : null));
</script>

<template>
  <li class="case-item">
    <RouterLink
      class="case"
      :class="{ closed: view.closed }"
      :to="{ name: 'review', params: { id: card.id } }"
      data-testid="case-card"
    >
      <span class="case-pill">
        <ActionPill :action="card.final_action" :fallback="view.short" />
      </span>
      <span class="case-main">
        <span class="case-head">
          <span class="case-title">{{ title }}</span>
          <span v-if="reference" class="case-ref mono">{{ reference }}</span>
        </span>
        <span class="case-summary">{{ view.summary }}</span>
      </span>
      <span class="case-side">
        <span class="case-total mono">{{ formatMoney(card.total, card.currency) }}</span>
        <span class="case-status">{{ view.label }}</span>
        <span class="case-date small muted">{{ formatDateTime(card.created_at) }}</span>
      </span>
    </RouterLink>
  </li>
</template>

<style scoped>
.case-item {
  list-style: none;
}
.case {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr) auto;
  gap: var(--space-8) var(--space-24);
  align-items: start;
  min-height: var(--target);
  padding: var(--space-16) var(--space-24);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--ground);
  color: var(--ink);
  text-decoration: none;
  transition: border-color var(--dur-fast) var(--ease-out), background-color var(--dur-fast) var(--ease-out);
}
.case.closed {
  background: var(--tint);
  border-color: var(--tint);
}
@media (hover: hover) and (pointer: fine) {
  .case:hover {
    border-color: var(--muted);
  }
}
.case-pill {
  padding-top: 2px;
}
.case-main {
  display: grid;
  gap: var(--space-4);
  min-width: 0;
}
.case-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-4) var(--space-12);
  min-width: 0;
}
.case-title {
  font: 600 18px/1.3 var(--font-display);
  overflow-wrap: anywhere;
}
.case-ref {
  color: var(--muted);
  overflow-wrap: anywhere;
}
.case-summary {
  color: var(--muted);
  font-size: 15px;
}
.case-side {
  display: grid;
  justify-items: end;
  gap: 2px;
  text-align: right;
}
.case-total {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
}
.case-status {
  font-size: 14px;
  font-weight: 500;
}
.case-date {
  white-space: nowrap;
}
@media (max-width: 720px) {
  .case {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--space-8) var(--space-12);
    padding: var(--space-16);
  }
  .case-pill {
    padding: 0;
  }
  /* The side column dissolves: total beside the pill, date under the summary. */
  .case-side {
    display: contents;
  }
  .case-total {
    grid-row: 1;
    grid-column: 2;
    align-self: center;
  }
  .case-main {
    grid-column: 1 / -1;
  }
  .case-status {
    display: none;
  }
  .case-date {
    grid-column: 1 / -1;
    text-align: left;
  }
}
</style>
