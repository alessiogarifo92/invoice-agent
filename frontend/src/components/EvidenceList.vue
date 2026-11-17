<script setup lang="ts">
// Rule evidence first (phase 3, "Agent"): one numbered item per flag, the most severe first.
// The numbers match the key in the PDF drawer.
import { computed } from "vue";

import type { Evidence, Flag } from "../api/types";
import { describeFlag, evidencePair, flagQuotes, flagTitle, sortFlags } from "../lib/flags";

// leadShown: the verdict above already quotes the first item's sentence; it is not repeated.
const props = defineProps<{ flags: Flag[]; evidence: Evidence[]; leadShown?: boolean }>();

const items = computed(() =>
  sortFlags(props.flags).map((flag, i) => ({
    number: i + 1,
    flag,
    title: flagTitle(flag.code),
    sentence: describeFlag(flag),
    pair: evidencePair(flag, props.evidence),
    quotes: flagQuotes(flag),
    tone: flag.severity === "critical" ? "critical" : "warning",
  })),
);

const SEVERITY: Record<string, string> = {
  critical: "Fraud or manipulation signal",
  warning: "Needs a check",
  info: "Note",
};
</script>

<template>
  <ol v-if="items.length" class="evidence">
    <li v-for="item in items" :key="item.number" class="evidence-item" data-testid="evidence-item">
      <div class="evidence-head">
        <span class="evidence-number mono" aria-hidden="true">{{ item.number }}</span>
        <div class="evidence-heading">
          <h3>{{ item.title }}</h3>
          <p class="evidence-meta small">
            <span :class="`severity severity-${item.tone}`">{{ SEVERITY[item.flag.severity] ?? item.flag.severity }}</span>
            <span class="muted">· rule</span>
            <code class="mono muted">{{ item.flag.code }}</code>
          </p>
        </div>
      </div>
      <p v-if="!(leadShown && item.number === 1)" class="evidence-sentence">{{ item.sentence }}</p>
      <div v-if="item.pair" class="pair">
        <div v-if="item.pair.invoice.length" class="pair-side" :class="`pair-invoice pair-${item.tone}`">
          <p class="pair-heading">On the invoice</p>
          <dl>
            <div v-for="row in item.pair.invoice" :key="row.label" class="pair-row">
              <dt>{{ row.label }}</dt>
              <dd :class="{ mono: row.mono }">{{ row.value }}</dd>
            </div>
          </dl>
        </div>
        <div class="pair-side pair-record">
          <p class="pair-heading">{{ item.pair.recordHeading }}</p>
          <dl>
            <div v-for="row in item.pair.record" :key="row.label" class="pair-row">
              <dt>{{ row.label }}</dt>
              <dd :class="{ mono: row.mono }">{{ row.value }}</dd>
            </div>
          </dl>
        </div>
      </div>
      <figure v-for="(quote, q) in item.quotes" :key="q" class="quote">
        <blockquote class="mono">{{ quote }}</blockquote>
        <figcaption class="small muted">
          Quoted from the document as found · <code class="mono">{{ item.flag.code }}</code>
        </figcaption>
      </figure>
    </li>
  </ol>
  <p v-else class="evidence-none">
    No check raised a flag: the invoice matches its purchase order, the contract prices and the
    vendor record.
  </p>
</template>

<style scoped>
.evidence {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-32);
}
.evidence-item {
  display: grid;
  gap: var(--space-12);
}
.evidence-head {
  display: flex;
  gap: var(--space-12);
  align-items: flex-start;
}
.evidence-number {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-pill);
  background: var(--ink);
  color: var(--ground);
  font-size: 13px;
  font-weight: 500;
}
.evidence-heading {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.evidence-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0 var(--space-4);
  align-items: baseline;
}
.severity {
  font-weight: 600;
}
.severity-critical {
  color: var(--block);
}
.severity-warning {
  color: var(--clarify);
}
.evidence-sentence {
  overflow-wrap: anywhere;
}
.pair {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-12);
}
.pair-side {
  flex: 1 1 260px;
  min-width: 0;
  padding: var(--space-16);
  border-radius: var(--radius);
}
.pair-critical {
  background: var(--block-bg);
}
.pair-warning {
  background: var(--clarify-bg);
}
.pair-record {
  background: var(--tint);
}
.pair-heading {
  margin-bottom: var(--space-8);
  font-size: 14px;
  font-weight: 600;
}
dl {
  margin: 0;
  display: grid;
  gap: var(--space-8);
}
.pair-row {
  display: grid;
  gap: 0;
}
dt {
  font-size: 13px;
  color: var(--muted);
}
.pair-critical dt,
.pair-warning dt {
  color: var(--ink);
  opacity: 0.8;
}
dd {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
dd.mono {
  font-size: 14px;
}
.quote {
  margin: 0;
  display: grid;
  gap: var(--space-4);
}
blockquote {
  margin: 0;
  padding: var(--space-12) var(--space-16);
  border: 1px solid rgba(180, 35, 24, 0.25);
  border-radius: var(--radius);
  background: var(--block-bg);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 14px;
}
code {
  font-size: 13px;
}
.evidence-none {
  padding: var(--space-16);
  border-radius: var(--radius);
  background: var(--tint);
}
</style>
