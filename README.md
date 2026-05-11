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

- **Compose writeback:** only the **`vmid:`** line under that **`services.<name>`** block is rewritten (**`awk`**-style line edit), so **comments and blank lines stay**. You must already have a **`vmid:`** key under the service (e.g. **`vmid: next`**); the service name must be a plain unquoted key matching **`^[a-zA-Z0-9_-]+$`**. Use **`--no-write-compose`** if you do not want the file touched.
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
- **`about`** (optional): multiline string appended under **Notes** in the **composed** Proxmox CT description (see below) for services that use **`guest_ports`** or **`about`**; combined with each service’s own **`about`** (stack text first).
- **`repo`** (optional): controls the **Source** link at the bottom of composed CT descriptions. Omit or **`null`** → default [lennysh/pve-oci-compose](https://github.com/lennysh/pve-oci-compose). Set a **string** (URL) to point elsewhere (forks / internal docs). Set **`false`** to omit that footer entirely.
- **`defaults`** (optional): mapping shallow-merged into **each** service; any key on a service overrides `defaults`.
- **`services`** (required): mapping of **service name** → **service spec** (use stable names; stored inside the CT marker JSON plus the **`pve-oci-compose`** tag).

### Per-service fields

| Field | Required for | Meaning |
|-------|----------------|--------|
| `vmid` | all commands that touch a CT | Fixed number, or **`next`** / **`auto`** / **`null`** / omit = allocate at **apply**; **refresh** needs a number. |
| `image` or `reference` | all | OCI image reference (same conventions as the worker create/refresh scripts). |
| `rootfs` | **plan**, **apply**, **refresh** | e.g. `local-zfs:8` — passed to create; **pull** does not require it. |
| `template_storage` | optional | vztmpl storage id for `oci-registry-pull`; alias `storage`. Empty or omitted lets the create script auto-pick when there is exactly one suitable store. |
| `pool` | optional | Datacenter **resource pool** id for UI grouping. Defaults to top-level **`name`** / **`project`**. Set to **`""`** or **`null`** in **`defaults`** to disable. If the pool id does not exist, **apply** / **refresh** run **`pvesh create /pools`** (opt out with **`PVE_OCI_POOL_NO_AUTOCREATE=1`**). |
| `hostname`, `net0`, `net1`, … | optional | **`pct --netN`**: any keys matching `net[0-9]+` are passed in numeric order. If none are set, **`net0`** defaults to `name=eth0,bridge=vmbr0,ip=dhcp` (or **`OCI_CT_CREATE_NET0`**). |
| `node`, `memory`, `swap`, `cores`, `cpulimit`, `cpuunits`, `ostype`, `arch`, `features` | optional | Passed to **`pct create`** as the matching flags (`--memory`, `--swap`, …). |
| `nameserver` | optional | **`pct --nameserver`**: string (IPs separated by commas or whitespace) or YAML list → one flag per address. |
| `searchdomain` | optional | **`pct --searchdomain`**: string or YAML list (domains joined with spaces). |
| `entrypoint` | optional | **`pct --entrypoint`** (OCI / init command). |
| `env` | optional | **`pct --env`**, repeatable: YAML **mapping** (`KEY: value`) or **list** of `KEY=value` strings (or one string). |
| `description`, `tags` | optional | **`pct --description`** and **`pct --tags`** (UI tags string). **apply** still merges the **`pve-oci-compose`** sentinel tag afterward. |
| `guest_ports` | optional | YAML **list** documenting listener ports **inside the CT** (not Docker Compose publish maps). Each entry is either a **string** (free-form line, e.g. ``5678/tcp — n8n UI``) or a **mapping** with **`port`** (or **`port_number`**), optional **`proto`** / **`protocol`** (default **`tcp`**), and optional **`description`** / **`desc`**. If **`guest_ports`** and/or **`about`** (service or top-level) is set, **`description`** is embedded in a **composed** Markdown-style block (stack, service, image, ports section, notes). If you only set **`description`** and omit **`guest_ports`** / **`about`**, the text is passed **verbatim** (same as before). |
| `about` | optional | Per-service multiline string merged into the **Notes** section when the rich composed description is used (after top-level **`about`**). |
| `timezone`, `password`, `ssh_public_keys` | optional | **`pct --timezone`**, **`--password`**, **`--ssh-public-keys`** (path to a **host** file; must exist when **apply** validates the service). |
| `start`, `startup`, `hookscript` | optional | **`pct --start`**, **`--startup`**, **`--hookscript`** (e.g. `local:snippets/hook.sh`). Booleans/ints follow the same rules as **`onboot`**. |
| `protection`, `ha_managed`, `ignore_unpack_errors`, `pct_debug` | optional | **`pct --protection`**, **`--ha-managed`**, **`--ignore-unpack-errors`**, **`--debug`** (`pct_debug` in YAML to avoid a generic `debug` key). |
| `cmode`, `console`, `tty`, `bwlimit` | optional | **`pct --cmode`**, **`--console`**, **`--tty`**, **`--bwlimit`**. |
| `lxc_dev` | optional | List of device specs → **`--dev0`**, **`--dev1`**, … in list order (see **`pct(1)`** / **`--dev[n]`**). |
| `unused_disks` | optional | List of volume specs → **`--unused0`**, … (advanced; see Proxmox docs). |
| `unprivileged`, `onboot` | optional | YAML booleans or integers; mapped to `pct` flags on create. |
| `mounts` | optional | List of strings `STORAGE:GiB:/absolute/path` → `--mp` on create (extra CT volumes). |

The Proxmox UI may show **`description`** as plain text; Markdown-style headings and bullets still read clearly when copy-pasted. Very long text can hit UI limits—keep secrets out of compose and out of the CT description.

For the **rich** description ( **`guest_ports`** and/or **`about`** / stack **`about`** ), **apply** and **refresh** run a second **`pct set --description`** after the template pull so the CT notes show **Image (pulled):** (the ref **`oci_create_main`** actually used—same skopeo resolution as for floating **`:latest`** ) plus **Template sync:** (UTC time of that pull). If the pulled ref differs from the compose **`image`**, a **Compose file ref:** line is kept for context. **`plan`** previews still reflect the compose file only, not the post-resolve ref.

See **`compose.example.yaml`**: a minimal working service plus **long commented examples** for every optional **`pct create`** field (DNS, **`net1`**, **`env`**, **`startup`** vs **`onboot`**, devices, etc.).

**Create-only fields:** everything in the table above that maps to **`pct create`** (including DNS, **`entrypoint`**, **`env`**, extra **`netN`**, **`lxc_dev`**, …) is applied only when a **new** CT is created. **refresh** does not re-run **`pct create`**; change those settings later with **`pct set`** or the UI (and note that **`password`** in YAML is easy to leak via git — prefer **`pct set`** or secrets after first boot).

## Commands

| Command | Behavior |
|---------|----------|
| **plan** | No changes. For each service: CT exists?, **marker path** + **`tags:`**, **`ref`** read from **`/etc/pve-oci-compose.json`** only, would **apply** / **refresh**? If the vmid is an **existing LXC** without compose marker/tag (e.g. OCI created outside this tool), **plan** prints a **WARNING** and **would REFUSE** for **apply** so you can verify before pulling templates. |
| **apply** | If missing, **create**, then sentinel tag **`pve-oci-compose`** + guest JSON marker (**`pct mount`** briefly on a stopped CT). **plan**/`refresh` use **`pct exec`** when running else **`pct mount`** to read the JSON. Before pull/create, **apply** checks **`pvesh get /cluster/resources`** so a vmid already used by a **QEMU** guest fails immediately (LXC-only **`pct config`** is not enough). An LXC may **run on another cluster node**: **`pct config`** can fail on the member where you run the script even though the guest exists; the tool then uses the API (**hosting node** from resources + **`pvesh …/lxc/<vmid>/config`**) for **tags** and still treats the vmid as taken. If the vmid is an existing LXC without a compose marker/tag, **apply** refuses (same as “not adopted”) instead of pulling then failing at **`pct create`**. |
| **refresh** | If compose **`image`** ≠ stored **`ref`** ( **`--force`** always), refresh rootfs then reconcile tag + JSON. |
| **pull** | For each service, run the create script with **`--pull-only`** (and your `template_storage` / `image`). |

### Flags

| Flag | Meaning |
|------|--------|
| `-f`, `--file PATH` | Compose file (default `./compose.yaml`). |
| `--adopt` | **refresh** only: allow refreshing when guest marker JSON is missing/unreadable (**e.g.** hand-created CT). |
| `--force` | **refresh** only: run refresh even when the stored ref already matches the compose image. |
| `-n`, `--dry-run` | Print worker invocations; do not run them. |
| `--no-write-compose` | After **apply** with `vmid: next` / `auto` / `null`, do not rewrite the compose file (same as **`PVE_OCI_COMPOSE_NO_WRITE=1`**). |

## Compose marker (UI + drift + snapshots)

Canonical **`service`** + **`ref`** live in a small JSON file on the CT root disk (default **`/etc/pve-oci-compose.json`**, configurable with **`PVE_OCI_ROOTFS_MARKER`**). **`pct rollback`** restores that file with the rest of rootfs—so **`plan` / `--force` / drift checks** align with whatever image tree is actually on disk.

Optional host tag (**`pve-oci-compose`**) stays short; **`ref`** drift always comes from the guest JSON (**`stopped`**: **`pct mount`** briefly; **`running`**: **`pct exec cat`**).

After **refresh**, the driver also updates the JSON via **`pct exec`** so the host does not need another mount.

Use **pinned tags or digests** in compose when you care about exactly when a refresh runs; floating `:latest` is easy to misread across machines.

## Operational notes

- **Log style:** **apply** / **pull** (create path) and **refresh** use the same helpers in **`lib/ui.inc.sh`**: phase **title** + horizontal rule, **key/value** lines, **steps**, and **✓** completion. Long explanations and full `pct` / `skopeo` command lines are **hidden by default**; set **`PVE_OCI_VERBOSE=1`** to show them (`out_detail` / `out_cmd`).
- **OCI pull “Waiting for pull task …”** polls `pvesh get /nodes/<node>/tasks/<UPID>/status`. UPIDs contain colons—the tool **URL-encodes** the UPID for that path and **retries** with the raw UPID if the first response is empty. If a pull still hangs after the task logged OK in the UI, verify **`jq`** and run with **`PVE_OCI_COMPOSE_TASK_DEBUG=1`** so each poll prints a short JSON preview on stderr.
- **Entrypoint**: **`pct` `entrypoint`** lives in **`/etc/pve/lxc/<vmid>.conf`**, not in the root disk. After **`refresh`**, that value is synced from the temp CT built from the new image (or **`--delete entrypoint`** if the new template has none). Other config keys (**`ostype`**, **`features`**, …) stay as they were unless you extend the tool.
- **Why a scratch CT for refresh:** Proxmox turns an OCI template into a runnable root tree via **`pct create`** (unpack + metadata). There is no supported one-step “re-unpack this vztmpl **onto** an existing CT’s root volume in place.” Alternatives would be hand-unpacking the **`.tar`** to a directory and **`rsync`** (still scratch space + you must mirror whatever **`pct create`** does), or a second volume you swap in (**ZFS** dataset replace, etc.)—more moving parts. The temp VMID is only a **read source** for **`rsync`**; **`PVE_OCI_CREATE_QUIET=1`** trims duplicate create-style banners so logs read as “refresh,” not “go start this CT.”
- **Stateful data**: keep long-lived data on **`mp`** volumes or bind mounts, not only on rootfs—the refresh worker replaces the root tree (see the refresh script README for details).
- **Snapshots / backups**: refresh uses the worker’s snapshot behaviour; production DR is still **vzdump** / PBS / your policy—not replaced by this tool.
- **Clusters**: run on the node that owns the CT; remote placement is out of scope for this driver.

## Help

```bash
./pve-oci-compose.sh --help
```
