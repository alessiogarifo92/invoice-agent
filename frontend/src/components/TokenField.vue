<script setup lang="ts">
import { ref, useId } from "vue";

import { useDemoToken } from "../composables/useDemoToken";

defineProps<{ purpose: string }>();

const store = useDemoToken();
const draft = ref("");
const input = ref<HTMLInputElement | null>(null);
const id = useId();

function save(): void {
  store.save(draft.value);
  draft.value = "";
}

defineExpose({ focus: () => input.value?.focus() });
</script>

<template>
  <div class="token" data-testid="token-field">
    <div v-if="store.token.value" class="token-set">
      <p><strong>Demo token set</strong> for this tab.</p>
      <button type="button" class="btn btn-quiet" @click="store.forget()">Forget it</button>
    </div>
    <form v-else class="field" @submit.prevent="save">
      <label class="field-label" :for="id">Demo token</label>
      <p :id="`${id}-hint`" class="field-hint">
        {{ purpose }} It stays in this tab only and is sent as a bearer token.
      </p>
      <div class="token-row">
        <input
          :id="id"
          ref="input"
          v-model="draft"
          class="input mono-input"
          type="password"
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
          :aria-describedby="`${id}-hint${store.rejected.value ? ` ${id}-error` : ''}`"
          :aria-invalid="store.rejected.value ? 'true' : undefined"
        />
        <button type="submit" class="btn" :disabled="!draft.trim()">Use token</button>
      </div>
      <p v-if="store.rejected.value" :id="`${id}-error`" class="field-error" role="alert">
        The demo token was not accepted. Check it and enter it again.
      </p>
    </form>
  </div>
</template>

<style scoped>
.token-set {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8);
  min-height: var(--target);
}
.token-row {
  display: flex;
  gap: var(--space-8);
}
.token-row .btn {
  flex: none;
  white-space: nowrap;
}
.token-row .input {
  flex: 1 1 auto;
  min-width: 0;
}
.mono-input {
  font-family: var(--font-mono);
  font-size: 16px;
}
</style>
