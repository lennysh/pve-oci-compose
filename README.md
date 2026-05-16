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

Override the compose path (`-f` / `--file`, or env **`COMPOSE_FILE`**; default **`./compose.yaml`** in the current directory):

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
- **`defaults`** (optional): mapping shallow-merged into **each** service; any key on a service overrides `defaults`. Exception: **`env`** / **`environment`** mappings are **deep-merged** (service keys override `defaults` on the same variable name).
- **`services`** (required): mapping of **service name** → **service spec** (use stable names; stored inside the CT marker JSON plus the **`pve-oci-compose`** tag). **plan** / **apply** / **refresh** / **pull** run services in **YAML file order** (not alphabetical).

### Per-service fields

| Field | Required for | Meaning |
|-------|----------------|--------|
| `vmid` | all commands that touch a CT | Fixed number, or **`next`** / **`auto`** / **`null`** / omit = allocate at **apply**; **refresh** needs a number. |
| `image` or `reference` | all | OCI image reference (same conventions as the worker create/refresh scripts). |
| `rootfs` | **plan**, **apply**, **refresh** | e.g. `local-zfs:8` — passed to create; **pull** does not require it. |
| `template_storage` | optional | vztmpl storage id for `oci-registry-pull`; alias `storage`. Empty or omitted lets the create script auto-pick when there is exactly one suitable store. |
| `pool` | optional | Datacenter **resource pool** id for UI grouping. Defaults to top-level **`name`** / **`project`**. Set to **`""`** or **`null`** in **`defaults`** to disable. If the pool id does not exist, **apply** / **refresh** run **`pvesh create /pools`** (opt out with **`PVE_OCI_POOL_NO_AUTOCREATE=1`**). |
| `hostname`, `net0`, `net1`, … | optional | **`pct --netN`**: any keys matching **`net[0-9]+`** are passed in numeric order. If none are set, **`net0`** defaults to `name=eth0,bridge=vmbr0,ip=dhcp` (or env **`OCI_CT_CREATE_NET0`**). |
| `node`, `memory`, `swap`, `cores`, `cpulimit`, `cpuunits`, `ostype`, `arch`, `features` | optional | Passed to **`pct create`** as the matching flags (`--memory`, `--swap`, …). **`node`** is also used as a hint when **`refresh`** cannot read **`pct config`** on this cluster member (existence via **`pvesh`**). |
| `nameserver` | optional | **`pct --nameserver`**: string (IPs separated by commas or whitespace) or YAML list → one flag per address. |
| `searchdomain` | optional | **`pct --searchdomain`**: string or YAML list (domains joined with spaces). |
| `entrypoint` | optional | **`pct --entrypoint`** (OCI / init command). |
| `env` / `environment` | optional | Same field: Docker-style **`environment`** is an alias for **`env`**. YAML **mapping** (`KEY: value`), **list** of `KEY=value` strings, a single string, or a list of one-key objects / `{name:, value:}` objects (Kubernetes-style). **`defaults.env`** and service **`env`** are **merged** (service wins on duplicate keys). **OCI vztmpl:** Proxmox sets runtime env from the image **`Config.Env`** during unpack; **apply** then merges **image env + compose env** into **`/etc/pve/lxc/<vmid>.conf`** (`env:` line, NUL-separated — same as **`pct set --env`**, without calling it from Python). Omit **`env`** entirely to keep only the image’s variables. |
| `description`, `tags` | optional | **`pct --description`** and **`pct --tags`** (UI tags string). **apply** still merges the **`pve-oci-compose`** sentinel tag afterward. |
| `guest_ports` | optional | YAML **list** documenting listener ports **inside the CT** (not Docker Compose publish maps). Each entry is either a **string** (free-form line, e.g. ``5678/tcp — n8n UI``) or a **mapping** with **`port`** (or **`port_number`**), optional **`proto`** / **`protocol`** (default **`tcp`**), and optional **`description`** / **`desc`**. **Composed Notes:** with **`guest_ports`** and/or **`about`** (service or top-level), **`description`** is merged into a Markdown-style block (stack, service, image, optional ports/notes). With **none** of those, compose still writes a **minimal** block (stack, service, image, source footer) unless you set **`description:`** alone — then that text is passed **verbatim** and post-pull metadata is not overwritten. |
| `about` | optional | Per-service multiline string merged into the **Notes** section when the rich composed description is used (after top-level **`about`**). |
| `timezone`, `password`, `ssh_public_keys` | optional | **`pct --timezone`**, **`--password`**, **`--ssh-public-keys`** (path to a **host** file; must exist when **apply** validates the service). |
| `start`, `startup`, `hookscript` | optional | **`pct --start`**, **`--startup`**, **`--hookscript`** (e.g. `local:snippets/hook.sh`). Booleans/ints follow the same rules as **`onboot`**. |
| `protection`, `ha_managed`, `ignore_unpack_errors`, `pct_debug` | optional | **`pct --protection`**, **`--ha-managed`**, **`--ignore-unpack-errors`**, **`--debug`** (`pct_debug` in YAML to avoid a generic `debug` key). |
| `cmode`, `console`, `tty`, `bwlimit` | optional | **`pct --cmode`**, **`--console`**, **`--tty`**, **`--bwlimit`**. |
| `lxc_dev` | optional | List of device specs → **`--dev0`**, **`--dev1`**, … in list order (see **`pct(1)`** / **`--dev[n]`**). |
| `unused_disks` | optional | List of volume specs → **`--unused0`**, … (advanced; see Proxmox docs). |
| `unprivileged`, `onboot` | optional | YAML booleans or integers; mapped to `pct` flags on create. |
| `mounts` | optional | List of strings **`STORAGE:GiB:/absolute/path-in-CT`** or **`STORAGE:GiB:/path:extra`** where **`extra`** is comma-separated **`pct`** `mp` flags (fourth **`:`** field: the first **`:`** after the path starts **`extra`**). The path itself must not contain **`:`**. If **`extra`** does not include **`backup=`**, **`backup=1`** is appended so **vzdump** includes the volume by default; use **`…:backup=0`** to exclude. **`bind_mounts`** stay full strings (set **`backup=`** yourself if needed). |
| `bind_mounts` | optional | List of strings for host bind mounts: each entry is the full **`pct`** `mp` value (absolute **host** path, comma-separated options including **`mp=`** guest path), e.g. `/mnt/pve/nfs-media,mp=/mnt/Media01,shared=1,replicate=0,size=0T`. Passed as **`--mp-bind`** after **`mounts`** (indexes continue as **`mp0`**, **`mp1`**, …). Compose checks for `/` + `,mp=`; Proxmox still validates at **`pct create`**. |
| `lxc_config_lines` | optional | List of strings appended **after** a successful **`pct create`** into **`/etc/pve/lxc/<vmid>.conf`** (low-level **`lxc.*`** / **`lxc.mount.entry`** style lines, **`KEY: value`** or **`KEY = value`**). Separate from **`env`** (see above). No **`#` comment markers** in the conf file — Proxmox surfaces **`#` lines from `.conf` in the CT Notes UI. Only runs when **apply** actually creates the CT (not when the vmid already exists). |

