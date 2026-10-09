import { render } from "preact";
import { App } from "./app/App";
import { createWorkspace } from "./state/workspace";
import { createApiClient } from "./api/client";
import { Endpoints } from "./api/endpoints";
import { parseSchema } from "./schema/parse";
import "./styles/index.css";

const client = createApiClient();
const endpoints = new Endpoints(client);

const workspace = createWorkspace({
  endpoints: {
    surfaceSchema: (options) => endpoints.surfaceSchema(options),
    surfaceBaseline: (options) => endpoints.surfaceBaseline(options),
    createBaseline: (body, options) => endpoints.createBaseline(body, options),
    submitPreview: (payload, options) => endpoints.submitPreview(payload, options),
    job: (jobId, options) => endpoints.job(jobId, options),
    blenderStatus: () => endpoints.blenderStatus(),
    framingContext: () => endpoints.framingContext(),
    parseSchema,
  },
});

const mount = document.getElementById("app");
if (mount) {
  render(<App workspace={workspace} />, mount);
}

export {};
