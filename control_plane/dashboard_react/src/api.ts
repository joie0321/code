export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers }, ...init });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = value && typeof value === "object" && "detail" in value ? (value as { detail?: unknown }).detail : null;
    throw new ApiError(typeof detail === "string" ? detail : "Control-plane dashboard request failed.");
  }
  return value as T;
}
export const api = {
  collectors: () => request<any[]>("/api/v1/collectors"),
  inventory: (id: string, historical = false, asOf?: string) => request<any[]>(`/api/v1/collectors/${encodeURIComponent(id)}/inventory?historical=${historical}${asOf ? `&historical_as_of=${encodeURIComponent(asOf)}` : ""}`),
  connections: (id: string, args: URLSearchParams) => request<any[]>(`/api/v1/collectors/${encodeURIComponent(id)}/connections?${args}`),
  waves: (id: string, args: URLSearchParams) => request<any[]>(`/api/v1/collectors/${encodeURIComponent(id)}/waves?${args}`),
  summary: (id: string, args: URLSearchParams) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/wave-summary?${args}`),
  currentEnvironment: (id: string, args: URLSearchParams) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/current-environment?${args}`),
  waveConnections: (id: string, args: URLSearchParams) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/wave-connections?${args}`),
  waveGroupingPortAllowlist: (id: string) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/wave-grouping-port-allowlist`),
  updateWaveGroupingPortAllowlist: (id: string, ports: { protocol: "tcp" | "udp"; port: number }[]) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/wave-grouping-port-allowlist`, { method: "PUT", body: JSON.stringify({ ports }) }),
  enrollment: (tenant_id: string, expires_in_minutes: number) => request<any>("/api/v1/enrollments", { method: "POST", body: JSON.stringify({ tenant_id, expires_in_minutes }) }),
  reconnect: (id: string, expires_in_minutes: number) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}/reconnection-codes`, { method: "POST", body: JSON.stringify({ expires_in_minutes }) }),
  remove: (id: string) => request<any>(`/api/v1/collectors/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
