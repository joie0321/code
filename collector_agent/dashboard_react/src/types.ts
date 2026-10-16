export interface CollectorSource {
  collector_id: string;
  display_name: string;
  source_type: string;
  vcenter_configured?: boolean;
  azure_configured?: boolean;
  aws_configured?: boolean;
  aws_flow_logs_configured?: boolean;
  aws_flow_log_status?: string;
  aws_flow_log_detail?: string;
  aws_flow_log_objects_processed?: number;
  aws_flow_log_observations_uploaded?: number;
  aws_flow_log_last_sync_at?: string | null;
  azure_flow_logs_configured?: boolean;
  azure_flow_log_status?: string;
  azure_flow_log_detail?: string;
  azure_flow_log_objects_processed?: number;
  azure_flow_log_observations_uploaded?: number;
  azure_flow_log_last_sync_at?: string | null;
  guest_credentials_configured?: boolean;
  last_inventory_vm_count?: number;
  last_inventory_sync_at?: string | null;
  last_traffic_sync_at?: string | null;
  last_heartbeat_at?: string | null;
  ipfix_exporter_count?: number;
  ipfix_status?: string;
  ipfix_detail?: string;
  last_error?: string | null;
}

export interface RejectedIpfixDatagram {
  exporter_ip: string;
  datagrams_rejected: number;
  last_rejected_at?: string | null;
  reason?: string;
}

export interface IpfixMatchingDiagnostics {
  powered_on_inventory_ipv4_addresses: number;
  unmatched_flow_records: number;
  last_inventory_matched_flow_at?: string | null;
  unmatched_flow_samples: Array<{
    source_ip: string;
    destination_ip: string;
    unmatched_flow_count: number;
    last_seen_at?: string | null;
  }>;
}

export interface CollectorStatus {
  sources?: CollectorSource[];
  unassigned_ipfix_datagrams?: RejectedIpfixDatagram[];
}
