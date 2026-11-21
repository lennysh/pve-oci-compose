# pve-oci-compose

Declarative **Proxmox VE** LXC workflows driven by a **YAML** compose file: create OCI-based containers from a registry template (`oci-registry-pull` + `pct create`), pull templates only, and refresh an existing CT’s **rootfs** from a newer image (snapshot → temp CT → `rsync`). Written in Python (`pve-oci-compose.py` + the `pve_oci_compose/` package); it drives the stock Proxmox tools (`pct`, `pvesh`) on the node.

## What ships in this repository

| Item | Purpose |
|------|--------|
| `pve-oci-compose.py` | CLI: reads `compose.yaml`, runs **plan**, **apply**, **refresh**, or **pull** |
| `pve_oci_compose/` | Implementation: compose loading/validation (`compose.py`), create (`create.py`), refresh (`refresh.py`), guest marker (`marker.py`), symlink-safe guest writes (`guestfs.py`), file injection (`inject.py`), Proxmox CLI wrapper (`pve.py`) |
| `tests/` | `pytest` suite (no Proxmox needed: workflows run against an in-memory fake) |
| `compose.example.yaml` | Copy to `compose.yaml` and edit |
| `bindep.txt` | APT package names for dependencies on Debian-based nodes |

## Requirements

Run on a **Proxmox VE** node as **root** (same host that will own the CTs / storage).

| Source | Packages / tools |
|--------|------------------|
| **Proxmox VE** (preinstalled) | `pct`, `pvesh`, **`oci-registry-pull`**, **`perl`** + **`PVE::Storage`**, cluster API |
| **`bindep.txt`** (install via apt) | `python3` (≥ 3.11), `python3-yaml`, `skopeo`, `rsync` |

Install the extra packages (from the directory that contains `bindep.txt`):

```bash
sudo apt-get update && sudo apt-get install -y $(awk '!/^[[:space:]]*#/ && NF {print $1}' bindep.txt)
```

## Quick start

On the node, use the repo as cloned (the `pve_oci_compose/` directory must sit next to `pve-oci-compose.py`):

```text
./pve-oci-compose.py
./pve_oci_compose/
./compose.yaml
```

Or install it (`pip install .` from the checkout, or `pipx install .`) to get a `pve-oci-compose` command on `PATH`.

### Automatic `vmid` (`next` / `auto` / `null`)

You can set **`vmid: next`**, **`vmid: auto`**, **`vmid: null`**, or **omit `vmid`**: **`apply`** asks the cluster for the next free id, creates the CT with that id, then **rewrites your compose file** so `vmid` becomes that number (so **`refresh`** and later runs use a stable id).

- **Compose writeback:** only the **`vmid:`** line under that **`services.<name>`** block is rewritten (line edit), so **comments and blank lines stay**. You must already have a **`vmid:`** key under the service (e.g. **`vmid: next`**); the service name must be a plain unquoted key matching **`^[a-zA-Z0-9_-]+$`**. Use **`--no-write-compose`** if you do not want the file touched.
- Skip rewriting: **`--no-write-compose`** or **`PVE_OCI_COMPOSE_NO_WRITE=1`**.
- **`refresh`** requires a numeric `vmid` (run **`apply`** once to pin it, or set the id yourself).

Create your compose file (start from the example):

```bash
cp compose.example.yaml compose.yaml
# edit compose.yaml: vmids, image refs, rootfs, template storage, …
```

Typical flow (multiple **`services:`** run in **YAML order**, one after another):

```bash
./pve-oci-compose.py plan
./pve-oci-compose.py apply          # create missing CTs only
./pve-oci-compose.py pull            # optional: templates only (--pull-only)
./pve-oci-compose.py refresh         # rootfs sync when image ref changed
./pve-oci-compose.py refresh --force # re-sync even if marker ref matches compose image
```

Override the compose path (`-f` / `--file`, or env **`COMPOSE_FILE`**; default **`./compose.yaml`** in the current directory):

```bash
./pve-oci-compose.py -f /path/to/compose.yaml plan
# or:  COMPOSE_FILE=/path/to/compose.yaml ./pve-oci-compose.py plan
```

Dry-run (print the underlying commands, do not execute):

