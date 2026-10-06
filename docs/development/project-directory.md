# Project directory slice (#14)

`ghost_hermes_pm.Manager` is the authoritative same-machine directory service. Its public operations are `read_snapshot(VerifiedIdentity(subject, source), scope=None)` and `apply_directory_change(identity, expected_version, change)`. `VerifiedIdentity` belongs to trusted entry adapters; `source` records provenance and never grants a role. Owner identity comes from trusted configuration. Participant responsibility comes from the persisted Profile binding.

A directory change contains `project` and/or `profile`. Project fields are `id`, `name`, an existing absolute `repo_path`, and explicit `test_artifact_paths`. Profile fields are `id`, `native_profile`, `identity_ref`, `role`, `capability`, `project_id`, `parent_profile_id`, and `connection_refs`. Roles are `steward`, `project_lead`, `subproject_lead`, `independent`; capability classifications are `development`, `non_development`. Connections accept non-sensitive `bot` (`identity:`), `credential` (`native:`), and `codex` (`local:`) references. A new native Profile, bot or repository is not created by this operation. Profile project binding cannot be changed. Repeat corrections use the same stable IDs and current version.

The owner registers identities and boundaries. Project leads remain root project roles; only subproject leads have a parent, which must be a root project lead. The candidate directory is checked against this three-layer/merged organization, including existing children; self-parenting, cycles and extra responsibility layers are rejected. Cross-project child assignment is allowed without changing its long-term project binding. Project leads can correct names in their responsibility scope. The steward can correct global project names and transfer an existing Profile to a registered project lead without changing its project or identity. Independent/subproject assistants have scoped reads. These operations do not mutate native `profile.yaml.role`.

Snapshots return `status`, `version`, `last_verified_at`, `projects`, `profiles`, `runtime`, `execution`, and `needs_human`. The timestamp verifies the directory and repository registration, not a bot or execution connection. Profiles remain `configuring`, `can_execute=false`; their execution capability is disabled. `ManagementError.code` distinguishes `unauthorized`, `forbidden`, `version_conflict`, `binding_conflict`, `invalid_change`, `invalid_repository`, `unknown_version` and `unavailable`. Dashboard maps conflict to HTTP 409, authorization to 403, unavailable to 503, malformed changes to 422.

The root `register(ctx)` is synchronous and only assembles client entries and the native `pre_gateway_dispatch` hook. Ordinary CLI/Profile discovery does not create SQLite, a manager lock or a listener. Only a hook invocation carrying the actual Gateway object can lazily start the configured management Profile's manager and private `manager.sock`, guarded by an exclusive lease and mode 0600. Before the first matching Gateway dispatch, the authority is explicitly unavailable. The registered Profile name/home are captured; callback home/Profile and a bound native secret scope's stamped home must match before credentials are read. Each bridge credential maps to a server-side verified subject; actor/role JSON is rejected. The credential itself is resolved from native secret management and stays in memory. Dashboard delegates to that same socket; it never opens a second SQLite controller. The async Gateway hook starts a formally supervised task that awaits the Gateway's public `wait_for_shutdown()`. Gateway shutdown and plugin unload both stop the bridge, close SQLite and remove only this instance's socket inode; cleanup is idempotent and preserves the database and existing files. An existing socket is preserved and requires explicit reconciliation instead of automatic deletion.

The plugin registers only the read-only `hermes_pm_snapshot` tool and `/hermes-pm` command. Their native SDK contract lacks a verified human sender, so they use a distinct configured participant credential and do not receive owner write authority. The actual resolved token is compared with the owner token without returning either value. The server additionally rejects owner identity on a participant snapshot request, including when the owner secret cannot be resolved in the caller's Profile scope. Missing path/credential configuration reports `configuring`/`not_enabled`; a configured client without a live Gateway authority reports `manager_unavailable`/`not_enabled`.

Trusted settings live under `plugins.entries.ghost-hermes-pm.settings`. This synthetic example illustrates the shape; it is not an installation or migration instruction:

