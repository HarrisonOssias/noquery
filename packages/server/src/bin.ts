#!/usr/bin/env node
/**
 * noquery-server [config]
 *
 * Loads a config module (default ./noquery.config.mjs, or $NOQUERY_CONFIG) whose default export is
 * either EngineOptions or { engine } / { engineOptions, authenticate }, then serves it over HTTP.
 *
 * Environment:
 *   NOQUERY_SERVICE_TOKEN  shared secret callers send as "Authorization: Bearer <token>" (required
 *                          unless the config exports its own `authenticate`)
 *   PORT                   default 8417
 *   HOST                   default 127.0.0.1 (localhost only)
 */
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { Engine, EngineOptions } from "@noquery/core";
import { createEngine } from "@noquery/core";
import type { Authenticate } from "./index.js";
import { createNoqueryServer, serviceTokenAuth } from "./index.js";

interface ConfigObject {
  engine?: Engine;
  engineOptions?: EngineOptions;
  authenticate?: Authenticate;
}

async function main() {
  const configPath = resolve(process.argv[2] ?? process.env.NOQUERY_CONFIG ?? "noquery.config.mjs");
  const loaded = (await import(pathToFileURL(configPath).href)) as { default?: unknown };
  const config = loaded.default as (ConfigObject & Partial<EngineOptions>) | undefined;
  if (!config || typeof config !== "object") {
    throw new Error(`${configPath} must have a default export.`);
  }

  let engine: Engine;
  if (config.engine) engine = config.engine;
  else if (config.engineOptions) engine = createEngine(config.engineOptions);
  else if (config.model && config.dialect && config.executor) engine = createEngine(config as EngineOptions);
  else throw new Error(`${configPath} must export engine options ({ model, dialect, executor, ... }) or { engine }.`);

  let authenticate = config.authenticate;
  if (!authenticate) {
    const token = process.env.NOQUERY_SERVICE_TOKEN;
    if (!token) throw new Error("Set NOQUERY_SERVICE_TOKEN (at least 16 characters), or export `authenticate` from the config.");
    authenticate = serviceTokenAuth(token);
  }

  const port = Number(process.env.PORT ?? 8417);
  const host = process.env.HOST ?? "127.0.0.1";
  const server = createNoqueryServer({
    engine,
    authenticate,
    onError: (error) => console.error("[noquery-server]", error),
  });
  server.on("error", (error: NodeJS.ErrnoException) => {
    if (error.code === "EADDRINUSE") {
      console.error(`Port ${port} on ${host} is already in use. Start with a different PORT, e.g. PORT=${port + 1}.`);
    } else {
      console.error(error.message);
    }
    process.exit(1);
  });
  server.listen(port, host, () => {
    console.log(`noquery-server listening on http://${host}:${port} (planner: ${engine.hasPlanner ? "on" : "off"})`);
  });
  const stop = () => server.close(() => process.exit(0));
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
