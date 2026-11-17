<script setup lang="ts">
// DESIGN.md "Audit chain": this case's stamps, the reviewer's step greyed until decided,
// and the whole chain's verification (GET /audit/verify). Only the events this screen knows
// the time of are stamped; per-event hashes are not exposed by the API.
import type { AuditVerify, Decision } from "../api/types";
import { formatDateTime, shortHash } from "../lib/format";
import { ACTION_LABEL } from "../lib/status";

defineProps<{
  receivedAt: string;
  proposedAt: string | null;
  decision: Decision | null;
  verify: AuditVerify | null;
  verifyFailed: boolean;
}>();
</script>

<template>
  <div class="audit">
    <ol class="stamps">
      <li class="stamp">
        <span class="stamp-actor mono">api</span>
        <span class="stamp-event">Invoice received</span>
        <time class="stamp-time mono" :datetime="receivedAt">{{ formatDateTime(receivedAt) }}</time>
      </li>
      <li v-if="proposedAt" class="stamp">
        <span class="stamp-actor mono">worker</span>
        <span class="stamp-event">Proposal created</span>
        <time class="stamp-time mono" :datetime="proposedAt">{{ formatDateTime(proposedAt) }}</time>
      </li>
      <Transition name="stamp">
        <li v-if="decision" key="decided" class="stamp" data-testid="stamp-decided">
          <span class="stamp-actor mono">{{ decision.decided_by }}</span>
          <span class="stamp-event">Decided: {{ ACTION_LABEL[decision.action] }}</span>
          <time class="stamp-time mono" :datetime="decision.decided_at">{{
            formatDateTime(decision.decided_at)
          }}</time>
        </li>
        <li v-else-if="proposedAt" key="pending" class="stamp stamp-pending">
          <span class="stamp-actor mono">reviewer</span>
          <span class="stamp-event">Waiting for a decision</span>
        </li>
      </Transition>
    </ol>
    <p v-if="verify && verify.ok" class="chain chain-ok" data-testid="audit-ok">
      <span class="chain-dot" aria-hidden="true"></span>
      <span>
        Chain verified: {{ verify.checked.toLocaleString("en-GB") }} events, none altered.
        <template v-if="verify.head_hash">
          Head <span class="mono">#{{ shortHash(verify.head_hash) }}</span>
        </template>
      </span>
    </p>
    <p v-else-if="verify" class="chain chain-broken" data-testid="audit-broken">
      <span class="chain-dot" aria-hidden="true"></span>
      <span>
        Chain broken at event <span class="mono">{{ verify.first_broken_seq ?? "?" }}</span>: the
        log was altered after it was written.
      </span>
    </p>
    <p v-else-if="verifyFailed" class="chain muted">The chain could not be verified right now.</p>
    <p v-else class="chain muted">Verifying the chain…</p>
  </div>
</template>

<style scoped>
.audit {
  display: grid;
  gap: var(--space-16);
}
.stamps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-12);
  position: relative;
}
.stamp {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-4) var(--space-12);
  margin-left: var(--space-24);
  padding: var(--space-12) var(--space-16);
  border-radius: var(--radius);
  background: var(--tint);
}
.stamp::before {
  content: "";
  position: absolute;
  left: calc(-1 * var(--space-24) + 1px);
  top: 20px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
}
.stamp:not(:last-child)::after {
  content: "";
  position: absolute;
  left: calc(-1 * var(--space-24) + 5px);
  top: 30px;
  bottom: calc(-1 * var(--space-12) - 20px);
  width: 2px;
  background: var(--rule);
}
.stamp-pending {
  background: var(--ground);
  border: 1px dashed var(--rule);
  color: var(--muted);
}
.stamp-pending::before {
  background: var(--rule);
}
.stamp-actor {
  color: var(--muted);
  overflow-wrap: anywhere;
}
.stamp-event {
  font-weight: 600;
  font-size: 15px;
}
.stamp-time {
  margin-left: auto;
  color: var(--muted);
}
.chain {
  display: flex;
  gap: var(--space-8);
  align-items: baseline;
  font-size: 15px;
}
.chain-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  transform: translateY(-1px);
}
.chain-ok .chain-dot {
  background: var(--accent);
}
.chain-broken {
  color: var(--block);
  font-weight: 500;
}
.chain-broken .chain-dot {
  background: var(--block);
}
.stamp-enter-active {
  transition:
    opacity var(--dur-slow) var(--ease-out),
    transform var(--dur-enter) var(--ease-out);
}
.stamp-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.stamp-leave-active {
  display: none;
}
@media (max-width: 560px) {
  .stamp-time {
    margin-left: 0;
    width: 100%;
  }
}
</style>