The Proxmox UI may show **`description`** as plain text; Markdown-style headings and bullets still read clearly when copy-pasted. Very long text can hit UI limits—keep secrets out of compose and out of the CT description.

**apply** and **refresh** run a second **`pct set --description`** after the template pull when the service is not “verbatim **`description:`** only” (see **`guest_ports`** row). That update adds **Image (pulled):** (resolved ref) and **Template sync:** (UTC). **`plan`** previews still reflect the compose file only, not the post-resolve ref.

See **`compose.example.yaml`**: a minimal working service plus **long commented examples** for every optional **`pct create`** field (DNS, **`net1`**, **`env`**, **`startup`** vs **`onboot`**, devices, etc.).

**Create-only (no second `pct create` on skip):** settings that only affect initial **`pct create`** or post-create updates (**`env`** merge into CT config, **`lxc_config_lines`** append, **`mounts`**, **`bind_mounts`**, **`netN`**, **`rootfs`**, **`hostname`**, **`node`**, **`memory`**, **`swap`**, **`cores`**, limits, **`ostype`**, **`arch`**, **`features`**, **`unprivileged`**, **`onboot`**, **`start`**, **`entrypoint`**, **`nameserver`**, **`searchdomain`**, **`timezone`**, **`password`**, **`ssh_public_keys`**, **`startup`**, **`hookscript`**, **`cmode`**, **`console`**, **`tty`**, **`bwlimit`**, **`protection`**, **`ha_managed`**, **`ignore_unpack_errors`**, **`pct_debug`**, **`lxc_dev`**, **`unused_disks`**, …) are not re-applied when **apply** finds the CT already exists. **refresh** does not recreate **`mp`** volumes or rewrite that marked block. **`pool`** is different: **apply** / **refresh** still ensure pool **membership** when a pool is configured. Changing **`tags`**, **`description`**, or other host config later is normally **`pct set`** / the UI; the composed **description** can still be updated on **apply** / **refresh** when **`guest_ports`** / **`about`** warrant runtime lines (resolved image ref + template sync time). **`password`** in YAML is easy to leak via git — prefer **`pct set`** or secrets after first boot.

