import type { CollectorStatus, IpfixMatchingDiagnostics } from "../types";

const STATUS_ENDPOINT = "/api/v1/status";

export type SourceType = "vmware" | "azure" | "aws";

export interface RegistrationPayload {
  control_plane_url: string;
  tenant_id: string;
  enrollment_code: string;
  display_name: string;
  source_type: SourceType;
}

export interface ReconnectionPayload {
  control_plane_url: string;
  collector_id: string;
  reconnection_code: string;
  display_name?: string;
  source_type: SourceType;
}

export interface SetupResult {
  collector_id: string;
  status: "registered" | "reconnected" | "unregistered";
}

export class CollectorApiError extends Error {}

async function requestJson<T>(path: string, method: "POST" | "DELETE", payload?: object): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: payload ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
    credentials: "same-origin",
  });
  const responsePayload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof responsePayload === "object" && responsePayload !== null && "detail" in responsePayload
      ? (responsePayload as { detail?: unknown }).detail
      : undefined;
    throw new CollectorApiError(
      typeof detail === "string" && detail.length <= 512 ? detail : "Collector agent request was rejected.",
    );
  }
  if (typeof responsePayload !== "object" || responsePayload === null || Array.isArray(responsePayload)) {
    throw new CollectorApiError("Collector agent returned an invalid response.");
  }
  return responsePayload as T;
}

export async function getCollectorStatus(signal?: AbortSignal): Promise<CollectorStatus> {
  const response = await fetch(STATUS_ENDPOINT, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
    credentials: "same-origin",
  });

  if (!response.ok) {
    throw new Error("Cannot contact the local collector agent.");
  }
  const payload: unknown = await response.json();
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new Error("Collector agent returned an invalid status response.");
  }
  return payload as CollectorStatus;
}

export function registerSource(payload: RegistrationPayload): Promise<SetupResult> {
  return requestJson<SetupResult>("/api/v1/setup/register", "POST", payload);
}

export function reconnectSource(payload: ReconnectionPayload): Promise<SetupResult> {
  return requestJson<SetupResult>("/api/v1/setup/reconnect", "POST", payload);
}

export function unregisterSource(collectorId: string): Promise<SetupResult> {
  return requestJson<SetupResult>(
    `/api/v1/setup/sources/${encodeURIComponent(collectorId)}`,
    "DELETE",
  );
}

export interface InventorySyncResult { discovered: number; accepted: number; sequence: number }
export interface FlowSyncResult { objects_processed: number; observations_uploaded: number }
export interface IpfixCandidate { host_name: string; cluster: string | null; vmk0_ips: string[] }
export interface IpfixSetup {
  exporter_candidates: IpfixCandidate[];
  selected_exporters: string[];
  listener: { status: string; datagrams_received: number; flows_received: number; observations_stored: number; last_received_at?: string | null };
  last_error?: string | null;
  matching?: IpfixMatchingDiagnostics;
}

function collectorAction<T>(path: string, collectorId: string): Promise<T> {
  return requestJson<T>(`${path}?collector_id=${encodeURIComponent(collectorId)}`, "POST");
}

export function configureVmware(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/setup/credentials", "POST", payload); }
export function configureAzure(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/setup/azure/credentials", "POST", payload); }
export function configureAzureFlowLogs(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/setup/azure/flow-logs", "POST", payload); }
export function configureAws(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/setup/aws/credentials", "POST", payload); }
export function configureAwsFlowLogs(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/setup/aws/flow-logs", "POST", payload); }
export function syncInventory(collectorId: string): Promise<InventorySyncResult> { return collectorAction("/api/v1/collection/inventory/sync", collectorId); }
export function syncAwsFlowLogs(collectorId: string): Promise<FlowSyncResult> { return collectorAction("/api/v1/aws/flow-logs/sync", collectorId); }
export function syncAzureFlowLogs(collectorId: string): Promise<FlowSyncResult> { return collectorAction("/api/v1/azure/flow-logs/sync", collectorId); }
export function stopAwsFlowLogs(collectorId: string): Promise<{ status: string }> { return collectorAction("/api/v1/aws/flow-logs/stop", collectorId); }
export function startAwsFlowLogs(collectorId: string): Promise<{ status: string }> { return collectorAction("/api/v1/aws/flow-logs/start", collectorId); }
export function stopAzureFlowLogs(collectorId: string): Promise<{ status: string }> { return collectorAction("/api/v1/azure/flow-logs/stop", collectorId); }
export function startAzureFlowLogs(collectorId: string): Promise<{ status: string }> { return collectorAction("/api/v1/azure/flow-logs/start", collectorId); }
export function stopIpfix(collectorId: string): Promise<{ status: string }> { return collectorAction("/api/v1/ipfix/stop", collectorId); }
export function startIpfix(payload: object): Promise<{ status: string }> { return requestJson("/api/v1/ipfix/start", "POST", payload); }

export async function getIpfixSetup(collectorId: string): Promise<IpfixSetup> {
  const response = await fetch(`/api/v1/ipfix/setup?collector_id=${encodeURIComponent(collectorId)}`, { credentials: "same-origin" });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null || Array.isArray(body)) {
    const detail = typeof body === "object" && body !== null && "detail" in body ? (body as { detail?: unknown }).detail : null;
    throw new CollectorApiError(typeof detail === "string" ? detail : "Unable to load IPFIX setup.");
  }
  return body as IpfixSetup;
}
