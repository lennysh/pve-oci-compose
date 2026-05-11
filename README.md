# pve-oci-compose

Declarative **Proxmox VE** LXC workflows driven by a **YAML** compose file: create OCI-based containers from a registry template (`oci-registry-pull` + `pct create`), pull templates only, and refresh an existing CT’s **rootfs** from a newer image (snapshot → temp CT → `rsync`). The implementation lives in this repo (`pve-oci-compose.sh` plus `lib/*.inc.sh`), not in separate helper scripts.

## What ships in this repository

| Item | Purpose |
|------|--------|
| `pve-oci-compose.sh` | CLI: reads `compose.yaml`, runs **plan**, **apply**, **refresh**, or **pull** |
| `lib/common.inc.sh` | Shared helpers (e.g. cluster **nextid** for `vmid: next`, task JSON parsing) |
| `lib/ui.inc.sh` | Shared terminal output (`out_title`, `out_kv`, `out_ok`, …) for create + refresh |
| `lib/oci-create.inc.sh` | OCI vztmpl pull + `pct create` (same behaviour as the former standalone create script) |
| `lib/oci-refresh.inc.sh` | Rootfs refresh workflow (same behaviour as the former standalone refresh script) |
| `compose.example.yaml` | Copy to `compose.yaml` and edit |
| `bindep.txt` | APT package names for dependencies on Debian-based nodes |

## Requirements

- **Proxmox VE** node (run as **root**): `pct`, `pvesh`, OCI template storage with **`oci-registry-pull`**, `skopeo`, `rsync`, **`jq`**, **`perl`** with **`PVE::Storage`** (same as a normal PVE node used for OCI templates).
- **`python3`** and **PyYAML** (`python3-yaml`) to parse the compose file (`apt install python3-yaml`).

APT packages used by this stack are listed in **`bindep.txt`**. Install them in one shot (from the directory that contains `bindep.txt`):

```bash
sudo apt-get update && sudo apt-get install -y $(awk '!/^[[:space:]]*#/ && NF {print $1}' bindep.txt)
```

## Quick start

On the node, use the repo layout as cloned (the `lib/` directory must sit next to `pve-oci-compose.sh`):

```text
./pve-oci-compose.sh
./lib/common.inc.sh
./lib/ui.inc.sh
./lib/oci-create.inc.sh
./lib/oci-refresh.inc.sh
./compose.yaml
```

### Automatic `vmid` (`next` / `auto` / `null`)

You can set **`vmid: next`**, **`vmid: auto`**, **`vmid: null`**, or **omit `vmid`**: **`apply`** asks the cluster for the next free id, creates the CT with that id, then **rewrites your compose file** so `vmid` becomes that number (so **`refresh`** and later runs use a stable id).

- **YAML caveat:** the updater uses PyYAML `safe_dump`; **comments and some formatting may change** — keep the file in git or back it up first.
- Skip rewriting: **`--no-write-compose`** or **`PVE_OCI_COMPOSE_NO_WRITE=1`**.
- **`refresh`** requires a numeric `vmid` (run **`apply`** once to pin it, or set the id yourself).

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
- **`services`** (required): mapping of **service name** → **service spec** (use stable names without spaces; embedded in compose **pct tags**, see marker section).

### Per-service fields

| Field | Required for | Meaning |
|-------|----------------|--------|
| `vmid` | all commands that touch a CT | Fixed number, or **`next`** / **`auto`** / **`null`** / omit = allocate at **apply**; **refresh** needs a number. |
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
| **plan** | No changes. For each service: CT exists?, **tags** + stored image ref?, would **apply** create?, would **refresh**? |
| **apply** | If the target CT does not exist, run **create** (pull + `pct create`), then **`pct set --tags`** merge-in the compose marker (below). Legacy **description-only** markers are cleared when superseded by tags. With **`vmid: next`**, writes the allocated id back into the compose file unless disabled. Existing CTs are left unchanged (no implicit recreate). |
| **refresh** | If the compose image ref differs from the stored ref in **tags** (or **`--force`**), run the refresh worker, then update **tags**. |
| **pull** | For each service, run the create script with **`--pull-only`** (and your `template_storage` / `image`). |

### Flags

| Flag | Meaning |
|------|--------|
| `-f`, `--file PATH` | Compose file (default `./compose.yaml`). |
| `--adopt` | **refresh** only: allow refreshing a CT with no compose **tags** and no legacy `pve-oci-compose … ref=…` **description**; marker tags are written after success. |
| `--force` | **refresh** only: run refresh even when the stored ref already matches the compose image. |
| `-n`, `--dry-run` | Print worker invocations; do not run them. |
| `--no-write-compose` | After **apply** with `vmid: next` / `auto` / `null`, do not rewrite the compose file (same as **`PVE_OCI_COMPOSE_NO_WRITE=1`**). |

## Tags marker (ownership and refresh drift)

Proxmox **tags** allow only `[a-z0-9_.+-]` per segment, so the image ref cannot be stored raw. After **apply** / **refresh** the driver merges **`pct --tags`** with any tags you already have and adds:

- **`pve-oci-compose`** — ownership sentinel  
- **`pveocid1` + URL-safe Base64(JSON `{"service","ref"}`)** — canonical compose service name + image ref string  

**Legacy:** CTs provisioned earlier may still expose only **`pve-oci-compose service=… ref=…`** as **`pct`** **notes** (**description**). **plan** / **refresh** read **tags first**, then fall back to that description line until you next **apply**/ **refresh**.

**Rollback note:** **`pct rollback`** restores the CT rootfs from the snapshot but does **not** revert **`/etc/pve/lxc/*.conf`**, so **tags** still show the newer ref until you **`pct set`** them again—or run compose **refresh**/ **apply** as appropriate after a rollback.

**refresh** compares the stored **`ref`** to the current compose **`image`** / **`reference`**. If neither tags nor legacy description carries a **`ref`**, **refresh** skips unless **`--adopt`**.

Use **pinned tags or digests** in compose when you care about exactly when a refresh runs; floating `:latest` is easy to misread across machines.

## Operational notes

- **Log style:** **apply** / **pull** (create path) and **refresh** use the same helpers in **`lib/ui.inc.sh`**: phase **title** + horizontal rule, **key/value** lines, **steps**, and **✓** completion. Long explanations and full `pct` / `skopeo` command lines are **hidden by default**; set **`PVE_OCI_VERBOSE=1`** to show them (`out_detail` / `out_cmd`).
- **OCI pull “Waiting for pull task …”** polls `pvesh get /nodes/<node>/tasks/<UPID>/status`. UPIDs contain colons—the tool **URL-encodes** the UPID for that path and **retries** with the raw UPID if the first response is empty. If a pull still hangs after the task logged OK in the UI, verify **`jq`** and run with **`PVE_OCI_COMPOSE_TASK_DEBUG=1`** so each poll prints a short JSON preview on stderr.
- **Stateful data**: keep long-lived data on **`mp`** volumes or bind mounts, not only on rootfs—the refresh worker replaces the root tree (see the refresh script README for details).
- **Snapshots / backups**: refresh uses the worker’s snapshot behaviour; production DR is still **vzdump** / PBS / your policy—not replaced by this tool.
- **Clusters**: run on the node that owns the CT; remote placement is out of scope for this driver.

## Help

```bash
./pve-oci-compose.sh --help
```