## Commands

| Command | Behavior |
|---------|----------|
| **plan** | No changes. For each service: CT exists?, **`tags`** from **`pct`** / API, stored **`ref`** from the guest marker JSON (**`PVE_OCI_ROOTFS_MARKER`**), would **apply** / **refresh**? If the vmid is an **existing LXC** without compose marker/tag (e.g. OCI created outside this tool), **plan** prints a **WARNING** and **would REFUSE** for **apply** so you can verify before pulling templates. |
| **apply** | If missing, **create**, then sentinel tag **`pve-oci-compose`** + guest JSON marker (**`pct mount`** briefly on a stopped CT). **plan**/`refresh` use **`pct exec`** when running else **`pct mount`** to read the JSON. Before pull/create, **apply** checks **`pvesh get /cluster/resources`** so a vmid already used by a **QEMU** guest fails immediately (LXC-only **`pct config`** is not enough). An LXC may **run on another cluster node**: **`pct config`** can fail on the member where you run the script even though the guest exists; the tool then uses the API (**hosting node** from resources + **`pvesh …/lxc/<vmid>/config`**) for **tags** and still treats the vmid as taken. If the vmid is an existing LXC without a compose marker/tag, **apply** refuses (same as “not adopted”) instead of pulling then failing at **`pct create`**. |
| **refresh** | If compose **`image`** ≠ stored **`ref`** ( **`--force`** always), refresh rootfs then reconcile tag + JSON. Existence uses **`pct config`** or, when that fails on a cluster member, **`pvesh`** on the hosting node ( **`/cluster/resources`** ) and your compose **`node:`** hint; if the CT is on another member, the tool tells you to re-run refresh there (**`pct mount`** is local to the host that holds the rootfs). |
| **pull** | For each service, run the create script with **`--pull-only`** (and your `template_storage` / `image`). |

### CLI options

Global options may appear **before or after** the command (`plan`, `apply`, `refresh`, `pull`), e.g. `./pve-oci-compose.sh -f compose.yaml refresh --no-snapshot`.

