<script setup lang="ts">
// One case, read top to bottom (DESIGN.md "C · Case File — Review"): verdict, evidence, how
// the action was set, the agent's reading, the decision, the audit chain. The invoice is
// polled while the worker runs; the proposal is read once it is ready, with its ETag.
import { computed, onMounted, ref, shallowRef, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "../api";
import { ApiError, isUnreachable, userMessage } from "../api/client";
import type { Action, AuditVerify, Proposal } from "../api/types";
import ActionPill from "../components/ActionPill.vue";
import AgentReading from "../components/AgentReading.vue";
import AuditChain from "../components/AuditChain.vue";
import DecisionPanel from "../components/DecisionPanel.vue";
import EvidenceList from "../components/EvidenceList.vue";
import GateLine from "../components/GateLine.vue";
import PdfDrawer from "../components/PdfDrawer.vue";
import PlainProse from "../components/PlainProse.vue";
import StageProgress from "../components/StageProgress.vue";
import { usePolling } from "../composables/usePolling";
import { useServerHealth } from "../composables/useServer";
import { flagTitle, sortFlags, verdictSummary } from "../lib/flags";
import { formatDateTime } from "../lib/format";
import { ACTION_LABEL, extractionFailed, isTerminal, stageLabel, statusView } from "../lib/status";

const props = defineProps<{ id: string }>();
const route = useRoute();
const health = useServerHealth();

const invoicePoll = usePolling(() => api.getInvoice(props.id), {
  intervalMs: 2500,
  isDone: (invoice) => isTerminal(invoice.status),
});

const proposal = shallowRef<Proposal | null>(null);
const etag = ref<string | null>(null);
const proposalError = ref<unknown>(null);
const audit = shallowRef<AuditVerify | null>(null);
const auditFailed = ref(false);
const drawerOpen = ref(false);

async function loadProposal(proposalId: string): Promise<void> {
  proposalError.value = null;
  try {
    const loaded = await api.getProposal(proposalId);
    proposal.value = loaded.proposal;
    etag.value = loaded.etag;
  } catch (err) {
    proposalError.value = err;
    if (isUnreachable(err)) health.reportUnreachable();
  }
}

async function loadAudit(): Promise<void> {
  auditFailed.value = false;
  try {
    audit.value = await api.verifyAudit();
  } catch {
    auditFailed.value = true;
  }
}

function reloadCase(): void {
  proposal.value = null;
  etag.value = null;
  invoicePoll.refresh();
  void loadAudit();
}

onMounted(() => {
  invoicePoll.start();
  void loadAudit();
});

watch(
  () => props.id,
  () => reloadCase(),
);

const invoice = computed(() => invoicePoll.data.value);
watch(invoice, (current) => {
  if (current && isTerminal(current.status) && current.proposal_id && proposal.value?.id !== current.proposal_id) {
    void loadProposal(current.proposal_id);
  }
});
watch(invoicePoll.error, (err) => {
  if (isUnreachable(err)) health.reportUnreachable();
});

const view = computed(() => (invoice.value ? statusView(invoice.value.status) : null));
const notFound = computed(
  () => invoicePoll.error.value instanceof ApiError && invoicePoll.error.value.kind === "not_found",
);
const firstLoadFailed = computed(() => invoice.value === null && invoicePoll.error.value !== null);
const flags = computed(() => (proposal.value ? sortFlags(proposal.value.flags) : []));
const markers = computed(() => flags.value.map((f, i) => ({ number: i + 1, title: flagTitle(f.code) })));
const readFailed = computed(() => extractionFailed(flags.value));
const canDecide = computed(
  () =>
    proposal.value !== null &&
    etag.value !== null &&
    proposal.value.decision === null &&
    proposal.value.is_current &&
    proposal.value.invoice_status === "pending_approval",
);
const uploaded = computed(() => route.query.uploaded);

const HEADLINES: Record<Action, string> = {
  block: "Block this payment",
  clarify: "Ask the vendor before paying",
  approve: "Ready to approve",
};

// Announced politely as the case moves through the pipeline.
const liveStatus = computed(() => {
  if (!invoice.value || !view.value) return "";
  if (view.value.state === "processing") {
    return `${view.value.label}: ${stageLabel(invoice.value.job?.stage ?? "")}.`;
  }
  return `Status: ${view.value.label}.`;
});
</script>

<template>
  <article class="case-file">
    <nav class="crumbs" aria-label="Breadcrumb">
      <RouterLink to="/" class="back">← Inbox</RouterLink>
    </nav>
    <p class="visually-hidden" aria-live="polite" data-testid="live-status">{{ liveStatus }}</p>

    <div v-if="notFound" class="state" data-testid="case-not-found">
      <h1>This case does not exist</h1>
      <p>The link may be wrong, or the demo data was reset.</p>
      <RouterLink class="btn" to="/">Back to the inbox</RouterLink>
    </div>

    <div v-else-if="firstLoadFailed" class="state" role="alert" data-testid="case-error">
      <h1>The case could not be loaded</h1>
      <p>{{ userMessage(invoicePoll.error.value) }} This page retries on its own.</p>
      <button type="button" class="btn" @click="invoicePoll.refresh()">Try again now</button>
    </div>

    <div v-else-if="!invoice" class="loading" aria-busy="true" data-testid="case-loading">
      <span class="visually-hidden">Loading the case…</span>
      <div class="skeleton" style="height: 20px; width: 40%" aria-hidden="true"></div>
      <div class="skeleton" style="height: 44px; width: 80%" aria-hidden="true"></div>
      <div class="skeleton" style="height: 160px" aria-hidden="true"></div>
    </div>

    <template v-else>
      <header class="case-meta">
        <p class="mono muted meta-line">
          <span>{{ invoice.filename }}</span>
          <span aria-hidden="true">·</span>
          <span>received {{ formatDateTime(invoice.created_at) }}</span>
        </p>
        <button type="button" class="btn" data-testid="open-pdf" @click="drawerOpen = true">View the PDF</button>
      </header>

      <p v-if="uploaded" class="note small" data-testid="uploaded-note">
        {{
          uploaded === "existing"
            ? "This file was uploaded before: this is its case."
            : "Uploaded. The case below updates on its own."
        }}
      </p>

      <!-- Being processed -->
      <section v-if="view?.state === 'processing'" class="verdict" data-testid="case-processing">
        <p class="eyebrow">{{ view.label }}</p>
        <h1>Reading and checking this invoice</h1>
        <p class="lead">This usually takes under a minute. The page updates on its own.</p>
        <StageProgress :stage="invoice.job?.stage ?? null" />
      </section>

      <!-- Paused by the demo budget -->
      <section v-else-if="view?.state === 'quota'" class="verdict" data-testid="case-quota">
        <div class="banner" role="status">
          <p class="banner-text">
            <strong>The demo budget for today is used up.</strong> This invoice waits until
            {{ invoice.job ? formatDateTime(invoice.job.run_after) : "00:00 UTC" }}; every case already
            processed stays fully reviewable.
          </p>
          <button type="button" class="btn" @click="invoicePoll.refresh()">Check again</button>
        </div>
        <h1>Paused until the budget resets</h1>
        <p class="lead">
          The work done so far is kept; it resumes at the step “{{
            stageLabel(invoice.job?.stage ?? "").toLowerCase()
          }}”. Nothing was decided and nothing was paid.
        </p>
      </section>

      <!-- Processing failed -->
      <section v-else-if="view?.state === 'failed'" class="verdict" data-testid="case-failed">
        <p class="eyebrow eyebrow-block">Could not be processed</p>
        <h1>This invoice could not be processed</h1>
        <p class="lead">
          The pipeline stopped
          <template v-if="invoice.job">
            at the step “{{ stageLabel(invoice.job.stage).toLowerCase() }}” after
            {{ invoice.job.attempts }} {{ invoice.job.attempts === 1 ? "attempt" : "attempts" }}</template
          >. Nothing was decided and nothing was paid. The PDF is still available above.
        </p>
      </section>

      <!-- Waiting for the proposal -->
      <div v-else-if="!proposal && proposalError" class="state" role="alert" data-testid="proposal-error">
        <h2>The proposal could not be loaded</h2>
        <p>{{ userMessage(proposalError) }}</p>
        <button type="button" class="btn" @click="reloadCase">Try again</button>
      </div>
      <div v-else-if="!proposal" class="loading" aria-busy="true" data-testid="proposal-loading">
        <span class="visually-hidden">Loading the proposal…</span>
        <div class="skeleton" style="height: 44px; width: 80%" aria-hidden="true"></div>
        <div class="skeleton" style="height: 160px" aria-hidden="true"></div>
      </div>

      <template v-else>
        <section class="verdict" aria-labelledby="verdict-title" data-testid="verdict">
          <div class="verdict-label">
            <span class="eyebrow">Proposed action</span>
            <ActionPill :action="proposal.final_action" />
            <span v-if="view?.closed" class="eyebrow">· {{ view.label }}</span>
          </div>
          <h1 id="verdict-title">{{ HEADLINES[proposal.final_action] }}</h1>
          <!-- An unreadable invoice is explained by the banner below instead. -->
          <p v-if="!readFailed" class="lead">{{ verdictSummary(flags) }}</p>
          <div v-if="readFailed" class="banner" data-testid="extraction-failed">
            <p class="banner-text">
              <strong>The invoice's fields could not be read reliably.</strong> The checks ran on what
              could be read, so compare the evidence with the PDF before deciding.
            </p>
          </div>
          <div v-if="!proposal.is_current" class="banner" role="status">
            <p class="banner-text">A newer proposal replaced this one.</p>
            <button type="button" class="btn" @click="reloadCase">Show the newest</button>
          </div>
        </section>

        <section class="section" aria-labelledby="evidence-title">
          <h2 id="evidence-title">Evidence</h2>
          <EvidenceList :flags="flags" :evidence="proposal.evidence" lead-shown />
        </section>

        <section class="section" aria-labelledby="gate-title">
          <h2 id="gate-title">How the action was set</h2>
          <GateLine
            :rule-floor="proposal.rule_floor"
            :agent-action="proposal.agent_action"
            :final-action="proposal.final_action"
          />
        </section>

        <section class="section" aria-labelledby="agent-title">
          <h2 id="agent-title">The agent's reading</h2>
          <AgentReading :rationale="proposal.rationale" :evidence="proposal.evidence" />
        </section>

        <section class="section" aria-labelledby="decision-title">
          <h2 id="decision-title">Your decision</h2>
          <div v-if="proposal.decision" class="decided" data-testid="decision-recorded">
            <p>
              <strong>Decided: {{ ACTION_LABEL[proposal.decision.action] }}</strong>
              <span class="muted">
                by <span class="mono">{{ proposal.decision.decided_by }}</span>,
                {{ formatDateTime(proposal.decision.decided_at) }}</span
              >
            </p>
            <PlainProse v-if="proposal.decision.note" :text="proposal.decision.note" class="decided-note" />
          </div>
          <DecisionPanel
            v-else-if="canDecide && etag"
            :proposal="proposal"
            :etag="etag"
            @decided="reloadCase"
            @reload="reloadCase"
          />
          <p v-else class="muted">This proposal is not waiting for a decision.</p>
        </section>

        <section class="section" aria-labelledby="audit-title">
          <h2 id="audit-title">Audit chain</h2>
          <AuditChain
            :received-at="invoice.created_at"
            :proposed-at="proposal.created_at"
            :decision="proposal.decision"
            :verify="audit"
            :verify-failed="auditFailed"
          />
        </section>
      </template>

      <PdfDrawer
        :open="drawerOpen"
        :src="api.pdfUrl(invoice.id)"
        :filename="invoice.filename"
        :markers="markers"
        @close="drawerOpen = false"
      />
    </template>
  </article>
</template>

<style scoped>
.case-file {
  max-width: var(--measure);
  margin: 0 auto;
  display: grid;
  gap: var(--space-48);
}
.crumbs {
  margin-bottom: calc(-1 * var(--space-32));
}
.back {
  display: inline-flex;
  align-items: center;
  min-height: var(--target);
  font-weight: 500;
  text-decoration: none;
}
.case-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-12) var(--space-16);
  margin-bottom: calc(-1 * var(--space-24));
}
.meta-line {
  display: flex;
  flex-wrap: wrap;
  gap: 0 var(--space-8);
  min-width: 0;
  overflow-wrap: anywhere;
}
.note {
  padding: var(--space-12) var(--space-16);
  border-radius: var(--radius);
  background: var(--tint);
  margin-bottom: calc(-1 * var(--space-24));
}
.verdict {
  display: grid;
  gap: var(--space-16);
}
.verdict-label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8);
}
.eyebrow {
  font-size: 14px;
  font-weight: 600;
  color: var(--muted);
}
.eyebrow-block {
  color: var(--block);
}
.lead {
  font-size: 18px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.section {
  display: grid;
  gap: var(--space-24);
  padding-top: var(--space-32);
  border-top: 1px solid var(--rule);
}
.decided {
  display: grid;
  gap: var(--space-12);
  padding: var(--space-16);
  border-radius: var(--radius);
  background: var(--tint);
}
.decided-note {
  padding: var(--space-12) var(--space-16);
  border-radius: var(--radius);
  background: var(--ground);
}
.state {
  display: grid;
  justify-items: start;
  gap: var(--space-12);
  padding: var(--space-32);
  border-radius: var(--radius);
  background: var(--tint);
}
.state h1 {
  font-size: 30px;
}
.loading {
  display: grid;
  gap: var(--space-16);
}
@media (max-width: 560px) {
  .case-file {
    gap: var(--space-32);
  }
  .crumbs {
    margin-bottom: calc(-1 * var(--space-16));
  }
  .case-meta,
  .note {
    margin-bottom: calc(-1 * var(--space-8));
  }
  h1 {
    font-size: 32px;
  }
  .lead {
    font-size: 17px;
  }
  .section {
    padding-top: var(--space-24);
  }
  .state {
    padding: var(--space-24) var(--space-16);
  }
}
</style>
