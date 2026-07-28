import { execFile } from "node:child_process";
import { resolve } from "node:path";
import { promisify } from "node:util";
import type { Plugin } from "vite";

const execFileAsync = promisify(execFile);

export function podcastLiveData(): Plugin {
  let refreshInFlight: Promise<void> | null = null;

  return {
    name: "kedaibiao-podcast-live-data",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(
        "/__podcast-dashboard/refresh",
        async (request, response) => {
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");

          if (request.method !== "POST") {
            response.statusCode = 405;
            response.end(JSON.stringify({ ok: false, error: "POST required" }));
            return;
          }

          const root = server.config.root;
          const python = resolve(root, "../.venv-podcast/bin/python");
          const script = resolve(root, "scripts/generate-dashboard-data.py");

          refreshInFlight ??= execFileAsync(python, [script], {
            cwd: root,
            timeout: 120_000,
            maxBuffer: 1024 * 1024,
          })
            .then(() => undefined)
            .finally(() => {
              refreshInFlight = null;
            });

          try {
            await refreshInFlight;
            response.statusCode = 200;
            response.end(JSON.stringify({ ok: true }));
          } catch (error) {
            server.config.logger.error(
              `Podcast dashboard refresh failed: ${
                error instanceof Error ? error.message : String(error)
              }`,
            );
            response.statusCode = 500;
            response.end(
              JSON.stringify({
                ok: false,
                error: "读取实时状态失败，请稍后重试。",
              }),
            );
          }
        },
      );
    },
  };
}
