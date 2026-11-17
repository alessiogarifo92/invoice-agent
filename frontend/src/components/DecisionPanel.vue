<script setup lang="ts">
// The person's decision (T9): sent with the proposal's ETag in If-Match and the demo token.
// Lowering the proposed action needs a note, checked here first and by the server again.
import { computed, nextTick, ref, useId } from "vue";

import { api } from "../api";
import { ApiError, userMessage } from "../api/client";
import type { Action, Proposal } from "../api/types";
import { useDemoToken } from "../composables/useDemoToken";
import { ACTION_LABEL, ACTION_ORDER } from "../lib/status";
import TokenField from "./TokenField.vue";

const props = defineProps<{ proposal: Proposal; etag: string }>();
const emit = defineEmits<{ decided: []; reload: [] }>();

const store = useDemoToken();
const ACTIONS: Action[] = ["approve", "clarify", "block"];
const HINTS: Record<Action, string> = {
  approve: "Mark it approved for payment. Nothing is paid.",
  clarify: "Ask the vendor before anything happens.",
  block: "Stop this payment.",
};

const selected = ref<Action | null>(null);
const note = ref("");
const submitting = ref(false);
const error = ref<string | null>(null);
const noteError = ref<string | null>(null);
// The case changed or was decided elsewhere: the only way forward is a reload.
const needsReload = ref(false);
const noteInput = ref<HTMLTextAreaElement | null>(null);
const tokenField = ref<InstanceType<typeof TokenField> | null>(null);
const noteId = useId();

const lowered = computed(
  () => selected.value !== null && ACTION_ORDER[selected.value] < ACTION_ORDER[props.proposal.final_action],
);

function choose(action: Action): void {
  selected.value = action;
  error.value = null;
  noteError.value = null;
}

async function submit(): Promise<void> {
  if (selected.value === null || submitting.value) return;
  error.value = null;
  noteError.value = null;
  if (!store.token.value) {
    error.value = "Enter the demo token to record a decision.";
    await nextTick();
    tokenField.value?.focus();
    return;
  }
  const trimmed = note.value.trim();
  if (lowered.value && !trimmed) {
    noteError.value = "A note is required: say why the action is lower than proposed.";
    noteInput.value?.focus();
    return;
  }
  submitting.value = true;
  try {
    await api.decide(
      props.proposal.id,
      { action: selected.value, note: trimmed || null },
      props.etag,
      store.token.value,
    );
    emit("decided");
  } catch (err) {
    if (err instanceof ApiError && err.kind === "unauthorized") {
      // The token field shows the refusal next to the field to fix.
      store.reject();
      await nextTick();
      tokenField.value?.focus();
    } else if (err instanceof ApiError && err.code === "note_required") {
      noteError.value = userMessage(err);
      noteInput.value?.focus();
    } else {
      needsReload.value =
        err instanceof ApiError && ["stale", "precondition_required", "conflict"].includes(err.kind);
      error.value = userMessage(err);
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <form class="decision" data-testid="decision-panel" @submit.prevent="submit">
    <fieldset class="decision-choices">
      <legend class="visually-hidden">Choose an action</legend>
      <button
        v-for="action in ACTIONS"
        :key="action"
        type="button"
        class="choice"
        :class="[`choice-${action}`, { selected: selected === action }]"
        :aria-pressed="selected === action"
        :data-testid="`choose-${action}`"
        @click="choose(action)"
      >
        <span class="choice-top">
          <span class="choice-word">{{ ACTION_LABEL[action] }}</span>
          <span v-if="proposal.final_action === action" class="choice-tag">Proposed</span>
        </span>
        <span class="choice-hint">{{ HINTS[action] }}</span>
      </button>
    </fieldset>

    <div v-if="lowered" class="field">
      <label class="field-label" :for="noteId">Why lower the action? (required)</label>
      <p :id="`${noteId}-hint`" class="field-hint">
        {{ ACTION_LABEL[selected!] }} is less severe than the proposed
        {{ ACTION_LABEL[proposal.final_action] }}. The note goes into the audit chain.
      </p>
      <textarea
        :id="noteId"
        ref="noteInput"
        v-model="note"
        class="input note"
        rows="3"
        maxlength="2000"
        :aria-describedby="`${noteId}-hint${noteError ? ` ${noteId}-error` : ''}`"
        :aria-invalid="noteError ? 'true' : undefined"
        data-testid="decision-note"
      ></textarea>
      <p v-if="noteError" :id="`${noteId}-error`" class="field-error" role="alert">{{ noteError }}</p>
    </div>

    <TokenField ref="tokenField" purpose="Recording a decision needs the demo token." />

    <div class="decision-actions">
      <button
        type="submit"
        class="btn btn-primary decision-submit"
        :disabled="selected === null || submitting || needsReload"
        data-testid="decision-submit"
      >
        {{ submitting ? "Recording…" : selected ? `Record: ${ACTION_LABEL[selected]}` : "Record decision" }}
      </button>
    </div>

    <div v-if="error" class="banner" :class="{ 'banner-block': !needsReload }" role="alert" data-testid="decision-error">
      <p class="banner-text">{{ error }}</p>
      <button v-if="needsReload" type="button" class="btn" @click="emit('reload')">Reload the case</button>
    </div>
  </form>
</template>

<style scoped>
.decision {
  display: grid;
  gap: var(--space-24);
}
.decision-choices {
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-12);
}
.choice {
  display: grid;
  align-content: start;
  justify-items: start;
  gap: var(--space-4);
  min-height: var(--target-decision);
  padding: var(--space-12) var(--space-16);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--ground);
  color: var(--ink);
  text-align: left;
  font: inherit;
  cursor: pointer;
  touch-action: manipulation;
  transition:
    background-color var(--dur-base) var(--ease-out),
    border-color var(--dur-base) var(--ease-out),
    box-shadow var(--dur-base) var(--ease-out),
    transform var(--dur-fast) var(--ease-out);
}
.choice:active {
  transform: scale(0.98);
}
@media (prefers-reduced-motion: reduce) {
  .choice:active {
    transform: none;
  }
}
.choice-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8);
}
.choice-word {
  font: 600 17px/1.2 var(--font-display);
}
.choice-tag {
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: var(--tint);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}
.choice-hint {
  color: var(--muted);
  font-size: 14px;
  line-height: 1.4;
}
.choice-approve .choice-word {
  color: var(--approve);
}
.choice-clarify .choice-word {
  color: var(--clarify);
}
.choice-block .choice-word {
  color: var(--block);
}
.choice-approve.selected {
  background: var(--approve-bg);
  border-color: var(--approve);
  box-shadow: inset 0 0 0 1px var(--approve);
}
.choice-clarify.selected {
  background: var(--clarify-bg);
  border-color: var(--clarify);
  box-shadow: inset 0 0 0 1px var(--clarify);
}
.choice-block.selected {
  background: var(--block-bg);
  border-color: var(--block);
  box-shadow: inset 0 0 0 1px var(--block);
}
.choice.selected .choice-tag {
  background: var(--ground);
}
@media (hover: hover) and (pointer: fine) {
  .choice:not(.selected):hover {
    border-color: var(--muted);
  }
}
.note {
  resize: vertical;
  min-height: 96px;
}
.decision-submit {
  min-height: var(--target-decision);
  padding: 0 var(--space-24);
}
@media (max-width: 560px) {
  .decision-choices {
    grid-template-columns: 1fr;
  }
  .decision-submit {
    width: 100%;
  }
}
</style>
