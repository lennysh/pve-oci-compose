# pve-oci-compose

Declarative **Proxmox VE** LXC workflows driven by a **YAML** compose file: create OCI-based containers from a registry template, pull templates only, and refresh an existing CT’s **rootfs** from a newer image—similar in spirit to `docker compose`, but wired to `pct` / `pvesh` and the two worker shell scripts this tool invokes.

## What ships in this repository

| Item | Purpose |
|------|--------|
| `pve-oci-compose.sh` | CLI: reads `compose.yaml`, runs **plan**, **apply**, **refresh**, or **pull** |
| `compose.example.yaml` | Copy to `compose.yaml` and edit |

Worker scripts under `oci_ct_create/` and `oci_ct_rootfs_refresh/` are **not tracked** in git (see `.gitignore`). You still need them **on the Proxmox node** next to `pve-oci-compose.sh`, with the expected names:

- `oci_ct_create/oci-ct-create-from-registry.sh`
- `oci_ct_rootfs_refresh/oci-ct-refresh-rootfs.sh`

Copy them from your other repo, a release tarball, or wherever you maintain those scripts. This compose driver only orchestrates them.

## Requirements

- **Proxmox VE** node (run as **root**), with the same capabilities those worker scripts expect (`pct`, `pvesh`, OCI template storage, `skopeo` where applicable—see their own docs).
- **`jq`** (JSON from `pvesh`; same baseline as the create script).
- **`python3`** and **PyYAML** for parsing YAML (not in the Python stdlib). On Debian-based nodes:

  `apt install python3-yaml`

APT packages used by this stack are listed in **`bindep.txt`**. Install them in one shot (from the directory that contains `bindep.txt`):

```bash
sudo apt-get update && sudo apt-get install -y $(awk '!/^[[:space:]]*#/ && NF {print $1}' bindep.txt)
```

## Quick start

On the node, lay out a directory like:

```text
./pve-oci-compose.sh
./compose.yaml
./oci_ct_create/oci-ct-create-from-registry.sh
./oci_ct_rootfs_refresh/oci-ct-refresh-rootfs.sh
```

Create your compose file (start from the example):

```bash
cp compose.example.yaml compose.yaml
# edit compose.yaml: vmids, image refs, rootfs, template storage, …
chmod +x pve-oci-compose.sh
```

Typical flow:

```bash
./pve-oci-compose.sh plan
./pve-oci-compose.sh apply          # create missing CTs only
./pve-oci-compose.sh pull            # optional: templates only (--pull-only)
./pve-oci-compose.sh refresh         # rootfs sync when image ref changed
```

Override the compose path:

```bash
./pve-oci-compose.sh -f /path/to/compose.yaml plan
# or:  COMPOSE_FILE=/path/to/compose.yaml ./pve-oci-compose.sh plan
```

Dry-run (print the underlying commands, do not execute):

```bash
./pve-oci-compose.sh -n apply
./pve-oci-compose.sh --dry-run refresh
```

## Compose file format

The file is YAML with a single top-level mapping.

- **`name`** or **`project`** (optional): informational label for humans; surfaced in **plan** output.
- **`defaults`** (optional): mapping shallow-merged into **each** service; any key on a service overrides `defaults`.
- **`services`** (required): mapping of **service name** → **service spec** (use stable names without spaces; they appear in the CT **description** marker).

### Per-service fields

| Field | Required for | Meaning |
|-------|----------------|--------|
| `vmid` | all commands that touch a CT | Fixed CT ID (recommended for GitOps-style workflows). |
| `image` or `reference` | all | OCI image reference (same conventions as the worker create/refresh scripts). |
| `rootfs` | **plan**, **apply**, **refresh** | e.g. `local-zfs:8` — passed to create; **pull** does not require it. |
| `template_storage` | optional | vztmpl storage id for `oci-registry-pull`; alias `storage`. Empty or omitted lets the create script auto-pick when there is exactly one suitable store. |
| `hostname`, `net0`, `node`, `memory`, `cores`, `ostype`, `features` | optional | Passed through to the create script where supported. |
| `unprivileged`, `onboot` | optional | YAML booleans or integers; mapped to `pct` flags on create. |
| `mounts` | optional | List of strings `STORAGE:GiB:/absolute/path` → `--mp` on create (extra CT volumes). |

See `compose.example.yaml` for a minimal working shape.

## Commands

| Command | Behavior |
|---------|----------|
| **plan** | No changes. For each service: CT exists?, description marker?, would **apply** create?, would **refresh**? |
| **apply** | If `vmid` does not exist, run the **create** worker, then set a **description** marker (below). Existing CTs are left unchanged (no implicit recreate). |
| **refresh** | If the compose image ref differs from the stored ref in the marker (or you pass **`--force`**), run the **refresh** worker, then update the marker. |
| **pull** | For each service, run the create script with **`--pull-only`** (and your `template_storage` / `image`). |

### Flags

| Flag | Meaning |
|------|--------|
| `-f`, `--file PATH` | Compose file (default `./compose.yaml`). |
| `--adopt` | **refresh** only: allow refreshing a CT that does not yet have a `pve-oci-compose` description (e.g. hand-made CT); marker is written after success. |
| `--force` | **refresh** only: run refresh even when the stored ref already matches the compose image. |
| `-n`, `--dry-run` | Print worker invocations; do not run them. |

## Description marker (ownership and refresh drift)

After a successful **apply** or **refresh**, the tool sets:

`pve-oci-compose service=<service-name> ref=<image-string-from-compose>`

**refresh** compares that stored `ref` to the current compose `image` / `reference`. If the CT has no such description, **refresh** skips unless **`--adopt`**.

Use **pinned tags or digests** in compose when you care about exactly when a refresh runs; floating `:latest` is easy to misread across machines.

## Operational notes

- **Stateful data**: keep long-lived data on **`mp`** volumes or bind mounts, not only on rootfs—the refresh worker replaces the root tree (see the refresh script README for details).
- **Snapshots / backups**: refresh uses the worker’s snapshot behaviour; production DR is still **vzdump** / PBS / your policy—not replaced by this tool.
- **Clusters**: run on the node that owns the CT; remote placement is out of scope for this driver.

## Help

```bash
./pve-oci-compose.sh --help
```
