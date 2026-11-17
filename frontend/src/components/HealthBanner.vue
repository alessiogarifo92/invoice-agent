<script setup lang="ts">
import type { HealthState } from "../composables/useHealth";

defineProps<{ state: HealthState }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div class="health" role="status" aria-live="polite">
    <div v-if="state === 'waking'" class="banner health-banner" data-testid="waking-banner">
      <p class="banner-text">
        <strong>The server is waking up.</strong> The free host sleeps when nobody uses it and
        takes about 30 seconds to start. Everything on this page stays readable meanwhile.
      </p>
      <button type="button" class="btn" @click="$emit('retry')">Retry now</button>
    </div>
  </div>
</template>

<style scoped>
.health-banner {
  max-width: var(--inbox-width);
  margin: var(--space-16) auto 0;
}
.health:has(.health-banner) {
  padding: 0 var(--space-24);
}
@media (max-width: 560px) {
  .health:has(.health-banner) {
    padding: 0 var(--space-16);
  }
}
</style>
