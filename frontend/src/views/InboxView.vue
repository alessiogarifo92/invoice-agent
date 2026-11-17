<script setup lang="ts">
// The inbox (DESIGN.md "C · Case File — Inbox"): case cards, filtered by status group and
// proposed action. The API has no filter parameters, so the newest page (up to 100 cases) is
// filtered here; it is polled while any of its cases is still being processed.
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { api } from "../api";
import { isUnreachable, userMessage } from "../api/client";
import type { InvoiceCard } from "../api/types";
import CaseCard from "../components/CaseCard.vue";
import FilterGroup from "../components/FilterGroup.vue";
import { usePolling } from "../composables/usePolling";
import { useServerHealth } from "../composables/useServer";
import {
  isTerminal,
  matchesFilter,
  type ActionFilter,
  type CaseFilter,
  type StatusFilter,
} from "../lib/status";

const PAGE = 100;
const route = useRoute();
const router = useRouter();
const health = useServerHealth();

const polling = usePolling(() => api.listInvoices(PAGE, 0), {
  intervalMs: 3000,
  isDone: (page) => page.items.every((item) => isTerminal(item.status)),
});
onMounted(() => polling.start());

watch(polling.error, (err) => {
  if (isUnreachable(err)) health.reportUnreachable();
});

// Older pages, loaded on demand; only the newest page is polled.
const older = ref<InvoiceCard[]>([]);
const loadingOlder = ref(false);
const olderError = ref<string | null>(null);

const cards = computed(() => {
  const first = polling.data.value?.items ?? [];
  const seen = new Set(first.map((c) => c.id));
  return [...first, ...older.value.filter((c) => !seen.has(c.id))];
});
const total = computed(() => polling.data.value?.total ?? 0);
const hasMore = computed(() => cards.value.length < total.value);

async function loadOlder(): Promise<void> {
  loadingOlder.value = true;
  olderError.value = null;
  try {
    const page = await api.listInvoices(PAGE, cards.value.length);
    older.value = [...older.value, ...page.items];
  } catch (err) {
    olderError.value = userMessage(err);
  } finally {
    loadingOlder.value = false;
  }
}

const STATUS_VALUES: StatusFilter[] = ["all", "review", "decided", "in_progress", "failed"];
const ACTION_VALUES: ActionFilter[] = ["all", "block", "clarify", "approve"];

function fromQuery<T extends string>(value: unknown, allowed: T[]): T {
  return allowed.includes(value as T) ? (value as T) : allowed[0];
}

const filter = computed<CaseFilter>(() => ({
  status: fromQuery(route.query.status, STATUS_VALUES),
  action: fromQuery(route.query.action, ACTION_VALUES),
}));

function setFilter(next: Partial<CaseFilter>): void {
  const merged = { ...filter.value, ...next };
  void router.replace({
    query: {
      ...(merged.status !== "all" ? { status: merged.status } : {}),
      ...(merged.action !== "all" ? { action: merged.action } : {}),
    },
  });
}

const statusModel = computed({
  get: () => filter.value.status,
  set: (status: StatusFilter) => setFilter({ status }),
});
const actionModel = computed({
  get: () => filter.value.action,
  set: (action: ActionFilter) => setFilter({ action }),
});

function count(f: CaseFilter): number {
  return cards.value.filter((c) => matchesFilter(c, f)).length;
}

const statusOptions = computed(() =>
  (
    [
      ["all", "All"],
      ["review", "Needs review"],
      ["decided", "Decided"],
      ["in_progress", "In progress"],
      ["failed", "Failed"],
    ] as const
  ).map(([value, label]) => ({ value, label, count: count({ status: value, action: filter.value.action }) })),
);
const actionOptions = computed(() =>
  (
    [
      ["all", "Any"],
      ["block", "Block"],
      ["clarify", "Clarify"],
      ["approve", "Approve"],
    ] as const
  ).map(([value, label]) => ({ value, label, count: count({ status: filter.value.status, action: value }) })),
);

