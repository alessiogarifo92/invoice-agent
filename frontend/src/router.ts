import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

import InboxView from "./views/InboxView.vue";

export const routes: RouteRecordRaw[] = [
  { path: "/", name: "inbox", component: InboxView, meta: { title: "Inbox" } },
  {
    path: "/invoices/:id",
    name: "review",
    component: () => import("./views/ReviewView.vue"),
    props: true,
    meta: { title: "Case" },
  },
  {
    path: "/upload",
    name: "upload",
    component: () => import("./views/UploadView.vue"),
    meta: { title: "Submit an invoice" },
  },
  { path: "/:pathMatch(.*)*", redirect: { name: "inbox" } },
];

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(import.meta.env.BASE_URL),
    routes,
    scrollBehavior: (_to, _from, saved) => saved ?? { top: 0 },
  });
  router.afterEach((to) => {
    const title = typeof to.meta.title === "string" ? to.meta.title : null;
    document.title = title ? `${title} · invoice-agent` : "invoice-agent";
  });
  return router;
}
