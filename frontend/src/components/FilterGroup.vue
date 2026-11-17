<script setup lang="ts" generic="T extends string">
// A group of radio buttons styled as pills: native radios, so arrow keys and screen readers
// work without any extra code.
defineProps<{
  legend: string;
  name: string;
  options: Array<{ value: T; label: string; count?: number }>;
}>();
const model = defineModel<T>({ required: true });
</script>

<template>
  <fieldset class="filter">
    <legend class="filter-legend">{{ legend }}</legend>
    <div class="filter-options">
      <label
        v-for="option in options"
        :key="option.value"
        class="filter-option"
        :class="{ selected: model === option.value }"
      >
        <input v-model="model" class="visually-hidden" type="radio" :name="name" :value="option.value" />
        <span>{{ option.label }}</span>
        <span v-if="option.count !== undefined" class="filter-count mono" aria-hidden="true">{{ option.count }}</span>
        <span v-if="option.count !== undefined" class="visually-hidden">({{ option.count }})</span>
      </label>
    </div>
  </fieldset>
</template>

<style scoped>
.filter {
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}
.filter-legend {
  padding: 0;
  margin-bottom: var(--space-8);
  font-size: 14px;
  font-weight: 600;
  color: var(--muted);
}
.filter-options {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8);
}
.filter-option {
  display: inline-flex;
  align-items: center;
  gap: var(--space-8);
  min-height: var(--target);
  padding: 0 var(--space-16);
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: var(--ground);
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}
.filter-option.selected {
  background: var(--ink);
  border-color: var(--ink);
  color: var(--ground);
}
.filter-option:has(:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (hover: hover) and (pointer: fine) {
  .filter-option:not(.selected):hover {
    border-color: var(--muted);
  }
}
.filter-count {
  font-size: 12px;
  opacity: 0.75;
}
</style>
