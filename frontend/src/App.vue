<script setup lang="ts">
import { onMounted } from "vue";
import { RouterLink, RouterView } from "vue-router";

import { api } from "./api";
import HealthBanner from "./components/HealthBanner.vue";
import { useHealth } from "./composables/useHealth";
import { provideServerHealth } from "./composables/useServer";

const health = useHealth(() => api.health());
provideServerHealth(health);
onMounted(() => health.start());
</script>

<template>
  <a class="skip" href="#main">Skip to content</a>
  <header class="site-header">
    <div class="site-header-inner">
      <RouterLink to="/" class="wordmark">invoice-agent</RouterLink>
      <span class="synthetic">Synthetic data</span>
      <nav class="site-nav" aria-label="Main">
        <RouterLink to="/" class="nav-link">Inbox</RouterLink>
        <RouterLink to="/upload" class="nav-link">Submit an invoice</RouterLink>
      </nav>
    </div>
  </header>
  <HealthBanner :state="health.state.value" @retry="health.retry()" />
  <main id="main" tabindex="-1">
    <RouterView />
  </main>
  <footer class="site-footer">
    <p>
      Every invoice, vendor and bank account here is synthetic. Approving a case only marks it
      approved for payment; nothing is ever paid.
    </p>
  </footer>
</template>

<style scoped>
.skip {
  position: absolute;
  left: var(--space-16);
  top: -100px;
  z-index: 10;
  padding: var(--space-12) var(--space-16);
  background: var(--ink);
  color: var(--ground);
  border-radius: var(--radius);
}
.skip:focus {
  top: var(--space-8);
}
.site-header {
  border-bottom: 1px solid var(--rule);
  padding-top: env(safe-area-inset-top);
}
.site-header-inner {
  max-width: var(--inbox-width);
  margin: 0 auto;
  padding: var(--space-8) var(--space-24);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-4) var(--space-12);
}
.wordmark {
  display: inline-flex;
  align-items: center;
  min-height: var(--target);
  font: 600 19px/1 var(--font-display);
  letter-spacing: -0.02em;
  color: var(--ink);
  text-decoration: none;
}
.synthetic {
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  background: var(--tint);
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}
.site-nav {
  display: flex;
  gap: var(--space-4);
  margin-left: auto;
}
.nav-link {
  display: inline-flex;
  align-items: center;
  min-height: var(--target);
  padding: 0 var(--space-12);
  border-radius: var(--radius);
  color: var(--muted);
  font-weight: 500;
  font-size: 15px;
  text-decoration: none;
  transition: color var(--dur-fast) var(--ease-out), background-color var(--dur-fast) var(--ease-out);
}
.nav-link.router-link-exact-active {
  color: var(--ink);
  background: var(--tint);
}
@media (hover: hover) and (pointer: fine) {
  .nav-link:hover {
    color: var(--ink);
  }
}
main {
  padding: var(--space-32) var(--space-24) var(--space-72);
  outline: none;
}
.site-footer {
  border-top: 1px solid var(--rule);
  padding: var(--space-24) var(--space-24) calc(var(--space-24) + env(safe-area-inset-bottom));
}
.site-footer p {
  max-width: var(--inbox-width);
  margin: 0 auto;
  color: var(--muted);
  font-size: 14px;
}
@media (max-width: 560px) {
  .site-header-inner {
    padding: var(--space-4) var(--space-16);
  }
  .site-nav {
    margin-left: -12px;
    width: 100%;
  }
  main {
    padding: var(--space-24) var(--space-16) var(--space-48);
  }
  .site-footer {
    padding-left: var(--space-16);
    padding-right: var(--space-16);
  }
}
</style>