const visible = computed(() => cards.value.filter((c) => matchesFilter(c, filter.value)));
const quotaPaused = computed(() => cards.value.some((c) => c.status === "awaiting_quota"));
const loading = computed(() => polling.data.value === null && polling.error.value === null);
const failedFirstLoad = computed(() => polling.data.value === null && polling.error.value !== null);
const processingCount = computed(() => cards.value.filter((c) => !isTerminal(c.status)).length);
</script>

<template>
  <div class="inbox">
    <header class="inbox-head">
      <h1>Inbox</h1>
      <p class="muted">
        Each invoice is a case: what was proposed, the evidence, and your decision.
        <template v-if="polling.data.value">
          <span class="mono" data-testid="case-count">{{ total }}</span> {{ total === 1 ? "case" : "cases" }}, all synthetic.
        </template>
      </p>
    </header>

    <div v-if="quotaPaused" class="banner" role="status" data-testid="quota-banner">
      <p class="banner-text">
        <strong>The demo budget for today is used up.</strong> Processing is paused until 00:00 UTC;
        every case already processed stays fully reviewable.
      </p>
    </div>

    <div v-if="polling.data.value" class="filters" role="search" aria-label="Filter cases">
      <FilterGroup v-model="statusModel" legend="Status" name="status" :options="statusOptions" />
      <FilterGroup v-model="actionModel" legend="Proposed action" name="action" :options="actionOptions" />
    </div>

    <p class="visually-hidden" aria-live="polite">
      <template v-if="polling.data.value">
        {{ visible.length }} {{ visible.length === 1 ? "case" : "cases" }} shown.
        <template v-if="processingCount">{{ processingCount }} still processing.</template>
      </template>
    </p>

    <div v-if="loading" class="skeletons" aria-busy="true" data-testid="inbox-loading">
      <span class="visually-hidden">Loading cases…</span>
      <div v-for="n in 4" :key="n" class="skeleton skeleton-card" aria-hidden="true"></div>
    </div>

    <div v-else-if="failedFirstLoad" class="state" role="alert" data-testid="inbox-error">
      <h2>{{ isUnreachable(polling.error.value) ? "Waiting for the server" : "The cases could not be loaded" }}</h2>
      <p>{{ userMessage(polling.error.value) }} This page retries on its own.</p>
      <button type="button" class="btn" @click="polling.refresh()">Try again now</button>
    </div>

    <div v-else-if="total === 0" class="state" data-testid="inbox-empty">
      <h2>No cases yet</h2>
      <p>
        The gallery of synthetic invoices appears here once it is loaded. With a demo token you can
        submit one yourself.
      </p>
      <RouterLink class="btn" to="/upload">Submit an invoice</RouterLink>
    </div>

    <div v-else-if="visible.length === 0" class="state" data-testid="inbox-no-match">
      <h2>No cases match these filters</h2>
      <p>{{ cards.length }} cases are loaded; none has this status and proposed action.</p>
      <button type="button" class="btn" @click="setFilter({ status: 'all', action: 'all' })">Clear the filters</button>
    </div>

    <template v-else>
      <ul class="cases" data-testid="case-list">
        <CaseCard v-for="card in visible" :key="card.id" :card="card" />
      </ul>
      <div v-if="hasMore" class="more">
        <button type="button" class="btn" :disabled="loadingOlder" @click="loadOlder">
          {{ loadingOlder ? "Loading…" : "Show older cases" }}
        </button>
        <p v-if="olderError" class="field-error" role="alert">{{ olderError }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.inbox {
  max-width: var(--inbox-width);
  margin: 0 auto;
  display: grid;
  gap: var(--space-32);
}
.inbox-head {
  display: grid;
  gap: var(--space-8);
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-16) var(--space-48);
}
.cases {
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-12);
}
.skeletons {
  display: grid;
  gap: var(--space-12);
}
.skeleton-card {
  height: 96px;
}
.state {
  display: grid;
  justify-items: start;
  gap: var(--space-12);
  padding: var(--space-32);
  border-radius: var(--radius);
  background: var(--tint);
}
.state p {
  max-width: 60ch;
}
.more {
  display: grid;
  justify-items: center;
  gap: var(--space-8);
}
@media (max-width: 560px) {
  .inbox {
    gap: var(--space-24);
  }
  h1 {
    font-size: 34px;
  }
  .state {
    padding: var(--space-24) var(--space-16);
  }
}
</style>
