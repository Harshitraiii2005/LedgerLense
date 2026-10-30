# Harness CI/CD

| File | Purpose |
|---|---|
| `pipeline.yaml` | Pipeline: **Build and Test** (lint, mypy, pytest; build and push the 5 Python images; package and push the umbrella chart to OCI) → **Approve** (only on `main`) → **Deploy** (Native Helm, with automatic `HelmRollback` on failure). |
| `service.yaml` | Harness Service: the umbrella chart from the OCI registry, plus a Values manifest from Git. |
| `values-harness.yaml` | Values overrides. Harness resolves the `<+…>` expressions in it, so every image points at the commit SHA the CI stage pushed. |

All three files are written against the Harness YAML v0 schema. They were not imported into a real
account (none was available), so expect the YAML editor to flag small schema differences on import.

## HUMAN ACTION REQUIRED

1. **Account:** sign up at https://app.harness.io (the free plan includes CI on Harness Cloud and CD). Create
   the org `default` and the project `ledgerlens`, or change `orgIdentifier`/`projectIdentifier` in both YAML files.
2. **Secrets** (Project Settings → Secrets):
   - `registry_user` and `registry_token`, for example a GitHub PAT with `write:packages`.
   - The app secrets used in step 6.
3. **Connectors** (Project Settings → Connectors):
   - **GitHub** (code repo): used by `properties.ci.codebase` and the Values manifest. API access via a PAT secret.
   - **Docker Registry** (`https://ghcr.io`, using the registry secrets): used by `BuildAndPushDockerRegistry`.
   - **OCI Helm repository** (`oci://ghcr.io/<owner>`, same credentials): used by `service.yaml`.
   - **Kubernetes cluster**: install a Harness Delegate in the target cluster (Helm install command
     shown in the UI), then create the connector "via delegate". For kind, run the delegate inside kind.
     For OpenShift, the delegate needs a service account allowed to install into the project.
4. **Entities:**
   - Create the Service from `service.yaml`.
   - Create an Environment (e.g. `dev`) with variable `kafka_bootstrap`.
   - Create an Infrastructure Definition (Kubernetes, the connector above, namespace `ledgerlens`).
5. **Pipeline:** import `pipeline.yaml` (Pipelines → Create → Import From Git → `deploy/harness/pipeline.yaml`).
   Set the pipeline variable `registry` (e.g. `ghcr.io/<owner>`).
6. **Target cluster prerequisites:** create the `ledgerlens-secrets` Secret in the target namespace
   (see deploy/openshift/README.md step 5), plus Kafka, Mongo and Couchbase endpoints. `values-harness.yaml`
   disables the in-cluster infra.
7. **Run:** choose the branch. On `main` the run pauses for approval. At the deploy stage, pick
   environment/infra and `chartVersion` `0.1.0-<short sha>`.
