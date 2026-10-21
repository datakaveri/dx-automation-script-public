import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { initializeKeycloak } from "./keycloak";

initializeKeycloak()
  .catch((error: unknown) => console.error("Keycloak initialization failed", error))
  .finally(() => {
    createRoot(document.getElementById("root")!).render(
      <StrictMode>
        <App />
      </StrictMode>
    );
  });
