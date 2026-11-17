<script setup lang="ts">
// Submitting an invoice needs the demo token (T9). The size and type checks here only save a
// round trip: the server checks again and stays the authority.
import { computed, nextTick, ref, useId } from "vue";
import { useRouter } from "vue-router";

import { api } from "../api";
import { ApiError, isUnreachable, userMessage } from "../api/client";
import TokenField from "../components/TokenField.vue";
import { useDemoToken } from "../composables/useDemoToken";
import { useServerHealth } from "../composables/useServer";

const MAX_BYTES = 2_000_000;

const router = useRouter();
const store = useDemoToken();
const health = useServerHealth();
const inputId = useId();

const file = ref<File | null>(null);
const fileError = ref<string | null>(null);
const error = ref<string | null>(null);
const sending = ref(false);
const tokenField = ref<InstanceType<typeof TokenField> | null>(null);

const sizeLabel = computed(() => (file.value ? `${Math.max(1, Math.round(file.value.size / 1000))} kB` : ""));

function onPick(event: Event): void {
  const picked = (event.target as HTMLInputElement).files?.[0] ?? null;
  error.value = null;
  fileError.value = null;
  file.value = null;
  if (!picked) return;
  if (picked.type !== "application/pdf" && !picked.name.toLowerCase().endsWith(".pdf")) {
    fileError.value = "Only PDF files can be uploaded.";
  } else if (picked.size > MAX_BYTES) {
    fileError.value = "The file is larger than 2 MB.";
  } else {
    file.value = picked;
  }
}

async function submit(): Promise<void> {
  error.value = null;
  if (!store.token.value) {
    error.value = "Enter the demo token first.";
    tokenField.value?.focus();
    return;
  }
  if (!file.value) {
    fileError.value = fileError.value ?? "Choose a PDF to upload.";
    return;
  }
  sending.value = true;
  try {
    const { invoice, created } = await api.upload(file.value, store.token.value);
    await router.push({
      name: "review",
      params: { id: invoice.id },
      query: { uploaded: created ? "new" : "existing" },
    });
  } catch (err) {
    if (err instanceof ApiError && err.kind === "unauthorized") {
      // The token field says so, next to the field to fix.
      store.reject();
      await nextTick();
      tokenField.value?.focus();
      return;
    }
    if (isUnreachable(err)) health.reportUnreachable();
    error.value = userMessage(err);
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <div class="upload">
    <header class="upload-head">
      <h1>Submit an invoice</h1>
      <p class="muted">
        Upload a synthetic supplier invoice and watch it become a case. Never upload a real invoice
        or personal data: this is a public demo.
      </p>
    </header>

    <section class="panel" aria-labelledby="token-title">
      <h2 id="token-title">1 · Access</h2>
      <TokenField ref="tokenField" purpose="Uploads need the demo token the project owner handed you." />
    </section>

    <form class="panel" aria-labelledby="file-title" data-testid="upload-form" @submit.prevent="submit">
      <h2 id="file-title">2 · The PDF</h2>
      <div class="field">
        <label class="field-label" :for="inputId">Invoice PDF</label>
        <p :id="`${inputId}-hint`" class="field-hint">
          PDF with selectable text, at most 2 MB and 5 pages.
        </p>
        <input
          :id="inputId"
          class="file-input"
          type="file"
          accept="application/pdf,.pdf"
          :aria-describedby="`${inputId}-hint${fileError ? ` ${inputId}-error` : ''}`"
          :aria-invalid="fileError ? 'true' : undefined"
          data-testid="upload-file"
          @change="onPick"
        />
        <p v-if="file" class="small muted">
          <span class="mono">{{ file.name }}</span> · {{ sizeLabel }}
        </p>
        <p v-if="fileError" :id="`${inputId}-error`" class="field-error" role="alert">{{ fileError }}</p>
      </div>
      <p class="small muted">
        If today's demo budget is used up, the invoice waits until 00:00 UTC before it is read;
        existing cases stay reviewable.
      </p>
      <div>
        <button type="submit" class="btn btn-primary submit" :disabled="sending" data-testid="upload-submit">
          {{ sending ? "Uploading…" : "Upload and open the case" }}
        </button>
      </div>
      <div v-if="error" class="banner banner-block" role="alert" data-testid="upload-error">
        <p class="banner-text">{{ error }}</p>
      </div>
    </form>
  </div>
</template>

<style scoped>
.upload {
  max-width: var(--measure);
  margin: 0 auto;
  display: grid;
  gap: var(--space-32);
}
.upload-head {
  display: grid;
  gap: var(--space-8);
}
.panel {
  display: grid;
  gap: var(--space-16);
  padding: var(--space-24);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
}
.panel h2 {
  font-size: 20px;
}
.file-input {
  min-height: var(--target);
  font: inherit;
  font-size: 16px;
  max-width: 100%;
}
.file-input::file-selector-button {
  min-height: var(--target);
  margin-right: var(--space-12);
  padding: 0 var(--space-16);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--ground);
  color: var(--ink);
  font: 500 15px/1 var(--font-body);
  cursor: pointer;
}
.submit {
  min-height: var(--target-decision);
  padding: 0 var(--space-24);
}
@media (max-width: 560px) {
  h1 {
    font-size: 32px;
  }
  .panel {
    padding: var(--space-16);
  }
  .submit {
    width: 100%;
  }
}
</style>
