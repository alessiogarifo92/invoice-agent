<script setup lang="ts">
import { ref } from "vue";

const apiUrl = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const status = ref<string>("not checked");

async function check(): Promise<void> {
  status.value = "checking…";
  try {
    const response = await fetch(`${apiUrl}/healthz`);
    status.value = response.ok ? "API reachable" : `API error ${response.status}`;
  } catch {
    status.value = "API unreachable (it may be waking up)";
  }
}
</script>

<template>
  <main>
    <h1>invoice-agent</h1>
    <p>Placeholder. The real UI lands in T10.</p>
    <button type="button" @click="check">Check API</button>
    <p data-testid="status">{{ status }}</p>
  </main>
</template>
