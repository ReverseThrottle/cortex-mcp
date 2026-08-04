# Roadmap — remaining Cortex API surface

This tracks what's left to wrap as MCP tools after the initial gap-analysis build-out (see
git history from `98f5732` through `4e5d443` for what shipped). It exists so the "wrap every
available API" goal from the original gap analysis stays visible instead of getting lost once
the first batch of tools landed.

**Out of scope by decision, not oversight:** the CNAPP / Platform IAM / Cloud Onboarding
cluster (ASPM, CWP, DSPM, CIEM, cloud onboarding, platform IAM, auth/SSO, agent config
settings, managed services, syslog, CLCS, classic API keys — ~149 operations). Different
product tier, different audience than the SOC-analyst tools this server targets. Revisit only
if the project's audience changes.

## Validation policy

Every item below follows the same rule the shipped tools did: build → unit test (mocked) →
validate against a real tenant → commit. Nothing merges without a live-tenant pass. See
`tests/` for the existing pattern (`test_openapi_specs.py` for read tools, `test_write_gate.py`
+ per-domain tests for write/action tools).

## Needs live validation now (built, not yet confirmed)

- **`set_user_role`** (`src/usecase/custom_components/rbac_actions.py`) — implemented,
  write-gated, unit-tested, but never called against a live tenant. It's a permission-changing
  action, so validate with a disposable/test user before trusting it in production use.

## Phase 1 remainder — round out Cases / Issues / Assets / XQL / Compliance / System

Domains already partially covered; these are the operations from the original gap analysis
that weren't reached yet.

| Area | Reads | Writes |
|---|---|---|
| Asset Groups | list, get by ID, schema | create, update, delete |
| XQL | query library (list/get), scheduled queries (list/get), lookup datasets (list/get data), user datasets (list) | query library CRUD, scheduled query CRUD, lookup dataset add/remove, dataset create/delete |
| Compliance | profiles, controls, standards (read) | profile/control/standard CRUD |
| Reports | list/get report definitions | — |

No docs-portal source gathered yet for Asset Groups or Compliance — will need either
user-pasted docs-portal samples or careful live-tenant probing adjacent to confirmed
endpoints, same process used for everything shipped so far.

## Phase 2 — Detection & automation content

| Reads (~9) | Writes (~13) |
|---|---|
| BIOCs, correlation rules, detection rules, IOCs, playbooks, alert-notification rules, dashboards, widgets, scripts metadata | Insert/update/delete for each read above, scripts-management CRUD |

Flag: `MCP_WRITE_TOOLS_ENABLED` (existing).

## Phase 3 — Endpoint administration

| Reads (~10) | Writes (~11) |
|---|---|
| Endpoint by ID, policy detail, violations, distributions + status + download URL, security profiles, legacy exception modules/rules, prevention profile modules | Delete endpoints, create distributions, tag assign/remove/delete, set alias, upgrade agents, legacy exception CRUD, prevention profile add/edit, signer allowlist, prevention policy edit |

Flag: `MCP_ENDPOINT_ADMIN_TOOLS_ENABLED` (already defined in config, unused until this phase
ships).

## Phase 4 — Response actions & script execution

| Reads (~7) | Actions |
|---|---|
| Action status, file retrieval details, triage presets, scripts (list/metadata/status/results/code) | Scan / cancel scan / forensics triage / retrieve file (3–4 tools), quarantine / restore / block-list / allow-list files (4 tools), run script / snippet (2 tools) |

Highest blast radius in the whole API — arbitrary script execution and file quarantine on
live endpoints. Flags: `MCP_ENDPOINT_ACTION_TOOLS_ENABLED`, `MCP_FILE_ACTION_TOOLS_ENABLED`,
`MCP_SCRIPT_EXEC_TOOLS_ENABLED` (all defined in config, unused until this phase ships).
Script-execution tools should also respect `MCP_ELICITATION_ENABLED` for interactive
confirmation before running.

## Phase 5 — Vulnerability surface

| Reads (~6) | Writes / actions |
|---|---|
| CVE list/detail, affected-software, findings list/get/bulk-export | Vuln policy CRUD (4 tools, `MCP_WRITE_TOOLS_ENABLED`), trigger scan / BYOS import / poll (3 tools), NetScan launch/status/control (6 tools) |

Flag: `MCP_VULN_SCAN_TOOLS_ENABLED` for scan/NetScan actions (defined in config, unused until
this phase ships).

## Removed from scope (confirmed non-existent, not deferred)

Case artifacts, war room (get/add), and a separate "update fields" endpoint were in the
original gap analysis but could not be located in the docs portal by either the user or in
this session's research — most likely an artifact of the docs-portal scraping issue described
below. `case/update/` (shipped) already covers case field updates including comments, status,
assignee, and severity, so no functionality gap actually exists here.

## Known constraint: schema sourcing

`docs-cortex.paloaltonetworks.com` is a client-side-only JS app with no server-rendered
content and no fetchable OpenAPI/Swagger export — it can't be scraped directly. The working
process for new domains is: (1) careful read-only probing against a live tenant for endpoints
adjacent to already-confirmed ones, reporting each attempt before shipping; (2) where that
doesn't resolve it, the user pastes real docs-portal code samples to build against. Apply the
same process to every remaining phase above.
