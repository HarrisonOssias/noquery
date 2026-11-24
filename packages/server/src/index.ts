import { timingSafeEqual } from "node:crypto";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { createServer } from "node:http";
import type { AuthContext, Engine, PlannerTurn, QueryIR } from "@noquery/core";
import { PlannerError, QueryValidationError } from "@noquery/core";

/** Throw from `authenticate` (or anywhere in a handler) to answer with a specific status. */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

/**
 * Decides who is asking. Return the auth context the engine should enforce, or throw HttpError(401).
 * `body` is the parsed JSON request body.
 */
export type Authenticate = (request: IncomingMessage, body: Record<string, unknown>) => AuthContext | Promise<AuthContext>;

export interface ServerOptions {
  engine: Engine;
  authenticate: Authenticate;
  /** Largest accepted request body. Default 1 MB. */
  maxBodyBytes?: number;
  /** Called with unexpected errors. Responses never include their details. */
  onError?: (error: unknown) => void;
}

/**
 * Sidecar authentication: the calling service proves itself with a shared bearer token and
 * passes the end user's auth context in the request body as `auth`.
 * Only use this when the service is reachable by your own backend alone, never by end users.
 */
export function serviceTokenAuth(token: string): Authenticate {
  if (!token || token.length < 16) {
    throw new Error("The service token must be at least 16 characters.");
  }
  const expected = Buffer.from(token);
  return (request, body) => {
    const header = request.headers.authorization ?? "";
    const given = Buffer.from(header.startsWith("Bearer ") ? header.slice(7) : "");
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
      throw new HttpError(401, "unauthorized", "Missing or invalid service token.");
    }
    return parseAuth(body.auth);
  };
}

function parseAuth(value: unknown): AuthContext {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(400, "bad_request", "`auth` must be an object, e.g. {\"userId\": \"u1\", \"orgId\": \"...\", \"roles\": [\"analyst\"]}.");
  }
  const scalar = (v: unknown) => typeof v === "string" || typeof v === "number" || typeof v === "boolean";
  for (const [key, v] of Object.entries(value)) {
    const ok = v === null || scalar(v) || (Array.isArray(v) && v.every(scalar));
    if (!ok) throw new HttpError(400, "bad_request", `auth.${key} must be a string, number, boolean or a list of those.`);
  }
  const auth = value as AuthContext;
  if (auth.roles !== undefined && !(Array.isArray(auth.roles) && auth.roles.every((r) => typeof r === "string"))) {
    throw new HttpError(400, "bad_request", "auth.roles must be a list of strings.");
  }
  return auth;
}

type Handler = (body: Record<string, unknown>, auth: AuthContext) => Promise<unknown> | unknown;

/** A request listener you can mount on any Node HTTP server. */
export function createRequestHandler(options: ServerOptions) {
  const { engine } = options;
  const maxBody = options.maxBodyBytes ?? 1024 * 1024;

  const routes: Record<string, Handler> = {
    "/v1/catalog": (_body, auth) => engine.catalog(auth),
    "/v1/validate": (body, auth) => engine.validate(requireIr(body), auth),
    "/v1/run": (body, auth) => engine.run(requireIr(body), { auth }),
    "/v1/ask": (body, auth) => {
      if (!engine.hasPlanner) {
        throw new HttpError(501, "planner_not_configured", "This server has no planner, so /v1/ask is unavailable. Use /v1/run.");
      }
      if (typeof body.question !== "string" || body.question.trim() === "") {
        throw new HttpError(400, "bad_request", "`question` must be a non-empty string.");
      }
      const opts: { auth: AuthContext; timezone?: string; history?: PlannerTurn[] } = { auth };
      if (typeof body.timezone === "string") opts.timezone = body.timezone;
      if (Array.isArray(body.history)) opts.history = body.history as PlannerTurn[];
      return engine.ask(body.question, opts);
    },
  };

  return async (request: IncomingMessage, response: ServerResponse) => {
    try {
      const path = new URL(request.url ?? "/", "http://localhost").pathname;
      if (path === "/healthz") {
        return send(response, 200, { ok: true, planner: engine.hasPlanner });
      }
      const route = routes[path];
      if (!route) throw new HttpError(404, "not_found", `No route ${path}.`);
      if (request.method !== "POST") throw new HttpError(405, "method_not_allowed", "Use POST.");
      const body = await readJson(request, maxBody);
      const auth = await options.authenticate(request, body);
      const result = await route(body, auth);
      send(response, 200, result);
    } catch (error) {
      if (error instanceof HttpError) {
        return send(response, error.status, { error: { code: error.code, message: error.message } });
      }
      if (error instanceof QueryValidationError) {
        return send(response, 422, { error: { code: "invalid_query", message: error.message, errors: error.errors } });
      }
      if (error instanceof PlannerError) {
        options.onError?.(error);
        return send(response, error.retryable ? 503 : 502, {
          error: { code: "planner_failed", message: error.message, reason: error.reason, retryable: error.retryable },
        });
      }
      if (error instanceof Error && error.name === "PlannerRefusalError") {
        return send(response, 422, { error: { code: "planner_refused", message: error.message } });
      }
      options.onError?.(error);
      send(response, 500, { error: { code: "internal", message: "Internal error." } });
    }
  };
}

/** A ready-to-listen HTTP server: `createNoqueryServer({...}).listen(8417, "127.0.0.1")`. */
export function createNoqueryServer(options: ServerOptions): Server {
  return createServer(createRequestHandler(options));
}

function requireIr(body: Record<string, unknown>): QueryIR {
  const ir = body.ir;
  if (!ir || typeof ir !== "object" || Array.isArray(ir)) {
    throw new HttpError(400, "bad_request", "`ir` must be a query object with version: 1.");
  }
  return ir as QueryIR;
}

async function readJson(request: IncomingMessage, maxBytes: number): Promise<Record<string, unknown>> {
  const type = request.headers["content-type"] ?? "";
  if (!type.includes("application/json")) {
    throw new HttpError(415, "unsupported_media_type", "Send Content-Type: application/json.");
  }
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += (chunk as Buffer).length;
    if (size > maxBytes) throw new HttpError(413, "payload_too_large", `Request body exceeds ${maxBytes} bytes.`);
    chunks.push(chunk as Buffer);
  }
  try {
    const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
    return parsed as Record<string, unknown>;
  } catch {
    throw new HttpError(400, "bad_request", "Request body must be a JSON object.");
  }
}

function send(response: ServerResponse, status: number, payload: unknown) {
  const body = JSON.stringify(payload, (_key, value: unknown) => (typeof value === "bigint" ? Number(value) : value));
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(body) });
  response.end(body);
}
