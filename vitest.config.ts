import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const pkg = (name: string) => fileURLToPath(new URL(`./packages/${name}/src/index.ts`, import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      "@noquery/core": pkg("core"),
      "@noquery/postgres": pkg("postgres"),
      "@noquery/drizzle": pkg("drizzle"),
      "@noquery/anthropic": pkg("anthropic"),
      "@noquery/server": pkg("server"),
    },
  },
  test: {
    include: ["packages/*/test/**/*.test.ts"],
  },
});