| Option | Commands | Meaning |
|--------|----------|--------|
| `-f`, `--file PATH` | all | Compose file path. Default: **`./compose.yaml`** in the current directory, or **`$COMPOSE_FILE`** if set. |
| `-n`, `--dry-run` | apply, refresh, pull | Print worker invocations (`DRY-RUN: …`); do not run them. **plan** is always read-only. |
| `--no-write-compose` | apply | After **apply** with `vmid: next` / `auto` / `null`, do not rewrite the compose file with the allocated id. Same as **`PVE_OCI_COMPOSE_NO_WRITE=1`**. |
| `--adopt` | refresh | Refresh a CT that has **no readable guest marker JSON** yet (hand-created / UI OCI guest). After a successful refresh, writes the marker file and merges the **`pve-oci-compose`** sentinel tag. Does **not** make **apply** create over an unmanaged CT. |
| `--force` | refresh | Run refresh even when the stored **`ref`** in the guest marker already matches the compose **`image`**. |
| `--no-snapshot` | refresh | Skip **`pct snapshot`** before rootfs sync. |
| `--allow-failed-snapshot` | refresh | Try **`pct snapshot`** but continue if it fails (default: abort on snapshot failure). Cannot be combined with **`--no-snapshot`**. |
| `-h`, `--help` | — | Print usage (compose schema summary + options) and exit. |

### Environment variables

| Variable | Effect |
|----------|--------|
| **`COMPOSE_FILE`** | Default compose path when **`-f`** is not passed (see above). |
| **`PVE_OCI_COMPOSE_NO_WRITE=1`** | Same as **`--no-write-compose`** (skip vmid writeback after **apply**). |
| **`PVE_OCI_ROOTFS_MARKER`** | Path inside the CT rootfs for the JSON marker (default **`/etc/pve-oci-compose.json`**). |
| **`PVE_OCI_POOL_NO_AUTOCREATE=1`** | Do not auto-create missing Datacenter resource pools; pool must exist before **apply** / **refresh**. |
| **`PVE_OCI_VERBOSE=1`** | Show dim hints and full **`pct`** / **`skopeo`** command lines during **apply** / **pull** / **refresh** (`lib/ui.inc.sh`). |
| **`PVE_OCI_COMPOSE_TASK_DEBUG=1`** | During **`oci-registry-pull`**, print a short JSON preview on each task poll (stderr) if the UPID status poll looks empty. |
| **`PVE_OCI_CREATE_QUIET=1`** | Set internally during **refresh** temp-CT create; fewer banners. Rarely set by hand. |
| **`OCI_CT_CREATE_NET0`** | Default **`pct --net0`** when compose sets no **`netN`** keys (default: `name=eth0,bridge=vmbr0,ip=dhcp`). |
| **`OCI_CT_CREATE_NO_RESOLVE_LATEST=1`** | Skip **`skopeo`** resolution of floating **`:latest`** / **`*_latest`** refs before pull (use the compose string as-is). |
| **`OCI_REFRESH_TEMPLATE_STORAGE`** | Vztmpl storage id for **refresh** temp-CT pulls when a node has multiple template stores. **refresh** from compose sets this from **`template_storage`** / **`storage`** per service; set manually only for direct **`oci_refresh_main`** calls. |

### Advanced: inlined workers (`lib/*.inc.sh`)

The compose driver calls **`oci_create_main`** and **`oci_refresh_main`**; you normally do not invoke them directly. For debugging or one-off use on the node (after sourcing the same **`lib/`** layout as **`pve-oci-compose.sh`**):

**Create / pull** (`oci_create_main`):

| Flag | Meaning |
|------|--------|
| `--reference REF` | OCI image reference (required unless listing storages). |
| `--rootfs SPEC` | New CT root disk **`STORAGE:GiB`** (required for create, not for **`--pull-only`**). |
| `--storage ID` | Vztmpl storage for **`oci-registry-pull`**; omit to auto-pick when exactly one candidate exists. |
| `--pull-only` | Pull template only (what **`pve-oci-compose.sh pull`** uses). |
| `--skip-pull` | Skip **`oci-registry-pull`** (use existing vztmpl on storage). |
| `--reuse-local-template` | Reuse existing normalized **`.tar`** if present. |
| `--list-template-storages` | List vztmpl-capable storages on this node; exit 0. |
| `--vmid` … `--mp-bind`, `--lxc-line`, … | Same **`pct create`** surface as compose fields (see **`oci_create_usage`** in **`lib/oci-create.inc.sh`**). |

