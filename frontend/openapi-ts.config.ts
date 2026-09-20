import { defineConfig } from "@hey-api/openapi-ts"

export default defineConfig({
  input: "./openapi.json",
  output: {
    path: "./src/generated",
    postProcess: [],
  },
  plugins: [
    {
      name: "@hey-api/typescript",
    },
    {
      name: "zod",
      compatibilityVersion: 4,
      definitions: true,
      requests: false,
      responses: true,
    },
  ],
})
