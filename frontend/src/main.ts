import { createApp } from "vue";

import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/base.css";
import App from "./App.vue";
import { createAppRouter } from "./router";

createApp(App).use(createAppRouter()).mount("#app");