**Refresh** (`oci_refresh_main OLD_VMID NEW_REF [TEMP_VMID]`): same **`--no-snapshot`** / **`--allow-failed-snapshot`** flags as **`pve-oci-compose.sh refresh`** (must appear before positional args when calling **`oci_refresh_main`** directly). Optional third argument: temp CT vmid (default: cluster next free id).

## Compose marker (UI + drift + snapshots)

Canonical **`service`** + **`ref`** live in a small JSON file on the CT root disk (default **`/etc/pve-oci-compose.json`**, configurable with **`PVE_OCI_ROOTFS_MARKER`**). **`pct rollback`** restores that file with the rest of rootfs—so **`plan` / `--force` / drift checks** align with whatever image tree is actually on disk.

Optional host tag (**`pve-oci-compose`**) stays short; **`ref`** drift always comes from the guest JSON (**`stopped`**: **`pct mount`** briefly; **`running`**: **`pct exec cat`**).

After **refresh**, the driver also updates the JSON via **`pct exec`** so the host does not need another mount.

Use **pinned tags or digests** in compose when you care about exactly when a refresh runs; floating `:latest` is easy to misread across machines.

## Operational notes

- **Post-create env:** compose **`env`** is merged with the OCI image env by rewriting the CT’s **`env:`** line in **`/etc/pve/lxc/<vmid>.conf`** (NUL-separated pairs). **`lxc_config_lines`** are still appended separately (no **`#` markers** — Proxmox copies **`#` comment lines from the conf into the CT Notes field).
- **Log style:** **apply** / **pull** (create path) and **refresh** use the same helpers in **`lib/ui.inc.sh`**: phase **title** + horizontal rule, **key/value** lines, **steps**, and **✓** completion. Long explanations and full `pct` / `skopeo` command lines are **hidden by default**; set **`PVE_OCI_VERBOSE=1`** to show them (`out_detail` / `out_cmd`).
- **OCI pull “Waiting for pull task …”** polls `pvesh get /nodes/<node>/tasks/<UPID>/status`. UPIDs contain colons—the tool **URL-encodes** the UPID for that path and **retries** with the raw UPID if the first response is empty. If a pull still hangs after the task logged OK in the UI, verify **`jq`** and run with **`PVE_OCI_COMPOSE_TASK_DEBUG=1`** so each poll prints a short JSON preview on stderr.
- **Entrypoint**: **`pct` `entrypoint`** lives in **`/etc/pve/lxc/<vmid>.conf`**, not in the root disk. After **`refresh`**, that value is synced from the temp CT built from the new image (or **`--delete entrypoint`** if the new template has none). Other config keys (**`ostype`**, **`features`**, …) stay as they were unless you extend the tool.
- **Why a scratch CT for refresh:** Proxmox turns an OCI template into a runnable root tree via **`pct create`** (unpack + metadata). There is no supported one-step “re-unpack this vztmpl **onto** an existing CT’s root volume in place.” Alternatives would be hand-unpacking the **`.tar`** to a directory and **`rsync`** (still scratch space + you must mirror whatever **`pct create`** does), or a second volume you swap in (**ZFS** dataset replace, etc.)—more moving parts. The temp VMID is only a **read source** for **`rsync`**; **`PVE_OCI_CREATE_QUIET=1`** trims duplicate create-style banners so logs read as “refresh,” not “go start this CT.”
- **Stateful data**: keep long-lived data on **`mp`** volumes or bind mounts, not only on rootfs—the refresh worker replaces the root tree (see **`lib/oci-refresh.inc.sh`** and the **Advanced** section above for snapshot flags).
- **Snapshots / backups**: refresh uses the worker’s snapshot behaviour; production DR is still **vzdump** / PBS / your policy—not replaced by this tool.
- **Clusters**: run **refresh** on the node that owns the CT (**`pct mount`** / **`rsync`** need local storage). If you run it elsewhere, the driver resolves the guest via the API (and **`node:`** in compose) and errors with the hosting node name instead of “vmid does not exist.”

## Help

```bash
./pve-oci-compose.sh --help    # same as -h; prints options + compose schema summary
```