```bash
./pve-oci-compose.py -n apply
./pve-oci-compose.py --dry-run refresh
```

Target a single service (by compose key or fixed **vmid**; also works with **plan** and **pull**):

```bash
./pve-oci-compose.py -f compose.yaml apply --service list-sync
./pve-oci-compose.py refresh --vmid 713
./pve-oci-compose.py apply --service traefik --vmid 710   # both must match the same service
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
| `image` or `reference` | all | OCI image reference (bare `name:tag` = Docker Hub; see **Image references and pulls**). |
| `rootfs` | **plan**, **apply**, **refresh** | e.g. `local-zfs:8` — passed to create; **pull** does not require it. |
| `template_storage` | optional | vztmpl storage id for `oci-registry-pull`; alias `storage`. Empty or omitted lets the create script auto-pick when there is exactly one suitable store. |
| `refresh_pre_backup` | optional | Pre-refresh safety: **`snapshot`** (default), **`auto`** (snapshot then **`vzdump`** on failure), **`vzdump`**, **`none`**. |
| `refresh_backup_storage` | optional | **`vzdump`** storage id for **`auto`** / **`vzdump`** (e.g. **`nfs-backups`**, **`pbs`**). Required for **`auto`** fallback when snapshots fail. |
| `refresh_vzdump_remove` | optional | Default **`false`**: pre-refresh **`vzdump`** uses **`--remove 0`** (no prune). Set **`true`** to use **`--remove 1`** so Proxmox may delete older backups per storage **`prune-backups`** when the per-CT backup limit is reached. |
| `refresh_vzdump_protected` | optional | Default **`true`**: mark pre-refresh **`vzdump`** as **`--protected 1`** so scheduled backup jobs skip them. |
| `pool` | optional | Datacenter **resource pool** id for UI grouping. Defaults to top-level **`name`** / **`project`**. Set to **`""`** or **`null`** in **`defaults`** to disable. If the pool id does not exist, **apply** / **refresh** run **`pvesh create /pools`** (opt out with **`PVE_OCI_POOL_NO_AUTOCREATE=1`**). |
| `hostname`, `net0`, `net1`, … | optional | **`pct --netN`**: any keys matching **`net[0-9]+`** are passed in numeric order. If none are set, **`net0`** defaults to `name=eth0,bridge=vmbr0,ip=dhcp` (or env **`OCI_CT_CREATE_NET0`**). |
| `node`, `memory`, `swap`, `cores`, `cpulimit`, `cpuunits`, `ostype`, `arch`, `features` | optional | Passed to **`pct create`** as the matching flags (`--memory`, `--swap`, …). **`node`** is also used as a hint when **`refresh`** cannot read **`pct config`** on this cluster member (existence via **`pvesh`**). |
| `nameserver` | optional | **`pct --nameserver`**: string (IPs separated by commas or whitespace) or YAML list → one flag per address. |
| `searchdomain` | optional | **`pct --searchdomain`**: string or YAML list (domains joined with spaces). |
| `entrypoint` | optional | **`pct --entrypoint`** (OCI / init command). |
| `env` / `environment` | optional | Same field: Docker-style **`environment`** is an alias for **`env`**. YAML **mapping** (`KEY: value`), **list** of `KEY=value` strings, a single string, or a list of one-key objects / `{name:, value:}` objects (Kubernetes-style). At **apply**, runtime env is merged in order: **(1) OCI image** vars from unpack, **(2) `defaults.env`**, **(3) service `env`** (each step overrides the same key). Written as one **`env:`** line in **`/etc/pve/lxc/<vmid>.conf`** (NUL-separated). Omit compose **`env`** entirely to keep only the image’s variables. |
| `description`, `tags` | optional | **`pct --description`** and **`pct --tags`** (UI tags string). **apply** still merges the **`pve-oci-compose`** sentinel tag afterward. |
| `guest_ports` | optional | YAML **list** documenting listener ports **inside the CT** (not Docker Compose publish maps). Each entry is either a **string** (free-form line, e.g. ``5678/tcp — n8n UI``) or a **mapping** with **`port`** (or **`port_number`**), optional **`proto`** / **`protocol`** (default **`tcp`**), and optional **`description`** / **`desc`**. **Composed Notes:** with **`guest_ports`** and/or **`about`** (service or top-level), **`description`** is merged into a Markdown-style block (stack, service, image, optional ports/notes). With **none** of those, compose still writes a **minimal** block (stack, service, image, source footer) unless you set **`description:`** alone — then that text is passed **verbatim** and post-pull metadata is not overwritten. |
| `about` | optional | Per-service multiline string merged into the **Notes** section when the rich composed description is used (after top-level **`about`**). |
| `timezone`, `password`, `ssh_public_keys` | optional | **`pct --timezone`**, **`--password`**, **`--ssh-public-keys`** (path to a **host** file; must exist when **apply** validates the service). |
| `start`, `startup`, `hookscript` | optional | **`pct --start`**, **`--startup`**, **`--hookscript`** (e.g. `local:snippets/hook.sh`). Booleans/ints follow the same rules as **`onboot`**. |
| `protection`, `ha_managed`, `ignore_unpack_errors`, `pct_debug` | optional | **`pct --protection`**, **`--ha-managed`**, **`--ignore-unpack-errors`**, **`--debug`** (`pct_debug` in YAML to avoid a generic `debug` key). |
| `cmode`, `console`, `tty`, `bwlimit` | optional | **`pct --cmode`**, **`--console`**, **`--tty`**, **`--bwlimit`**. |
| `lxc_dev` | optional | List of device specs → **`--dev0`**, **`--dev1`**, … in list order (see **`pct(1)`** / **`--dev[n]`**). |
| `unused_disks` | optional | List of volume specs → **`--unused0`**, … (advanced; see Proxmox docs). |
| `unprivileged`, `onboot` | optional | YAML booleans or integers; mapped to `pct` flags on create. Default **`1`** in **`compose.example.yaml`**. **`unprivileged: 0`** with OCI images fails on current **`pve-container`** releases — see [Known limitations](#privileged-containers-from-oci-images-unprivileged-0). |
| `mounts` | optional | List of strings **`STORAGE:GiB:/absolute/path-in-CT`** or **`STORAGE:GiB:/path:extra`** where **`extra`** is comma-separated **`pct`** `mp` flags (fourth **`:`** field: the first **`:`** after the path starts **`extra`**). The path itself must not contain **`:`**. If **`extra`** does not include **`backup=`**, **`backup=1`** is appended so **vzdump** includes the volume by default; use **`…:backup=0`** to exclude. **`bind_mounts`** stay full strings (set **`backup=`** yourself if needed). |
| `bind_mounts` | optional | List of strings for host bind mounts: each entry is the full **`pct`** `mp` value (absolute **host** path, comma-separated options including **`mp=`** guest path), e.g. `/mnt/pve/nfs-media,mp=/mnt/Media01,shared=1,replicate=0,size=0T`. Passed as **`--mp-bind`** after **`mounts`** (indexes continue as **`mp0`**, **`mp1`**, …). Compose checks for `/` + `,mp=`; Proxmox still validates at **`pct create`**. |
| `lxc_config_lines` | optional | List of strings appended **after** a successful **`pct create`** into **`/etc/pve/lxc/<vmid>.conf`** (low-level **`lxc.*`** / **`lxc.mount.entry`** style lines, **`KEY: value`** or **`KEY = value`**). Separate from **`env`** (see above). No **`#` comment markers** in the conf file — Proxmox surfaces **`#` lines from `.conf` in the CT Notes UI. Only runs when **apply** actually creates the CT (not when the vmid already exists). |
| `rootfs_files` | optional | Inject files into the **guest rootfs** after **apply** (new CT) or **refresh** (when **`on_refresh: true`**). Each entry is a mapping: **`path`** (absolute guest path, e.g. **`/etc/resolv.conf`**), **`source`** (host file relative to the compose file or absolute) **or** **`content`** (inline string), optional **`mode`** (octal, default **`644`**), **`owner`** / **`group`** as **guest** uid/gid (default **`0`** = guest root; on unprivileged CTs mapped to host ids via the CT's id map, normally **`+100000`**; values **≥ 65536** are treated as already-mapped host ids for older compose files), **`on_refresh`** / **`refresh`** (default **`false`** = apply-only). Uses **`pct mount`** on a stopped CT (combined with marker write on **apply**). Writes never follow symlinks or **`..`** out of the guest tree (an image or guest that plants a symlink makes the inject fail instead of writing host files). |
| `mount_files` | optional | Same as **`rootfs_files`**, but targets a **`mounts:`** or **`bind_mounts:`** guest **`mp=`** path: **`mount:`** (guest mount point, e.g. **`/var/lib/app`**), **`path`** (relative to that mount or absolute under it), plus **`source`** / **`content`** and ownership flags. **`mounts`** volumes are written via **`pvesm path`** when needed; **bind** mounts write on the host path. Requires at least one **`mounts`** / **`bind_mounts`** entry. |

The Proxmox UI may show **`description`** as plain text; Markdown-style headings and bullets still read clearly when copy-pasted. Very long text can hit UI limits—keep secrets out of compose and out of the CT description.

**apply** and **refresh** run a second **`pct set --description`** after the template pull when the service is not “verbatim **`description:`** only” (see **`guest_ports`** row). That update adds **Image (pulled):** (resolved ref) and **Template sync:** (UTC). **`plan`** previews still reflect the compose file only, not the post-resolve ref.

See **`compose.example.yaml`**: a minimal working service plus **long commented examples** for every optional **`pct create`** field (DNS, **`net1`**, **`env`**, **`startup`** vs **`onboot`**, devices, etc.).

**Create-only (no second `pct create` on skip):** settings that only affect initial **`pct create`** or post-create updates (**`env`** merge into CT config, **`lxc_config_lines`** append, **`mounts`**, **`bind_mounts`**, **`netN`**, **`rootfs`**, **`hostname`**, **`node`**, **`memory`**, **`swap`**, **`cores`**, limits, **`ostype`**, **`arch`**, **`features`**, **`unprivileged`**, **`onboot`**, **`start`**, **`entrypoint`**, **`nameserver`**, **`searchdomain`**, **`timezone`**, **`password`**, **`ssh_public_keys`**, **`startup`**, **`hookscript`**, **`cmode`**, **`console`**, **`tty`**, **`bwlimit`**, **`protection`**, **`ha_managed`**, **`ignore_unpack_errors`**, **`pct_debug`**, **`lxc_dev`**, **`unused_disks`**, **`rootfs_files`** / **`mount_files`** without **`on_refresh`**, …) are not re-applied when **apply** finds the CT already exists. **`on_refresh`** files are injected after **refresh** **`rsync`** (or with **`refresh --force`** when the image ref already matches). **refresh** does not recreate **`mp`** volumes or rewrite that marked block. **`pool`** is different: **apply** / **refresh** still ensure pool **membership** when a pool is configured. Changing **`tags`**, **`description`**, or other host config later is normally **`pct set`** / the UI; the composed **description** can still be updated on **apply** / **refresh** when **`guest_ports`** / **`about`** warrant runtime lines (resolved image ref + template sync time). **`password`** in YAML is easy to leak via git — prefer **`pct set`** or secrets after first boot.

## Commands

| Command | Behavior |
|---------|----------|
| **plan** | No changes. For each service: CT exists?, **`tags`** from **`pct`** / API, stored **`ref`** from the guest marker JSON (**`PVE_OCI_ROOTFS_MARKER`**), would **apply** / **refresh**? If the vmid is an **existing LXC** without compose marker/tag (e.g. OCI created outside this tool), **plan** prints a **WARNING** and **would REFUSE** for **apply** so you can verify before pulling templates. |
| **apply** | If missing, **create**, then sentinel tag **`pve-oci-compose`** + guest JSON marker (**`pct mount`** briefly on a stopped CT). **plan**/`refresh` read the JSON through **`/proc/<pid>/root`** when running, else **`pct mount`**. Before pull/create, **apply** checks **`pvesh get /cluster/resources`** so a vmid already used by a **QEMU** guest fails immediately (LXC-only **`pct config`** is not enough). An LXC may **run on another cluster node**: **`pct config`** can fail on the member where you run the script even though the guest exists; the tool then uses the API (**hosting node** from resources + **`pvesh …/lxc/<vmid>/config`**) for **tags** and still treats the vmid as taken. If the vmid is an existing LXC without a compose marker/tag, **apply** refuses (same as “not adopted”) instead of pulling then failing at **`pct create`**. Creating a new CT is **all-or-nothing**: if env merge, **`lxc_config_lines`**, the guest marker or file inject fails after **`pct create`**, the new CT is destroyed (and an auto-created pool removed) and the compose file is not touched, so re-running **apply** starts clean. The allocated vmid is written back as soon as the CT is complete. **`start: true`** is applied by **apply** itself after the marker / file inject (not via **`pct create --start`**). |
| **refresh** | Per service: if compose **`image`** ≠ guest marker **`ref`**, run rootfs replace (pre-backup: **`pct snapshot`**, optional **`vzdump`** fallback — see **`refresh_pre_backup`** / **`--pre-backup auto`**), **`rsync`** from a temp CT built from the new image, inject **`on_refresh`** compose files (e.g. **`/etc/resolv.conf`**) while the rootfs is mounted, sync **`entrypoint`** on the host config, destroy temp CT, then **start** the target CT again **only if it was running before**. If anything fails after **`rsync`** began, the CT is automatically **rolled back** to the pre-refresh snapshot (when one was taken; disable with **`OCI_REFRESH_NO_AUTO_ROLLBACK=1`**) and restarted if it was running; failures before that just remove the temp CT and restart the target. Skips when refs match unless **`--force`** (use **`--force`** to re-run refresh only to re-inject **`on_refresh`** files). Skips CTs with no readable marker unless **`--adopt`** (then writes marker + tag after success). **`rsync`** excludes **`mp*`** guest paths (anchored at the rootfs root) so **`--delete`** does not hit busy mountpoints. |
| **pull** | Per service: **`oci-registry-pull`** only (**`--pull-only`**); needs **`image`** / **`reference`**, not **`rootfs`**. |

### CLI options

Global options may appear **before or after** the command (`plan`, `apply`, `refresh`, `pull`), e.g. `./pve-oci-compose.py -f compose.yaml refresh --no-snapshot`.

| Option | Commands | Meaning |
|--------|----------|--------|
| `-f`, `--file PATH` | all | Compose file path. Default: **`./compose.yaml`** in the current directory, or **`$COMPOSE_FILE`** if set. |
| `-n`, `--dry-run` | apply, refresh, pull | Run read-only steps (storage choice, `:latest` resolution) and print every state-changing command (`DRY-RUN: …`) instead of running it. **plan** is always read-only. |
| `--service NAME` | plan, apply, refresh, pull | Run only the **`services:`** entry with that key (other services in the file are skipped). |
| `--vmid ID` | plan, apply, refresh, pull | Run only the service whose compose **`vmid:`** is that number (**`vmid: next`** cannot match). Fails if the id is missing or ambiguous. |
| `--no-write-compose` | apply | After **apply** with `vmid: next` / `auto` / `null`, do not rewrite the compose file with the allocated id. Same as **`PVE_OCI_COMPOSE_NO_WRITE=1`**. |
| `--adopt` | refresh | Refresh a CT that has **no readable guest marker JSON** yet (hand-created / UI OCI guest). After a successful refresh, writes the marker file and merges the **`pve-oci-compose`** sentinel tag. Does **not** make **apply** create over an unmanaged CT. |
| `--force` | refresh | Run refresh even when the stored **`ref`** in the guest marker already matches the compose **`image`**. |
| `--no-snapshot` | refresh | Skip pre-backup (`--pre-backup none`). |
| `--pre-backup MODE` | refresh | **`snapshot`** (default), **`auto`** (snapshot then **`vzdump`** on failure), **`vzdump`**, or **`none`**. |
| `--backup-storage ID` | refresh | **`vzdump`** target for **`auto`** / **`vzdump`** (overrides compose **`refresh_backup_storage`**). |
| `--allow-failed-snapshot` | refresh | Continue if pre-backup fails (default: abort). Cannot combine with **`--no-snapshot`**. |
| `-h`, `--help` | — | Print usage and exit. |

### Environment variables

| Variable | Effect |
|----------|--------|
| **`COMPOSE_FILE`** | Default compose path when **`-f`** is not passed (see above). |
| **`PVE_OCI_COMPOSE_NO_WRITE=1`** | Same as **`--no-write-compose`** (skip vmid writeback after **apply**). |
| **`PVE_OCI_ROOTFS_MARKER`** | Path inside the CT rootfs for the JSON marker (default **`/etc/pve-oci-compose.json`**). |
| **`PVE_OCI_POOL_NO_AUTOCREATE=1`** | Do not auto-create missing Datacenter resource pools; pool must exist before **apply** / **refresh**. |
| **`PVE_OCI_VERBOSE=1`** | Show extra detail and the full **`pct`** / **`pvesh`** / **`skopeo`** command lines (passwords redacted). |
| **`NO_COLOR=1`** | Plain output without colours (also automatic when stdout is not a terminal). |
| **`OCI_CT_CREATE_NET0`** | Default **`pct --net0`** when compose sets no **`netN`** keys (default: `name=eth0,bridge=vmbr0,ip=dhcp`). |
| **`OCI_CT_CREATE_NO_RESOLVE_LATEST=1`** | Skip **`skopeo`** resolution of floating **`:latest`** / **`*_latest`** refs before pull (use the compose string as-is). |
| **`OCI_REFRESH_TEMP_NET0`** | Default **`pct --net0`** for the **disposable temp CT** during **refresh** (default: `name=eth0,bridge=vmbr0,ip=dhcp`). |
| **`OCI_REFRESH_NO_AUTO_ROLLBACK=1`** | On a **refresh** failure after **`rsync`** started, do **not** **`pct rollback`** automatically; leave the CT stopped and print the rollback command. |
| **`OCI_REFRESH_SHUTDOWN_TIMEOUT`** | Seconds to wait for **`pct shutdown`** on the CT being refreshed before **`pct stop`** (default **60**). Temp CT still uses **`pct stop`**. |

### Image references and pulls

- **Compose refs** may be bare (`ghcr.io/org/app:tag`) or prefixed with **`oci://`** / **`docker://`**. **refresh** also accepts a local vztmpl volume (`local:vztmpl/x.tar`) or path, which is used as-is without a registry pull.
- **Floating `:latest` / `*_latest`:** before **`oci-registry-pull`**, the tool **`skopeo`**-resolves the ref to a digest or concrete tag so pulls are repeatable. Opt out with **`OCI_CT_CREATE_NO_RESOLVE_LATEST=1`** (use the compose string as-is).
- **Resolved ref in Notes:** after a successful pull, the resolved ref feeds the composed CT **description** (**Image (pulled):** + **Template sync:**) when that feature is enabled for the service.

### Validation (before **apply** / **refresh** / **pull**)

Per service, the driver checks (non-exhaustive): numeric or **`next`** **`vmid`**; **`image`** / **`reference`** present; **`rootfs`** present (except **pull**); **`ssh_public_keys`** path exists on the host when set; **`guest_ports`**, **`about`**, **`bind_mounts`**, **`lxc_config_lines`** are lists with valid shapes; each **`bind_mounts`** entry starts with **`/`** and contains **`,mp=`**; each **`lxc_config_lines`** entry looks like **`KEY: value`** (no **`[snapshots]`** sections).

## Compose marker (UI + drift + snapshots)

Guest marker JSON (default path **`/etc/pve-oci-compose.json`**, override with **`PVE_OCI_ROOTFS_MARKER`**) stores:

```json
{"service": "<compose service name>", "ref": "<image ref last applied>"}
```

- **`pct rollback`** restores that file with rootfs — **`plan`**, **`--force`**, and drift checks follow the rolled-back tree.
- Host tag **`pve-oci-compose`** is a short UI hint; drift uses the **guest JSON**, not tags alone. It is read from the host: **running** CTs through **`/proc/<pid>/root`** (no shell or **`cat`** needed in the image, so distroless images work), **stopped** CTs with a brief **`pct mount`**.
- The marker is written while the rootfs is mounted (after create, and after the **refresh** **`rsync`**), never by following symlinks inside the guest.
- **plan** warns if marker **`service`** ≠ compose service key for that **`vmid`**.

Use **pinned tags or digests** when you care exactly when **refresh** runs; floating **`:latest`** is easy to misread across machines.

## Operational notes

- **Post-create env (layer order):** **(1)** OCI image vars from unpack, **(2)** **`defaults.env`**, **(3)** service **`env`** — later layers override the same key. The tool removes old **`env:`** / **`lxc.environment.runtime:`** lines and writes one **`env:`** line (NUL-separated). Do not put **`#` comments** in **`/etc/pve/lxc/*.conf`** for compose markers; Proxmox shows **`#` lines in the CT Notes field.
- **Failed create:** if **`pct create`** or any step before the guest marker fails, the new CT is removed, and a resource pool is dropped only if this run auto-created it.
- **Unmanaged CTs:** LXC exists but no marker/tag → **plan** warns, **apply** refuses, **refresh** skips (or **`--adopt`** once you align the CT with compose).
- **Log style:** phase **title** + horizontal rule, **key/value** lines, and **✓** completion. Full `pct` / `skopeo` command lines are **hidden by default**; set **`PVE_OCI_VERBOSE=1`** to show them.
- **OCI pull “Waiting for pull task …”** polls `pvesh get /nodes/<node>/tasks/<UPID>/status`. UPIDs contain colons—the tool **URL-encodes** the UPID for that path and **retries** with the raw UPID if the first response is empty. If a pull still hangs after the task logged OK in the UI, check the node name (**`pvecm nodename`**) and the task log.
- **Entrypoint**: **`pct` `entrypoint`** lives in **`/etc/pve/lxc/<vmid>.conf`**, not in the root disk. After **`refresh`**, that value is synced from the temp CT built from the new image (or **`--delete entrypoint`** if the new template has none). The CT's **`env`** is rebuilt the same way: new image env, then **`defaults.env`**, then service **`env`** (env set by hand in the UI is replaced). Other config keys (**`ostype`**, **`features`**, …) stay as they were.
- **Why a scratch CT for refresh:** Proxmox turns an OCI template into a runnable root tree via **`pct create`** (unpack + metadata). There is no supported one-step “re-unpack this vztmpl **onto** an existing CT’s root volume in place.” Alternatives would be hand-unpacking the **`.tar`** to a directory and **`rsync`** (still scratch space + you must mirror whatever **`pct create`** does), or a second volume you swap in (**ZFS** dataset replace, etc.)—more moving parts. The temp VMID is only a **read source** for **`rsync`** and is destroyed afterwards (also on failure).
- **Stateful data**: keep long-lived data on **`mp`** volumes or bind mounts, not only on rootfs—**refresh** replaces the root tree (see **Snapshots / backups** below).
- **Snapshots / backups**: refresh pre-backup uses **`pct snapshot`** by default; set **`refresh_pre_backup: auto`** and **`refresh_backup_storage`** (or **`--pre-backup auto --backup-storage …`**) to **`vzdump`** when snapshots fail. Pre-refresh **vzdump** notes are **`{hostname} - pve-oci-compose-pre-refresh`**. By default compose uses **`--remove 0`** and **`--prune-backups keep-all=1`** (no prune). **Proxmox still enforces the storage “Max Backups” per guest**: with **`--remove 0`**, a new dump **cannot start** when that limit is full — it will not delete old backups to make room. Use **`refresh_pre_backup: snapshot`** or **`auto`** (local snapshot, no backup-storage quota), a **`refresh_backup_storage`** with a higher limit for pre-refresh only, raise **Max Backups** on the store, delete an old backup manually, or set **`refresh_vzdump_remove: true`** to allow prune for that job only. Pre-refresh dumps are **`--protected 1`** by default ( **`refresh_vzdump_protected: false`** to disable).
- **Short image names**: bare refs like **`traefik:v3.7.3`** are expanded to **`docker.io/library/traefik:v3.7.3`** before **oci-registry-pull** / skopeo (same rules as Docker Hub). Use a full registry path (**`ghcr.io/…`**, **`docker.io/…`**) when the image is not on Docker Hub.
- **Clusters**: run **refresh** on the node that owns the CT (**`pct mount`** / **`rsync`** need local storage). If you run it elsewhere, the driver resolves the guest via the API (and **`node:`** in compose) and errors with the hosting node name instead of “vmid does not exist.”

## Known limitations

These are **Proxmox VE / `pve-container` behaviour**, not bugs in the compose driver. The tool passes **`--unprivileged`** straight through to **`pct create`** (see **`pve_oci_compose/create.py`**); when Proxmox fails during OCI unpack, **`apply`** fails the same way a manual **`pct create`** would.

### Privileged containers from OCI images (`unprivileged: 0`)

**Symptom:** **`apply`** (or **`pct create … --unprivileged 0`**) fails with:

```text
unable to create CT <vmid> - Invalid argument
```

**Cause:** In **`restore_oci_archive()`** (`pve-container`), OCI layer extraction always runs inside a user namespace via **`run_in_userns()`**. For **unprivileged** CTs, **`parse_id_maps()`** supplies UID/GID mappings; for **privileged** CTs the map is **empty**, extraction fails with **`EINVAL`**, and create aborts. Classic **tar** LXC templates do not hit this path.

**Affected in this repo:** Any service with **`unprivileged: 0`** (or YAML **`unprivileged: false`**) on **apply** or on the disposable temp CT during **refresh** when the target CT is privileged. The default in **`compose.example.yaml`** is **`unprivileged: 1`**, which avoids the issue.

**Upstream status (check your node):** As of **`pve-container` 6.1.10**, the fix is **not** in released packages. Verify:

```bash
pveversion -v | grep pve-container
grep -n 'if (@\$id_map)' /usr/share/perl5/PVE/LXC/Create.pm   # no output = still broken
```

When Proxmox ships the fix, the changelog should mention **oci create: fix creating privileged containers** and **`Create.pm`** will contain **`if (@$id_map)`** inside **`restore_oci_archive()`**.

**References:**

| Link | What it is |
|------|------------|
| [Proxmox pve-devel patch (Nov 2025)](https://lore.proxmox.com/pve-devel/20251125141922.165771-1-f.schauer@proxmox.com/) | Upstream one-file fix (`src/PVE/LXC/Create.pm`) — patch series, not a GitHub PR |
| [Proxmox forum report](https://forum.proxmox.com/threads/proxmox-virtual-environment-9-1-available.176255/post-818600) | Original user report referenced in the patch |
| [bpg/terraform-provider-proxmox #2513](https://github.com/bpg/terraform-provider-proxmox/issues/2513) | Independent write-up with root-cause analysis and links into **`pve-container`** source |
| [`PVE/LXC/Create.pm` on git.proxmox.com](https://git.proxmox.com/?p=pve-container.git;a=blob;f=src/PVE/LXC/Create.pm;hb=HEAD) | Current upstream **`restore_oci_archive()`** |
| [proxmox/pve-container mirror](https://github.com/proxmox/pve-container) | Read-only GitHub mirror of **`pve-container`** |

**Workarounds:**

1. **Prefer unprivileged** (recommended for OCI/app containers): omit **`unprivileged`** or set **`unprivileged: 1`**. Use **`features`** (**`nesting`**, **`keyctl`**, **`fuse`**, …) and bind mounts where you need extra capability — same guidance as Proxmox OCI docs.
2. **Unprivileged create, then privileged restore:** create with **`unprivileged: 1`**, then convert once on the node (minimal same-VMID flow):
   ```bash
   VMID=<id> STORAGE=<backup-storage>
   pct stop $VMID
   vzdump $VMID --storage $STORAGE --mode stop
   pct destroy $VMID
   pct restore $VMID "$(ls -1t /var/lib/vz/dump/vzdump-lxc-${VMID}-*.tar.* | head -1)" --unprivileged 0
   pct start $VMID
   ```
   Update compose to **`unprivileged: 0`** afterward for accurate **plan** output (**apply** will not recreate an existing CT).
3. **Local hotfix (advanced):** Back up **`/usr/share/perl5/PVE/LXC/Create.pm`** and apply the [pve-devel patch](https://lore.proxmox.com/pve-devel/20251125141922.165771-1-f.schauer@proxmox.com/) until an official **`pve-container`** release includes it. **`apt upgrade pve-container`** will overwrite the file — re-apply or wait for upstream.

**Not a workaround:** Setting **`unprivileged: 0`** in compose alone, or **`pct set`** on an existing CT — Proxmox does not support flipping privilege in place for OCI-created rootfs the way **restore** does.

## Help

```bash
./pve-oci-compose.py --help    # options; the compose schema is documented above
```
