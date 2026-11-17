import { createApi } from "./client";

// Build-time API origin (the Render URL in production); the compose stack's API otherwise.
export const api = createApi(import.meta.env.VITE_API_URL ?? "http://localhost:8000");