```yaml
manager_profile: fixture-manager
state_dir: /tmp/fixture-coordinator
owner_identity_ref: fixture-owner
dashboard_credential_ref: native:HERMES_PM_OWNER_BRIDGE_TOKEN
allow_local_dashboard_owner: true
# Remote cookie sessions require exact trusted provider/user/tenant selectors.
dashboard_owner_users:
  - provider: fixture-idp
    user_id: fixture-owner
    org_id: fixture-tenant
participant_credential_ref: native:HERMES_PM_PARTICIPANT_TOKEN
participant_entries:
  - identity_ref: fixture-lead
    credential_ref: native:HERMES_PM_PARTICIPANT_TOKEN
```

Dashboard uses the current Hermes host's authenticated interactive session. Remote users require the exact `(provider,user_id,org_id)` selector. Local session-token access needs the explicit trusted opt-in above. Machine token principals are rejected; client `profile` query overrides are rejected. `_require_token` and native secret resolution are isolated in `ghost_hermes_pm.native`, because the former is a current internal host seam rather than a stable PluginContext method. Serving Dashboard must use the management Profile's trusted settings and secret scope. Credentials are never entered in the directory form or returned in responses.

The native directory artifact is the source package containing root `plugin.yaml`, root `__init__.py`, `ghost_hermes_pm/`, and `dashboard/`. `MANIFEST.in` retains those assets in source distributions. The Python wheel installs the management library and its dependencies; it is not by itself a native Dashboard-directory installation. The Dashboard bundle is an ordinary IIFE using the host React and `fetchJSON`; it registers `ghost-hermes-pm`. Its API is `GET /api/plugins/ghost-hermes-pm/snapshot` and `POST /api/plugins/ghost-hermes-pm/directory` with `{expected_version,change}`. Changes are previewed before confirmation. An offline bridge shows only the previous verified snapshot/time and never confirms a write.

Run ordinary behavior tests with `python -m pip install -e '.[test]'` and `python -m pytest`. Native loading is separately explicit:

```sh
HERMES_TEST_SDK_ROOT=/tmp/pristine-hermes-sdk \
HERMES_REQUIRE_SDK_SMOKE=1 python -m pytest tests/test_native_runtime.py
```

Use pristine official Hermes source at `bd0affe5e5f723579df8902852f5d0c47795f355`, with no real `.env` or configuration. The smoke stages source/assets in a short temporary root, uses an empty inherited environment plus fixture credentials, a throwaway home and synthetic Git repo. It exercises the actual native PluginManager threaded loader, tool dispatch and native async hook dispatch, native Dashboard scanner/backend import/mount, same-manager HTTP registration, restart and teardown. A synthetic public Gateway lifetime fixture provides the hook context and shutdown signal. The smoke separately verifies that ordinary CLI discovery does not create authority/occupy its lease, foreign Profile and mismatched secret-scope invocations do not start it, Gateway shutdown closes it before plugin unload, and unload cancels the supervised task without duplicate cleanup. The broad Dashboard host app/auth seam is a fixture, separately covered through HTTP owner-mapping tests. It does not launch `hermes_cli.main`, bootstrap, a real Gateway/Dashboard, Codex, Feishu or an existing user's runtime. Audit tripwires reject real Hermes/credential-file reads and network connections. If the fixture is absent, ordinary tests report an explicit skip; setting `HERMES_REQUIRE_SDK_SMOKE=1` makes absence fail, so CI must require it. Native SDK smoke passing is not real service/robot/capability acceptance.


Gateway runtime evidence is fixed to the SDK commit above: `gateway/run_inbound.py:111–123` calls the formal hook with `event`, `gateway=self` and `session_store`, before sender authorization. This plugin returns no directive and does not bypass that authorization. `hermes_cli/lifecycle.py:32–39` and `hermes_cli/plugins_dispatch.py:482–518` await async hooks on the Gateway's existing loop; `hermes_cli/plugins.py:419–429` supervises `ctx.spawn_task` and cancels it on unload. `gateway/run_shutdown.py:2286–2288` implements the public shutdown waiter. Profile/home selection is documented by `hermes_cli/plugins.py:1768–1791`; secret-scope provenance comes from `agent/secret_scope.py:155–167`. The plugin's explicit captured-home checks cover cross-profile callbacks instead of relying on a source message's identity or an environment variable claiming Gateway mode.
