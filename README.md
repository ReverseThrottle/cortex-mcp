# cortex-mcp

A community-built [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for the [Palo Alto Networks Cortex](https://docs-cortex.paloaltonetworks.com/) platform. It exposes Cortex XSIAM / XDR data — cases, issues, endpoints, assets, vulnerabilities, and XQL queries — as MCP tools that any MCP-compatible AI assistant (Claude, Cursor, etc.) can call directly.

> **License:** Palo Alto Networks Cortex Communication Python Files License 1.0 — see [LICENSE](LICENSE).

---

## Table of Contents

- [Features](#features)
- [Architecture overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Quickstart](#quickstart)
  - [Docker (recommended)](#option-1-docker-recommended)
  - [Local / Poetry](#option-2-local--poetry)
- [Configuration](#configuration)
- [Connecting to an AI client](#connecting-to-an-ai-client)
  - [Claude Desktop](#claude-desktop)
  - [Cursor / other MCP clients](#cursor--other-mcp-clients)
  - [HTTP transport (remote server)](#http-transport-remote-server)
- [Available tools](#available-tools)
- [CLI reference](#cli-reference)
- [Extending with custom tools](#extending-with-custom-tools)
  - [Python module](#python-module)
  - [OpenAPI spec](#openapi-spec)
- [Development](#development)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Features

| Category | Tools |
|---|---|
| **Cases / Incidents** | Search & filter cases, update status / severity / assignee, add comments |
| **Issues / Alerts** | Search & filter issues with full pagination |
| **Endpoints** | List & filter endpoints, isolate / unisolate from the network |
| **Assets** | Get asset inventory by ID or filtered list |
| **Vulnerabilities** | Paginated vulnerability search with CVSS, EPSS, CISA KEV filters |
| **XQL** | Execute any XQL query with automatic async polling and result return |
| **Assessment** | Pull assessment profile results |
| **Tenant** | Retrieve tenant information |
| **Remote components** | Pull Cortex-managed tools with the `update` CLI command |

---

## Architecture overview

```
AI Client (Claude / Cursor / …)
        │  MCP protocol (stdio or HTTP)
        ▼
  cortex-mcp server (FastMCP)
        │  HTTPS / REST
        ▼
  Cortex PAPI (your tenant)
```

The server acts as a thin bridge: it translates MCP tool calls into authenticated Cortex PAPI requests and returns the results as structured JSON. All authentication stays on the server — the AI client never sees your credentials.

**Tool sources (three layers):**

| Layer | Location | Who manages it |
|---|---|---|
| Built-in | `src/usecase/builtin_components/` | This repo |
| Custom | `src/usecase/custom_components/` | You |
| Remote | `src/usecase/remote_components/` | Cortex (via `update` command) |

---

## Prerequisites

- **Cortex API credentials** — a Standard API key and its numeric key ID from your Cortex tenant
- One of:
  - Docker (recommended for production / Claude Desktop)
  - Python ≥ 3.12 + [Poetry](https://python-poetry.org/) (recommended for development)

---

## Quickstart

### Option 1: Docker (recommended)

Published images are `ghcr.io/reversethrottle/cortex-mcp:<tag>` and `ghcr.io/reversethrottle/cortex-mcp:latest`. A version tag push (`v1.2.3` or `1.2.3`) publishes that tag. Publishing a GitHub release also publishes `latest`. The workflow logs in to GHCR with the built-in `GITHUB_TOKEN`.

```bash
# 1. Create your env file (copy .env.example from the repo)
# Edit .env with your Cortex credentials

# 2. Run the published image (stdio mode — used by Claude Desktop)
docker run --env-file /path/to/.env -i --rm ghcr.io/reversethrottle/cortex-mcp:latest
```

To build from this repo instead:

```bash
git clone https://github.com/ReverseThrottle/cortex-mcp.git
cd cortex-mcp
cp .env.example .env
docker build -t cortex-mcp .
docker run --env-file .env -i --rm cortex-mcp
```

### Option 2: Local / Poetry

```bash
# 1. Clone the repo
git clone https://github.com/ReverseThrottle/cortex-mcp.git
cd cortex-mcp

# 2. Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# 3. Install dependencies
poetry install

# 4. Create your env file
cp .env.example .env
# Edit .env with your Cortex credentials

# 5. Start the server
poetry run python src/cli.py start
```

---

## Configuration

All configuration is via environment variables (or a `.env` file in the project root). Copy `.env.example` to `.env` as your starting point.

### Required

| Variable | Description |
|---|---|
| `CORTEX_MCP_PAPI_URL` | Base URL of your Cortex tenant, e.g. `https://api-acme.xdr.us.paloaltonetworks.com` |
| `CORTEX_MCP_PAPI_AUTH_HEADER` | Your API key secret |
| `CORTEX_MCP_PAPI_AUTH_ID` | Numeric ID of the API key |

### Optional — API key type

| Variable | Default | Description |
|---|---|---|
| `CORTEX_MCP_PAPI_KEY_TYPE` | `standard` | `advanced` sends `SHA256(key + nonce + timestamp)` plus `x-xdr-nonce` and `x-xdr-timestamp` on every request. The raw key is not sent. |

### Optional — transport

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` | `0.0.0.0` | Bind host (HTTP mode only) |
| `MCP_PORT` | `8080` | Listen port (HTTP mode only) |
| `MCP_PATH` | `/api/v1/stream/mcp` | URL path (HTTP mode only) |
| `MCP_AUTH_TOKEN` | unset | Bearer token required from clients in HTTP mode (`Authorization: Bearer <token>`). Unauthenticated when unset — only safe for local/stdio use. |

### Optional — feature flags

| Variable | Default | Description |
|---|---|---|
| `MCP_WRITE_TOOLS_ENABLED` | `false` | When `true`, register tools that change tenant or Broker VM appliance state. Read-only tools are always registered. Each mutating tool description states the side effect. |
| `MCP_ISOLATE_ENDPOINT_TOOL_ENABLED` | `false` | When `true`, register `isolate_endpoint` and `unisolate_endpoint`. |
| `MCP_ELICITATION_ENABLED` | `false` | When `true`, a mutating tool asks for confirmation through MCP elicitation before it runs. When `false`, registered tools run without that prompt. Write and isolate flags still control registration. |

### Optional — limits & logging

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `LOG_ENABLE_UVICORN_ACCESS_LOGS` | `true` | Toggle uvicorn HTTP access logs |
| `MAX_OBJECTS_TO_RETRIEVE` | `50` | Default page size for list operations |
| `CORTEX_MCP_RESPONSE_ERROR_MAX_SIZE` | `1000` | Max characters of error detail returned to the LLM |
| `CORTEX_MCP_MAX_RETRIES` | `3` | Extra attempts after the first request when the Cortex call fails to connect or returns HTTP 429 or 503 |

### Optional — on-appliance Broker VM

These settings stay on the server. They are not tool parameters, and the MCP bearer is not sent to the broker. Leave both unset unless you use the 10 appliance tools. Those tools register only when `MCP_WRITE_TOOLS_ENABLED=true`.

| Variable | Default | Description |
|---|---|---|
| `CORTEX_MCP_BROKER_URL` | unset | HTTPS address of the Broker VM. A bare hostname is prefixed with `https://`. |
| `CORTEX_MCP_BROKER_FACTORY_PASSWORD` | unset | Factory or current admin password. The server exchanges it for a 10-minute bearer at `POST /public_api/v1/auth/token` and uses that bearer for the other appliance calls. |

`post_auth_reset_initial_password` sends this password as `current_password`. After it succeeds, this process uses the new password for later broker logins. Set the environment variable to that new password before the next restart.

---

## Connecting to an AI client

### Claude Desktop

Open the Claude Desktop config file (accessible from **Settings → Developer**) and add:

**Docker container:**

```json
{
  "mcpServers": {
    "Cortex MCP": {
      "command": "docker",
      "args": [
        "run",
        "--env-file", "/absolute/path/to/.env",
        "-i", "--rm",
        "ghcr.io/reversethrottle/cortex-mcp:latest"
      ]
    }
  }
}
```

**Local install:**

```json
{
  "mcpServers": {
    "Cortex MCP": {
      "command": "/path/to/cortex-mcp/.venv/bin/python",
      "args": ["/path/to/cortex-mcp/src/main.py"],
      "env": {
        "CORTEX_MCP_PAPI_URL": "https://api-acme.xdr.us.paloaltonetworks.com",
        "CORTEX_MCP_PAPI_AUTH_HEADER": "your_api_key",
        "CORTEX_MCP_PAPI_AUTH_ID": "12345"
      }
    }
  }
}
```

### Cursor / other MCP clients

Most MCP clients accept the same stdio command pattern. Point the command at the Docker run invocation or the local Python binary — exactly as shown for Claude Desktop above.

### HTTP transport (remote server)

Set `MCP_TRANSPORT=streamable-http` in your `.env`, then run the server. The MCP endpoint will be available at:

```
http://<host>:<port>/api/v1/stream/mcp
```

Configure your AI client to connect to that URL using the HTTP MCP transport.

If `MCP_AUTH_TOKEN` is set, clients must send `Authorization: Bearer <token>` on every
request, or they get `401`. Set this for any deployment reachable over a network.
That bearer token authenticates the MCP client to this server. It is not forwarded
to Cortex. Tenant API credentials stay in the server environment.

`streamable-http` is the remote transport. The server does not expose the deprecated HTTP+SSE transport.

A health-check endpoint is also available at `GET /ping/` (unauthenticated).

---

## Available tools

The server exposes the public Cortex tenant API (Cortex Cloud, XDR, XSIAM, Xpanse, AgentiX, and XSOAR 8), the on-appliance Broker VM API, and the helpers below. Read-only tools are always registered. Mutating tools, including the 10 Broker VM appliance tools, are registered only when `MCP_WRITE_TOOLS_ENABLED=true`. `isolate_endpoint` and `unisolate_endpoint` are registered only when `MCP_ISOLATE_ENDPOINT_TOOL_ENABLED=true`. Every tool description states whether the call changes state, including when writes are enabled. When `MCP_ELICITATION_ENABLED=true`, those mutating tools ask for confirmation before they run.

### Helpers

| Tool | Description |
|---|---|
| `get_cases` | Search cases (POST /public_api/v1/case/search). Read-only. |
| `update_case` | Update status, assignment, severity, or comments on a list of cases. Changes tenant state. |
| `get_issues` | Search issues (POST /public_api/v1/issue/search). Read-only. |
| `get_filtered_endpoints` | Filtered endpoint list (POST /public_api/v1/endpoints/get_endpoint). Read-only. |
| `isolate_endpoint` | Isolate endpoints from the network. Changes tenant state. |
| `unisolate_endpoint` | Restore network access to isolated endpoints. Changes tenant state. |
| `get_assets` | Asset inventory search. Read-only. |
| `get_asset_by_id` | Fetch one asset by ID. Read-only. |
| `get_vulnerabilities` | Vulnerability search. Read-only. |
| `run_xql_query` | Start an XQL query, poll until it finishes, and return rows. Consumes XQL quota. The documented start, results, stream, and quota operations are also separate tools. |
| `get_assessment_profile_results` | Assessment profile results. Read-only. |
| `get_tenant_info` | Tenant license information. Read-only. |
| `insert_script` | Upload a script YAML document. The server zips it for POST /public_api/v1/scripts/insert. Changes tenant state. |
| `insert_playbook` | Upload a playbook YAML document. The server zips it for POST /public_api/v1/playbooks/insert. Changes tenant state. |

### Broker VM on the appliance

These 10 tools call the broker appliance, not `/public_api/v1/brokers/` on the tenant. The server logs in with `CORTEX_MCP_BROKER_FACTORY_PASSWORD` and keeps the short-lived bearer. They register only when `MCP_WRITE_TOOLS_ENABLED=true`.

| Tool | Description |
|---|---|
| `post_auth_reset_initial_password` | Replace the factory admin password. The server supplies `current_password`. |
| `post_auth_token` | Exchange the server password for a 10-minute bearer. The token is not returned. |
| `post_logs` | Download the on-appliance log bundle. |
| `post_network_interface` | Configure or disable a physical interface. |
| `post_network_internal_subnet` | Set the Docker parent subnet. Restarts Docker. |
| `post_network_proxy` | Configure the outbound proxy. |
| `post_network_ntp` | Replace the NTP server list. |
| `post_network_ssl_certificate` | Install the HTTPS serving certificate. |
| `post_network_trusted_ca` | Install a trusted CA bundle. |
| `post_register` | Activate the broker with a tenant registration token. |

### Public API catalog (506 additional tools)

506 tools come from the public API reference, in addition to the helpers and Broker VM tools above. The list names every tool (530) registered when write and isolate tools are enabled. `get_endpoints` (all endpoints) is separate from `get_filtered_endpoints`. The documented per-case update is separate from `update_case`.

<details>
<summary>All tool names</summary>

- `delete_appsec_v1_application_by_applicationid`
- `delete_appsec_v1_application_by_applicationid_ass_3e059a`
- `delete_appsec_v1_application_criteria_by_criteriaid`
- `delete_appsec_v1_data_source_instances_by_id`
- `delete_appsec_v1_policies_by_policyid`
- `delete_appsec_v1_rules_by_ruleid`
- `delete_appsec_v1_unified_rules_by_ruleid`
- `delete_cwp_policies_by_id`
- `delete_cwp_registry_onboarding_instances_by_connectorid`
- `delete_engines_by_id`
- `delete_iam_v1_role_by_role_id`
- `delete_iam_v1_user_group_by_group_id`
- `delete_integration_v1_external_application_by_app_4a842b`
- `delete_jobs_by_job_id`
- `delete_notifications_v1_rule_by_rule_uuid`
- `delete_policy_by_policy_id`
- `delete_rule_by_id`
- `delete_settings_integration_by_instance_id`
- `delete_uvm_public_v1_delete_policy_by_id`
- `get_appsec_v1_application`
- `get_appsec_v1_application_by_applicationid`
- `get_appsec_v1_application_by_applicationid_assets_067d6a`
- `get_appsec_v1_application_by_applicationid_assets_75c322`
- `get_appsec_v1_application_by_applicationid_assets_7ffe0d`
- `get_appsec_v1_application_configuration`
- `get_appsec_v1_application_criteria_all`
- `get_appsec_v1_application_criteria_by_criteriaid`
- `get_appsec_v1_billing_contributors`
- `get_appsec_v1_code_to_cloud_coverage`
- `get_appsec_v1_data_source_instances`
- `get_appsec_v1_data_source_instances_by_id`
- `get_appsec_v1_issues_fix_by_issueid_fix_suggestion`
- `get_appsec_v1_issues_fix_by_remediationid`
- `get_appsec_v1_package_explorer_packages_by_name_v_d50c8f`
- `get_appsec_v1_policies`
- `get_appsec_v1_policies_by_policyid`
- `get_appsec_v1_repositories`
- `get_appsec_v1_repositories_by_assetid`
- `get_appsec_v1_repositories_by_assetid_branches`
- `get_appsec_v1_repositories_by_assetid_scan_configuration`
- `get_appsec_v1_rules`
- `get_appsec_v1_rules_by_ruleid`
- `get_appsec_v1_rules_rule_labels`
- `get_appsec_v1_sbom_organization`
- `get_appsec_v1_sbom_repository`
- `get_appsec_v1_scans_by_scanid_findings`
- `get_appsec_v1_scans_by_scanid_issues`
- `get_appsec_v1_scans_ci`
- `get_appsec_v1_scans_periodic`
- `get_appsec_v1_scans_pr`
- `get_appsec_v1_scans_unscanned_repositories`
- `get_appsec_v1_unified_rules`
- `get_appsec_v1_unified_rules_by_ruleid`
- `get_assessment_profile_results`
- `get_asset_by_id`
- `get_assets`
- `get_assets_by_assetid_sbom`
- `get_assets_by_id_raw_fields`
- `get_assets_enum_by_field_name`
- `get_assets_schema`
- `get_automation_metadata`
- `get_brokers`
- `get_brokers_action_status_by_action_id`
- `get_brokers_by_device_id_applets_by_applet_name`
- `get_brokers_by_device_id_logs_download`
- `get_brokers_by_device_id_logs_status`
- `get_brokers_images`
- `get_case_artifacts_by_case_id`
- `get_case_schema`
- `get_cases`
- `get_ciem_v1_access_destination_by_destination_uai`
- `get_ciem_v1_access_granter_by_granter_uai`
- `get_ciem_v1_access_source_by_source_uai`
- `get_ciem_v1_assets_by_assetid_least_privileged_access`
- `get_clcs_get_connected_devices`
- `get_cli_releases_version`
- `get_cloud_consumption_v1_license_posture`
- `get_cloud_consumption_v1_license_runtime`
- `get_contentpacks_metadata_installed`
- `get_cwp_policies`
- `get_cwp_policies_by_id`
- `get_cwp_registry_onboarding_instances_by_connectorid`
- `get_dashboards`
- `get_dashboards_by_dashboard_id`
- `get_engines_download_by_id`
- `get_engines_get`
- `get_engines_get_by_id`
- `get_entry_download_by_entry_id`
- `get_filtered_endpoints`
- `get_healthcheck`
- `get_iam_v1_api_key_by_api_key_id`
- `get_iam_v1_role`
- `get_iam_v1_role_permission_config`
- `get_iam_v1_scope_by_entity_type_by_entity_id`
- `get_iam_v1_user`
- `get_iam_v1_user_by_user_email`
- `get_iam_v1_user_group`
- `get_incident_csv_by_filename`
- `get_incident_load_by_id`
- `get_incidentfields`
- `get_indicators_csv_by_filename`
- `get_integration_v1_external_application`
- `get_integration_v1_external_application_by_applic_9b2917`
- `get_investigation_by_incident_id_workplan`
- `get_issues`
- `get_lists`
- `get_lists_download_by_list_id`
- `get_netscan_v1_scan_run`
- `get_netscan_v1_scan_run_by_id`
- `get_notifications_v1_list_rules`
- `get_notifications_v1_rule_by_rule_uuid`
- `get_performance_incident_export_by_incident_id`
- `get_playbook_by_playbook_id`
- `get_policy_by_policy_id`
- `get_report_by_id_latest`
- `get_reports`
- `get_rule_by_id`
- `get_settings_integration_commands`
- `get_tenant_info`
- `get_uvem_v1_vulnerabilities`
- `get_uvm_public_v1_get_policy_by_id`
- `get_v2_cwp_policies`
- `get_v2_cwp_policies_by_id`
- `get_vc_changes_uncommitted`
- `get_vulnerabilities`
- `get_vulnerability_management_v1_external_scans_as_ed3053`
- `insert_playbook`
- `insert_script`
- `isolate_endpoint`
- `patch_appsec_v1_rules_by_ruleid`
- `patch_iam_v1_user_by_user_email`
- `patch_iam_v1_user_group_by_group_id`
- `patch_notifications_v1_update_rule_status_by_rule_uuid`
- `patch_policy_by_policy_id`
- `patch_rule_by_id`
- `post_actions_file_retrieval_details`
- `post_actions_get_action_status`
- `post_alerts_get_alerts`
- `post_alerts_get_alerts_multi_events`
- `post_alerts_get_alerts_pcap`
- `post_alerts_insert_cef_alerts`
- `post_alerts_insert_parsed_alerts`
- `post_alerts_update_alerts`
- `post_api_keys_delete`
- `post_api_keys_generate`
- `post_api_keys_get_api_keys`
- `post_appsec_v1_application`
- `post_appsec_v1_application_by_applicationid_asset_d07aaa`
- `post_appsec_v1_application_criteria`
- `post_appsec_v1_collectors_by_collectorid`
- `post_appsec_v1_data_source_instances`
- `post_appsec_v1_issues_fix_trigger_fix_pull_request`
- `post_appsec_v1_policies`
- `post_appsec_v1_rules`
- `post_appsec_v1_rules_validate`
- `post_appsec_v1_scan_repository_by_repositoryid`
- `post_appsec_v1_unified_rules`
- `post_asm_management_remove_asm_data`
- `post_asm_management_upload_asm_data`
- `post_asset_groups`
- `post_asset_groups_create`
- `post_asset_groups_delete_by_group_id`
- `post_asset_groups_update_by_group_id`
- `post_assets_assets_internet_exposure_annotation`
- `post_assets_bulk_update_vulnerability_tests`
- `post_assets_create_asset_tag_rules`
- `post_assets_create_user_defined_ip_range`
- `post_assets_delete_unused_tag`
- `post_assets_get_asset_internet_exposure`
- `post_assets_get_assets_internet_exposure`
- `post_assets_get_assets_internet_exposure_last_ext_89526f`
- `post_assets_get_business_units`
- `post_assets_get_external_ip_address_range`
- `post_assets_get_external_ip_address_ranges`
- `post_assets_get_external_ip_address_ranges_last_e_c11293`
- `post_assets_get_external_service`
- `post_assets_get_external_services`
- `post_assets_get_external_services_last_external_a_4d82b2`
- `post_assets_get_external_website`
- `post_assets_get_external_websites`
- `post_assets_get_external_websites_last_external_a_1e24fe`
- `post_assets_get_vulnerability_tests`
- `post_assets_override_bu_tags`
- `post_assets_tags_assets_internet_exposure_assign`
- `post_assets_tags_assets_internet_exposure_remove`
- `post_assets_tags_external_ip_address_ranges_assign`
- `post_assets_tags_external_ip_address_ranges_remove`
- `post_audits_agents_reports`
- `post_audits_management_logs`
- `post_auth_reset_initial_password`
- `post_auth_token`
- `post_authentication_settings_create`
- `post_authentication_settings_delete`
- `post_authentication_settings_get_metadata`
- `post_authentication_settings_get_settings`
- `post_authentication_settings_update`
- `post_automation`
- `post_automation_load_by_script_id`
- `post_automation_search`
- `post_bioc_delete`
- `post_bioc_get`
- `post_bioc_insert`
- `post_brokers_by_device_id`
- `post_brokers_by_device_id_applets_by_applet_name_3f3fe4`
- `post_brokers_by_device_id_applets_by_applet_name_b7ff42`
- `post_brokers_by_device_id_applets_by_applet_name_config`
- `post_brokers_by_device_id_applets_network_mapper_2aed74`
- `post_brokers_by_device_id_applets_wec_wef_cert`
- `post_brokers_by_device_id_delete`
- `post_brokers_by_device_id_logs_generate`
- `post_brokers_by_device_id_reboot`
- `post_brokers_by_device_id_shutdown`
- `post_brokers_by_device_id_upgrade`
- `post_brokers_registration_token`
- `post_case_timeline_by_case_id`
- `post_case_timeline_by_case_id_add_record`
- `post_case_update_by_case_id`
- `post_clcs_disconnect_devices`
- `post_cloud_consumption_v1_details`
- `post_cloud_consumption_v1_over_time`
- `post_cloud_consumption_v1_per_asset_type`
- `post_cloud_onboarding_create_instance_template`
- `post_cloud_onboarding_create_outpost_template`
- `post_cloud_onboarding_delete_instance`
- `post_cloud_onboarding_edit_instance`
- `post_cloud_onboarding_edit_outpost`
- `post_cloud_onboarding_enable_disable_account`
- `post_cloud_onboarding_enable_disable_instance`
- `post_cloud_onboarding_get_accounts`
- `post_cloud_onboarding_get_azure_approved_tenants`
- `post_cloud_onboarding_get_identifiers`
- `post_cloud_onboarding_get_instance_details`
- `post_cloud_onboarding_get_instances`
- `post_cloud_onboarding_get_outposts`
- `post_cloud_onboarding_list_regions`
- `post_compliance_add_assessment_profile`
- `post_compliance_add_control`
- `post_compliance_add_rules_to_control`
- `post_compliance_add_standard`
- `post_compliance_delete_assessment_profile`
- `post_compliance_delete_control`
- `post_compliance_delete_rules_from_control`
- `post_compliance_delete_standard`
- `post_compliance_edit_assessment_profile`
- `post_compliance_edit_control`
- `post_compliance_edit_standard`
- `post_compliance_get_assessment_profile`
- `post_compliance_get_assessment_profiles`
- `post_compliance_get_asset`
- `post_compliance_get_assets`
- `post_compliance_get_control`
- `post_compliance_get_control_by_revision`
- `post_compliance_get_control_categories_and_subcategories`
- `post_compliance_get_control_failed_results`
- `post_compliance_get_controls`
- `post_compliance_get_reports`
- `post_compliance_get_rule_failed_results`
- `post_compliance_get_standard`
- `post_compliance_get_standards`
- `post_configurations_agent_action_center_expiration`
- `post_configurations_agent_action_center_expiration_set`
- `post_configurations_agent_advanced_analysis`
- `post_configurations_agent_advanced_analysis_set`
- `post_configurations_agent_agent_status`
- `post_configurations_agent_agent_status_set`
- `post_configurations_agent_auto_upgrade`
- `post_configurations_agent_auto_upgrade_set`
- `post_configurations_agent_content_management`
- `post_configurations_agent_content_management_set`
- `post_configurations_agent_cortex_xdr_log_collection`
- `post_configurations_agent_cortex_xdr_log_collection_set`
- `post_configurations_agent_critical_environment_ve_ea834b`
- `post_configurations_agent_critical_environment_versions`
- `post_configurations_agent_endpoint_administration_463e12`
- `post_configurations_agent_endpoint_administration_c62e23`
- `post_configurations_agent_informative_btp_issues`
- `post_configurations_agent_informative_btp_issues_set`
- `post_configurations_agent_wildfire_analysis`
- `post_configurations_agent_wildfire_analysis_set`
- `post_content_bundle`
- `post_content_checknew`
- `post_content_install`
- `post_contentpacks_marketplace_search`
- `post_correlations_delete`
- `post_correlations_get`
- `post_correlations_insert`
- `post_cwp_policies`
- `post_cwp_registry_onboarding_instances`
- `post_dashboards_delete`
- `post_dashboards_get`
- `post_dashboards_insert`
- `post_data_security_data_patterns`
- `post_data_security_objects_fields`
- `post_data_security_objects_files`
- `post_dataset_define_dataset`
- `post_dataset_delete_dataset`
- `post_dataset_get_created_datasets`
- `post_device_control_get_violations`
- `post_disable_injection_prevention_rules_add`
- `post_disable_injection_prevention_rules_disable`
- `post_disable_injection_prevention_rules_fetch`
- `post_disable_prevention_add`
- `post_disable_prevention_delete`
- `post_disable_prevention_edit`
- `post_disable_prevention_fetch`
- `post_disable_prevention_get_modules`
- `post_distributions_create`
- `post_distributions_delete`
- `post_distributions_get_dist_url`
- `post_distributions_get_distributions`
- `post_distributions_get_status`
- `post_distributions_get_versions`
- `post_distributions_restore`
- `post_endpoints_abort_scan`
- `post_endpoints_delete`
- `post_endpoints_file_retrieval`
- `post_endpoints_get_endpoints`
- `post_endpoints_get_policy`
- `post_endpoints_get_profiles`
- `post_endpoints_quarantine`
- `post_endpoints_restore`
- `post_endpoints_scan`
- `post_endpoints_update_agent_name`
- `post_endpoints_upgrade`
- `post_engines`
- `post_engines_config`
- `post_engines_upgrade`
- `post_entries_get`
- `post_entries_insert`
- `post_entry`
- `post_entry_execute_sync`
- `post_entry_note`
- `post_entry_tags`
- `post_entry_upload_by_incident_id`
- `post_evidence_delete`
- `post_evidence_search`
- `post_featured_fields_replace_ad_groups`
- `post_featured_fields_replace_hosts`
- `post_featured_fields_replace_ip_addresses`
- `post_featured_fields_replace_users`
- `post_forensics_investigations`
- `post_forensics_investigations_collections`
- `post_forensics_investigations_collections_get_data`
- `post_forensics_investigations_collections_hunt`
- `post_forensics_investigations_collections_triage`
- `post_forensics_investigations_collections_triage_b7c7c2`
- `post_forensics_investigations_collections_triage_fe802a`
- `post_get_attack_surface_rules`
- `post_get_risk_score`
- `post_get_risky_hosts`
- `post_get_risky_users`
- `post_get_triage_presets`
- `post_hash_exceptions_allowlist`
- `post_hash_exceptions_blocklist`
- `post_iam_v1_role`
- `post_iam_v1_user_group`
- `post_incident`
- `post_incident_batch`
- `post_incident_batch_exporttocsv`
- `post_incident_batchdelete`
- `post_incident_close`
- `post_incident_investigate`
- `post_incident_json`
- `post_incident_upload_by_incident_id`
- `post_incidentfield`
- `post_incidents_get_incident_extra_data`
- `post_incidents_get_incidents`
- `post_incidents_search`
- `post_incidents_update_incident`
- `post_incidenttype`
- `post_indicator_create`
- `post_indicator_edit`
- `post_indicators_batch_exporttocsv`
- `post_indicators_batchdelete`
- `post_indicators_delete`
- `post_indicators_feed_json`
- `post_indicators_get`
- `post_indicators_insert`
- `post_indicators_insert_csv`
- `post_indicators_insert_jsons`
- `post_indicators_search`
- `post_indicators_whitelist_update`
- `post_integration_v1_external_application`
- `post_integrations_syslog_create`
- `post_integrations_syslog_delete`
- `post_integrations_syslog_get`
- `post_integrations_syslog_test`
- `post_integrations_syslog_update`
- `post_inv_playbook_task_add_by_investigationid`
- `post_inv_playbook_task_complete`
- `post_inv_playbook_task_execute`
- `post_inv_playbook_task_uncomplete`
- `post_investigation_by_incident_id`
- `post_investigation_by_incident_id_reopen`
- `post_investigation_by_incident_id_workplan_tasks`
- `post_investigation_by_investigation_id_close`
- `post_investigation_by_investigation_id_context`
- `post_issue`
- `post_issue_by_issue_id`
- `post_issue_exceptions`
- `post_issue_exceptions_disable`
- `post_issue_exceptions_search`
- `post_issue_schema`
- `post_itemsdependencies`
- `post_jobs`
- `post_jobs_by_operation_by_job_id`
- `post_jobs_search`
- `post_legacy_exceptions_add`
- `post_legacy_exceptions_delete`
- `post_legacy_exceptions_edit`
- `post_legacy_exceptions_fetch`
- `post_legacy_exceptions_get_modules`
- `post_lists_delete`
- `post_lists_save`
- `post_logs`
- `post_mth_child_add_comment`
- `post_mth_child_get_all_reports`
- `post_mth_child_get_comments`
- `post_mth_child_get_reports_by_incident_id`
- `post_mth_child_get_reports_by_source_id`
- `post_mth_child_get_reports_by_statuses`
- `post_mth_child_report_update_assign`
- `post_mth_child_report_update_status`
- `post_netscan_v1_scan_definition`
- `post_netscan_v1_scan_run`
- `post_netscan_v1_scan_run_by_id`
- `post_netscan_v1_scan_run_by_id_command`
- `post_network_interface`
- `post_network_internal_subnet`
- `post_network_ntp`
- `post_network_proxy`
- `post_network_ssl_certificate`
- `post_network_trusted_ca`
- `post_notifications_v1_rule`
- `post_playbook_clone_by_playbook_id`
- `post_playbook_save_yaml`
- `post_playbook_search`
- `post_playbooks_delete`
- `post_playbooks_get`
- `post_policies_prevention_edit`
- `post_policy`
- `post_policy_search`
- `post_profiles_add_signer_cn_to_allowlist`
- `post_profiles_prevention_add`
- `post_profiles_prevention_edit`
- `post_profiles_prevention_get_modules`
- `post_quarantine_status`
- `post_rbac_get_roles`
- `post_rbac_get_user_group`
- `post_rbac_get_users`
- `post_rbac_set_user_role`
- `post_register`
- `post_relationship`
- `post_relationships_search`
- `post_remediation_confirmation_scanning_requests_g_0c28b0`
- `post_remediation_confirmation_scanning_requests_get`
- `post_rule`
- `post_rule_search`
- `post_scheduled_queries_delete`
- `post_scheduled_queries_insert`
- `post_scheduled_queries_list`
- `post_scripts_delete`
- `post_scripts_get`
- `post_scripts_get_script_code`
- `post_scripts_get_script_execution_results`
- `post_scripts_get_script_execution_results_files`
- `post_scripts_get_script_execution_status`
- `post_scripts_get_script_metadata`
- `post_scripts_get_scripts`
- `post_scripts_run_script`
- `post_scripts_run_snippet_code_script`
- `post_settings_credentials`
- `post_settings_credentials_delete`
- `post_settings_integration_fetch_history`
- `post_settings_integration_reset_by_instance_id`
- `post_settings_integration_search`
- `post_system_diagnostics_data_papi`
- `post_tags_agents_assign`
- `post_tags_agents_create`
- `post_tags_agents_delete_permanently`
- `post_tags_agents_remove`
- `post_triage_endpoint`
- `post_uvem_v1_get_affected_software`
- `post_uvm_public_v1_create_policy`
- `post_uvm_public_v1_list_policies`
- `post_v2_alerts_get_alerts_multi_events`
- `post_v2_cwp_policies`
- `post_v2_xql_delete_dataset`
- `post_vc_changes_uncommitted_commit`
- `post_vulnerability_finding_by_platform_id`
- `post_vulnerability_finding_search`
- `post_vulnerability_finding_snapshot`
- `post_vulnerability_management_v1_external_scans_assets`
- `post_vulnerability_management_v1_scan`
- `post_widgets_delete`
- `post_widgets_get`
- `post_widgets_insert`
- `post_xpanse_remediation_rules_rules`
- `post_xql_add_dataset`
- `post_xql_get_datasets`
- `post_xql_get_query_results`
- `post_xql_get_query_results_stream`
- `post_xql_get_quota`
- `post_xql_library_delete`
- `post_xql_library_get`
- `post_xql_library_insert`
- `post_xql_lookups_add_data`
- `post_xql_lookups_get_data`
- `post_xql_lookups_remove_data`
- `post_xql_start_xql_query`
- `post_xsoar_public_v2_statistics_widgets_query`
- `put_appsec_v1_application_by_applicationid`
- `put_appsec_v1_data_source_instances_by_id`
- `put_appsec_v1_policies_by_policyid`
- `put_appsec_v1_repositories_by_assetid_branches`
- `put_appsec_v1_repositories_by_assetid_scan_configuration`
- `put_appsec_v1_unified_rules_by_ruleid`
- `put_cwp_policies_by_id`
- `put_cwp_registry_onboarding_instances_by_connectorid`
- `put_iam_v1_api_key_by_api_key_id`
- `put_iam_v1_scope_by_entity_type_by_entity_id`
- `put_integration_v1_external_application_by_applic_666c9e`
- `put_notifications_v1_rule_by_rule_uuid`
- `put_settings_credentials`
- `put_settings_integration`
- `put_uvm_public_v1_update_policy_by_id`
- `put_v2_cwp_policies_by_id`
- `run_xql_query`
- `unisolate_endpoint`
- `update_case`
</details>

## CLI reference

The CLI is the recommended way to start the server and keep remote components up to date.

```
python src/cli.py <command> [OPTIONS]
```

### `start`

Start the MCP server.

```bash
python src/cli.py start \
  --api_key_id 12345 \
  --api_key_secret "your-secret" \
  --server-url "https://api-acme.xdr.us.paloaltonetworks.com" \
  --key-type standard \
  --log-level INFO
```

All flags fall back to the corresponding environment variables if not provided.

### `update`

Download the latest Cortex-managed remote components from the Cortex API and replace the `remote_components` folder.

```bash
python src/cli.py update
# or with a custom folder:
python src/cli.py update --folder /path/to/remote_components
```

> **Warning:** The `remote_components` folder is fully replaced on every update. Do not store custom tools there.

### `version`

Print the installed version.

```bash
python src/cli.py version
```

---

## Extending with custom tools

Add your own MCP tools in `src/usecase/custom_components/` — the server discovers and registers them automatically at startup.

### Python module

1. Create a Python file in `src/usecase/custom_components/`, e.g. `my_tool.py`.
2. Define a class that inherits from `BaseModule`:

```python
from fastmcp import Context, FastMCP
from usecase.base_module import BaseModule
from pkg.util import create_response
from usecase.fetcher import get_fetcher

async def my_custom_tool(ctx: Context, asset_id: str) -> str:
    """Short description shown to the LLM."""
    fetcher = await get_fetcher(ctx)
    data = await fetcher.send_request("my/endpoint/", data={"request_data": {"id": asset_id}})
    return create_response(data=data)

class MyToolModule(BaseModule):
    def register_tools(self):
        self._add_tool(my_custom_tool)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
```

3. Restart the server — the tool appears automatically.

### OpenAPI spec

For simple CRUD-style endpoints, drop a YAML file in `src/usecase/custom_components/openapi/`. Use the files in `src/usecase/builtin_components/openapi/` as a template and consult the [Cortex API docs](https://docs-cortex.paloaltonetworks.com/r/Cortex-Cloud-Platform-APIs/Cortex-Cloud-Platform-APIs).

---

## Development

```bash
# Install all dev dependencies
poetry install

# Run tests
poetry run pytest

# Format
poetry run black .
poetry run isort .

# Lint
poetry run ruff check .

# Type-check
poetry run mypy src/
```

### Debugging with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the best tool for interactively testing MCP servers:

```bash
npx @modelcontextprotocol/inspector python src/main.py
```

---

## Project structure

```
cortex-mcp/
├── Dockerfile
├── pyproject.toml
├── .env.example                        # Config template — copy to .env
└── src/
    ├── main.py                         # Async entry point
    ├── cli.py                          # CLI (start / update / version)
    ├── version.py
    ├── config/
    │   └── config.py                   # Pydantic settings (env vars)
    ├── entities/                       # Data models, exceptions, LLM hints
    ├── pkg/                            # HTTP client, logging, utilities
    ├── service/cortex_mcp/
    │   └── server.py                   # FastMCP server factory + lifespan
    └── usecase/
        ├── base_module.py              # BaseModule ABC
        ├── module_util.py              # Auto-discovery of modules
        ├── fetcher.py                  # Authenticated PAPI request helper
        ├── builtin_components/         # Bundled read tools (cases, issues, …)
        │   └── openapi/                # OpenAPI specs for built-in tools
        ├── custom_components/          # YOUR custom tools go here
        │   └── openapi/                # OpenAPI specs for custom tools
        └── remote_components/          # Cortex-managed tools (updated via CLI)
```

---

## Troubleshooting

**`Missing authentication headers`** — Ensure `CORTEX_MCP_PAPI_AUTH_HEADER` and `CORTEX_MCP_PAPI_AUTH_ID` are set correctly.

**`Connection refused` / network errors** — Verify `CORTEX_MCP_PAPI_URL` is reachable from your machine and does not include a trailing `/`.

**Tool not appearing in the AI client** — Check the server logs for registration errors. Confirm the module class inherits from `BaseModule` and calls `super().__init__(mcp)`.

**`update` command fails** — Confirm the API credentials have permission to call the MCP download endpoint and that the target folder exists and is writable.

**XQL query times out** — The server polls up to 30 seconds (15 × 2 s). For long-running queries, reduce scope with tighter filters or a shorter timeframe.

**Debugging** — Set `LOG_LEVEL=DEBUG` for verbose output, or connect with the MCP Inspector for interactive tool testing.
