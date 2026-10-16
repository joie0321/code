# FlowWave OCI Container Instance deployment

## Intended development topology

Create two OCI Container Instances in the existing `OCVS_SYD` VCN and
`oci-migration` subnet:

1. `flowwave-control-dev`: two containers using the Control image.
   - `control-api`: default image command, listening only on TCP 8100.
   - `control-ui`: override the command with
     `uvicorn control_plane.dashboard_react:app --host 0.0.0.0 --port 8503`.
   - The UI calls the API using `http://127.0.0.1:8100`. OCI Container Instance
     containers share a network namespace; this loopback-only request never
     leaves the instance. The API must not be exposed through the subnet.

2. `flowwave-collector-dev`: two containers using the Collector image.
   - `collector-agent`: default image command, bound to loopback TCP 8443 and
     accepting IPFIX on UDP 4739 once configured in the Collector UI.
   - `collector-ui`: override the command with
     `uvicorn collector_agent.dashboard_react:app --host 0.0.0.0 --port 8502`.
   - Store its local sequence state at
     `/var/lib/flowwave/collector_sequence_state.json`.

Keep both UI ports private for this first deployment. Access them from the
approved private network or through a separately approved TLS-terminating load
balancer. Do not publish the Control API or Collector API ports.

## Required environment values

Set these only as Container Instance environment variables. Do not add a `.env`
file to this repository or image.

### `control-api`

```text
APP_ENV=production
DATABASE_MODE=oci_vault
DATABASE_URL=postgresql+psycopg://ocimigration_app@primary.objllmruws46hisqphga3x6xx3baqa.postgresql.ap-sydney-1.oci.oraclecloud.com:5432/migrationdiscovery_react?sslmode=verify-full&sslrootcert=/opt/flowwave/certs/postgresql-ca.pem
DATABASE_PASSWORD_SECRET_OCID=<existing database-password secret OCID>
CONTROL_PLANE_ADMIN_API_KEY_SECRET_OCID=<administrative API-key secret OCID>
CONTROL_PLANE_DASHBOARD_API_KEY_SECRET_OCID=<dashboard API-key secret OCID>
CONTROL_PLANE_DASHBOARD_API_URL=http://127.0.0.1:8100
CONTROL_PLANE_DASHBOARD_API_LOCAL_LOOPBACK=true
```

### `control-ui`

Use the same Control Plane API secret OCIDs as `control-api`. The dashboard proxy
retrieves the keys through its resource principal; the browser never receives them.

### `collector-agent` and `collector-ui`

```text
APP_ENV=production
AGENT_SEQUENCE_STATE_FILE=/var/lib/flowwave/collector_sequence_state.json
```

The Collector's vCenter, Azure, and AWS setup credentials are intentionally
memory-only. A collector restart requires an administrator to re-enter them
through the Collector UI. The local sequence-state file is not durable across
Container Instance replacement or image redeployment; this is an accepted
trade-off for the one-time discovery deployment. Do not store credentials in
that directory.

## OCI DevOps pipeline

1. Push this directory to the OCI DevOps Code Repository.
2. Create two private OCIR repositories: `flowwave-control` and
   `flowwave-collector`.
3. Create an OCI DevOps build pipeline with `build_spec.yaml` as its build
   specification.
4. Add two Docker-image artifacts, mapping `flowwave-control-image` and
   `flowwave-collector-image` to their respective OCIR repositories.
5. Use the resulting image digests in the deployment stage. Never deploy
   mutable `latest` tags.
6. Use a DevOps Shell stage to update or recreate the two Container Instances
   with the produced digest, then verify `/healthz` on each UI and API container.

## OCI prerequisites before the first create

- An authenticated OCI CLI profile or DevOps connection with rights scoped to
  the intended compartment.
- Subnet OCID and availability domain for `oci-migration`.
- A dynamic group scoped to the two Container Instances and a policy allowing
  it to read only the existing database password, administrative API-key, and
  dashboard API-key secrets.
- OCIR pull access through the Container Instance resource principal.
- Network rules: UI TCP ports only from approved private CIDRs; database TCP
  5432 only from the Control instance; vCenter TCP 443 only from the Collector;
  UDP 4739 only from the approved IPFIX exporter IPs to the Collector.

The build source contains no database password, API keys, Auth Token, private
key, flow data, or collector state file.
