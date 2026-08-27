#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic>=2.9",
#   "tomlkit>=0.13",
# ]
# ///
"""Import an official TerraFirmaGreg-Modern release into this repository's Packwiz tree.

Desired state comes from two verified inputs:

  * the official CurseForge release zip (--zip): manifest.json carries the
    resolved CurseForge project/file pairs; overrides/ carries the resolved
    payload trees (config, defaultconfigs, kubejs, tacz) plus raw binary
    assets under mods/, resourcepacks/, shaderpacks/;
  * the release tag's pakku-lock.json (--lock): per-provider ids, filenames,
    sha1 hashes, and declared sides for every project.

Phases (strict dataflow, side effects only at the edges):

  scan   - read the two inputs, the previous release zip, the pack tree,
           and the existing metafiles. Produces a ScanRecord; no writes.
  plan   - PURE. Diff scan inputs into an immutable ImportPlan of Actions.
  apply  - the only writer. Executes the plan, runs `packwiz refresh`.

Invariants worth keeping forever:

  * Only *.pw.toml files are metadata entries; every other file under the
    pack is a payload synced by hash. Metadata and payloads are disjoint.
  * A metafile is left untouched only when its canonical rendering is
    byte-identical; otherwise it is rewritten canonically.
  * Payload deletions are classified against the previous release zip:
    present before + absent now -> upstream-deleted -> remove;
    absent from both releases -> local-only -> preserve and warn.
  * Metafiles that cannot be identified from the new release (no matching
    CurseForge identity, e.g. local Modrinth overlays such as Distant
    Horizons) are never touched; they are reported for manual review.
  * Lock records the release neither references nor ships raw are skipped
    with a note (upstream's export-time exclusions, e.g. ProbeJS).

  uv run scripts/import_tfg_release.py \
      --zip /tmp/TerraFirmaGreg-Modern-<v>-curseforge.zip \
      --lock /tmp/<tag>/pakku-lock.json \
      --prev-zip /tmp/TerraFirmaGreg-Modern-<prev>-curseforge.zip \
      --release-version <v> \
      [--expected-sha256 <hex>] [--local-retain distanthorizons.pw.toml] \
      [--check] [--apply]

Modes:

  default           plan and print a full report; no writes.
  --check           plan and exit 0 only if no mutating actions exist.
                    Point --zip/--lock at the PREVIOUS release to calibrate
                    the importer against a tree that should already match.
  --apply           execute the planned actions, then `packwiz refresh`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError

# ---------------------------------------------------------------------------
# Input schemas (validated at the scan boundary)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PakkuFile(_Strict):
    type: str  # "curseforge" | "modrinth"
    file_name: str
    id: str
    parent_id: str
    url: str | None = None
    hashes: Mapping[str, str]
    date_published: str = ""


class PakkuProject(_Strict):
    pakku_id: str
    type: str  # MOD | RESOURCE_PACK | SHADER | ...
    side: str | None = None  # CLIENT | SERVER | None (unspecified => both)
    slug: Mapping[str, str]
    name: Mapping[str, str]
    id: Mapping[str, str]
    files: Sequence[PakkuFile]


class PakkuLock(_Strict):
    mc_versions: Sequence[str]
    loaders: Mapping[str, str]
    projects: Sequence[PakkuProject]


class ManifestLoader(_Strict):
    id: str
    primary: bool = False


class ManifestFile(_Strict):
    projectID: int
    fileID: int


class MinecraftSpec(_Strict):
    version: str
    modLoaders: Sequence[ManifestLoader]


class Manifest(_Strict):
    minecraft: MinecraftSpec
    files: Sequence[ManifestFile]


# ---------------------------------------------------------------------------
# Plan vocabulary (immutable records; applied verbatim by apply_plan())


@dataclass(frozen=True)
class KeepMeta:
    """Existing metafile already byte-identical to canonical rendering."""

    rel: str


@dataclass(frozen=True)
class WriteMeta:
    """Create or rewrite a metafile with canonical content."""

    rel: str
    body: str
    detail: str


@dataclass(frozen=True)
class DeleteMeta:
    rel: str
    detail: str


@dataclass(frozen=True)
class WritePayload:
    """Copy a file from the release zip into the pack tree."""

    rel: str
    zip_path: str  # "overrides/..." entry inside the release zip
    reason: str  # add | update


@dataclass(frozen=True)
class DeletePayload:
    rel: str
    detail: str  # upstream-removed


@dataclass(frozen=True)
class SetVersion:
    version: str


type Action = KeepMeta | WriteMeta | DeleteMeta | WritePayload | DeletePayload | SetVersion


@dataclass(frozen=True)
class Note:
    kind: str  # "skip-lock-record" | "warn" | "info"
    subject: str
    detail: str

    def __str__(self) -> str:
        prefix = {"warn": "WARN", "skip-lock-record": "SKIP", "info": "NOTE"}[self.kind]
        return f"{prefix}: {self.subject}: {self.detail}"


@dataclass(frozen=True)
class ImportPlan:
    actions: tuple[Action, ...]
    notes: tuple[Note, ...]

    @property
    def mutating(self) -> bool:
        return any(not isinstance(a, KeepMeta) for a in self.actions)


# ---------------------------------------------------------------------------
# Scan-phase records


@dataclass(frozen=True)
class Entry:
    digest: str


@dataclass(frozen=True)
class ScanRecord:
    lock: PakkuLock
    manifest: Manifest
    """Every payload in the release, relative to the pack root."""
    release_entries: Mapping[str, Entry]  # rel -> digest, from overrides/*
    prev_override_rels: frozenset[str]  # rel set from previous zip overrides/*
    """Existing metafiles, keyed by pack-relative path (e.g. "mods/x.pw.toml")."""
    current_metas: Mapping[str, str]  # rel -> raw bytes text
    """Existing payload files, keyed by pack-relative path."""
    current_payloads: Mapping[str, Entry]  # rel -> digest
    pack_version: str


META_DIRS = ("mods", "resourcepacks", "shaderpacks")
PAYLOAD_DIRS = ("config", "defaultconfigs", "kubejs", "tacz", *META_DIRS)
SIDE_BY_DECLARED = {"CLIENT": "client", "SERVER": "server"}


# ---------------------------------------------------------------------------
# Scan boundary: everything that touches the filesystem happens below here


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def validated[T: BaseModel](model_cls: type[T], data: dict, origin: Path) -> T:
    try:
        return model_cls.model_validate(data)
    except ValidationError as err:
        raise SystemExit(f"FATAL: {origin} does not match the expected schema:\n{err}") from err


def parse_metafile_text(text: str) -> Mapping[str, object]:
    """Extract the identity fields of a packwiz metafile (lossy view)."""

    doc = tomlkit.parse(text)
    table = doc.unwrap()
    return {
        "name": table.get("name"),
        "filename": table.get("filename"),
        "side": table.get("side"),
        "hash": (table.get("download") or {}).get("hash"),
        "url": (table.get("download") or {}).get("url"),
        "cf_file_id": ((table.get("update") or {}).get("curseforge") or {}).get("file-id"),
        "cf_project_id": ((table.get("update") or {}).get("curseforge") or {}).get("project-id"),
        "mr_mod_id": ((table.get("update") or {}).get("modrinth") or {}).get("mod-id"),
        "mr_file_id": ((table.get("update") or {}).get("modrinth") or {}).get("version"),
    }


def scan(zip_path: Path, lock_path: Path, prev_zip_path: Path | None, pack_dir: Path,
         expected_zip_sha256: str | None) -> ScanRecord:
    if expected_zip_sha256 is not None:
        actual = sha256_file(zip_path)
        if actual != expected_zip_sha256:
            raise SystemExit(
                f"FATAL: {zip_path.name} sha256 {actual} != expected {expected_zip_sha256}"
            )
        print(f"verified {zip_path.name} sha256")

    lock = validated(PakkuLock, load_json(lock_path), lock_path)

    with zipfile.ZipFile(zip_path) as zf:
        manifest = validated(Manifest, json.loads(zf.read("manifest.json")), zip_path)
        release_entries: dict[str, Entry] = {}
        seen_normed: set[str] = set()
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = [p for p in Path(info.filename).parts]
            if any(p in ("", ".", "..") or p.startswith("/") or ":" in p
                   for p in parts) or Path(info.filename).is_absolute():
                raise SystemExit(
                    f"FATAL: unsafe zip member path {info.filename!r}"
                )
            if len(parts) < 3 or parts[0] != "overrides":
                continue  # manifest.json and non-override members are ignored
            if parts[1] not in PAYLOAD_DIRS:
                raise SystemExit(
                    f"FATAL: zip carries unregistered override root '{parts[1]}/'; "
                    "extend PAYLOAD_DIRS deliberately if upstream added one"
                )
            rel = "/".join(parts[1:])
            if rel in seen_normed:
                raise SystemExit(f"FATAL: duplicate zip member resolves to {rel!r}")
            seen_normed.add(rel)
            release_entries[rel] = Entry(sha256_bytes(zf.read(info)))

    prev_override_rels: frozenset[str] = frozenset()
    if prev_zip_path is not None:
        with zipfile.ZipFile(prev_zip_path) as pz:
            prev_override_rels = frozenset(
                "/".join(Path(n).parts[1:])
                for n in pz.namelist()
                if n.startswith("overrides/") and not n.endswith("/")
            )

    current_metas: dict[str, str] = {}
    current_payloads: dict[str, Entry] = {}
    for path in sorted(pack_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(pack_dir).as_posix()
        if rel in ("index.toml", "pack.toml"):
            continue
        if rel.endswith(".pw.toml"):
            current_metas[rel] = path.read_text()
        else:
            current_payloads[rel] = Entry(sha256_file(path))

    pack_version_match = re.search(r'^version = "(.*)"$', (pack_dir / "pack.toml").read_text(), re.MULTILINE)
    return ScanRecord(
        lock=lock,
        manifest=manifest,
        release_entries=release_entries,
        prev_override_rels=prev_override_rels,
        current_metas=current_metas,
        current_payloads=current_payloads,
        pack_version=pack_version_match.group(1) if pack_version_match else "",
    )


# ---------------------------------------------------------------------------
# Planning: pure functions below; no filesystem access permitted


def plan_lock_slug(project: PakkuProject) -> str:
    return project.slug.get("curseforge") or next(iter(project.slug.values()))


def plan_display_name(project: PakkuProject) -> str:
    return project.name.get("curseforge") or next(iter(project.name.values()), plan_lock_slug(project))


def plan_target_dir(project: PakkuProject) -> str | None:
    return {
        "MOD": "mods",
        "RESOURCE_PACK": "resourcepacks",
        "SHADER": "shaderpacks",
    }.get(project.type)


def plan_side(project: PakkuProject) -> str:
    return SIDE_BY_DECLARED.get(project.side or "", "both")


def plan_render_metafile(display_name: str, filename: str, side: str, sha1: str,
                         *,
                         download_url: str | None,
                         update_curseforge: tuple[str, str] | None = None,
                         update_modrinth: tuple[str, str] | None = None) -> str:
    """Canonical packwiz metafile rendering.

    download_url present => direct-url download mode (no metadata line);
    otherwise CurseForge metadata mode. One optional update block of either
    provider kind may accompany a download-url entry.
    """
    quoted = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = [
        f"name = {quoted(display_name)}",
        f"filename = {quoted(filename)}",
        f'side = "{side}"',
        "",
        "[download]",
        'hash-format = "sha1"',
        f"hash = {quoted(sha1)}",
    ]
    if download_url:
        lines.append(f"url = {quoted(download_url)}")
    else:
        lines.append('mode = "metadata:curseforge"')
    if update_curseforge:
        file_id, project_id = update_curseforge
        lines += ["", "[update]", "[update.curseforge]",
                  f"file-id = {file_id}", f"project-id = {project_id}"]
    elif update_modrinth:
        mod_id, version_id = update_modrinth
        lines += ["", "[update]", "[update.modrinth]",
                  f"mod-id = {quoted(mod_id)}", f"version = {quoted(version_id)}"]
    return "\n".join(lines) + "\n"


def plan_provider(project: PakkuProject, wanted_file_id: int | None) -> PakkuFile:
    """Choose the provider record: the CurseForge file referenced by the
    release manifest when available, else the newest CurseForge file."""
    curseforge = [f for f in project.files if f.type == "curseforge"]
    if wanted_file_id is not None:
        hit = next((f for f in curseforge if int(f.id) == wanted_file_id), None)
        if hit is not None:
            return hit
    if curseforge:
        return max(curseforge, key=lambda f: f.date_published)
    raise LookupError(f"project {plan_lock_slug(project)} has no CurseForge provider record")


def plan_metadata(record: ScanRecord, local_retained: frozenset[str],
                  side_overrides: Mapping[str, str] | None = None,
                  ) -> tuple[list[Action], list[Note]]:
    """Diff existing metafiles against the release's metadata expectations.

    Identity model per artifact: (filename, sha1, side, cf ids, route) where
    route is one of "meta" (curseforge metadata download), "url" (direct url,
    no update block), "urlcf" (direct url + curseforge update block), and
    "mrurl" (modrinth direct url + modrinth update block). Byte-stability:
    identical identities are never rewritten regardless of cosmetics such as
    display name or formatting.
    """
    side_overrides = side_overrides or {}
    actions: list[Action] = []
    notes: list[Note] = []

    manifest_pairs = {(f.projectID, f.fileID) for f in record.manifest.files}
    pair_owner: dict[tuple[int, int], PakkuProject] = {}
    for project in record.lock.projects:
        cf_project = project.id.get("curseforge")
        if cf_project is None:
            continue
        for entry in project.files:
            if entry.type == "curseforge":
                pair_owner.setdefault((int(cf_project), int(entry.id)), project)

    release_filenames: set[str] = {
        Path(rel).name for rel in record.release_entries
        if rel.split("/")[0] in META_DIRS
    }

    existing_by_rel = dict(record.current_metas)
    claimed: set[str] = set()

    def occupant_for(cf_project: int, filename: str) -> str | None:
        hit = next((rel for rel, text in existing_by_rel.items()
                    if rel.endswith(".pw.toml")
                    and parse_metafile_text(text).get("cf_project_id") == cf_project), None)
        return hit or next((rel for rel, text in existing_by_rel.items()
                            if rel.endswith(".pw.toml")
                            and parse_metafile_text(text).get("filename") == filename), None)

    for project in record.lock.projects:
        slug = plan_lock_slug(project)
        target_dir = plan_target_dir(project)
        if target_dir is None:
            notes.append(Note("warn", slug, f"unhandled project type {project.type}"))
            continue

        cf_project_id = project.id.get("curseforge")
        covered_pairs: list[int] = []
        if cf_project_id is not None:
            covered_pairs = sorted({
                fid for pid, fid in manifest_pairs
                if pid == int(cf_project_id) and pair_owner.get((pid, fid)) is project
            })

        if not covered_pairs:
            if any(entry.file_name in release_filenames for entry in project.files):
                continue  # payload diff owns these files; no metadata entry
            notes.append(Note(
                "skip-lock-record", slug,
                f"in neither manifest nor raw assets ({project.files[0].file_name})",
            ))
            continue

        chosen = plan_provider(project, covered_pairs[-1])
        # Skew rule: CurseForge propagating slower than Modrinth within one
        # release; the official serverpack ships the newest artifact.
        newest = max(project.files, key=lambda f: f.date_published)
        skew = (chosen.type == "curseforge"
                and newest.type == "modrinth"
                and newest.file_name != chosen.file_name
                and newest.date_published > chosen.date_published)
        if skew:
            notes.append(Note(
                "info", slug,
                f"provider skew: manifest/cf {chosen.file_name} lags "
                f"{newest.file_name}; adopting newer",
            ))
            chosen = newest

        filename, sha1 = chosen.file_name, chosen.hashes.get("sha1")
        if sha1 is None:
            notes.append(Note("warn", slug, "chosen provider record lacks a sha1 hash"))
            continue

        mr_update = (str(chosen.parent_id), str(chosen.id)) \
            if chosen.type == "modrinth" else None
        # Id-pair conventions, fixed here once so every consumer agrees:
        #   * rendering pair  = (file_id, project_id)  [packwiz emission order]
        #   * identity pair   = (project_id, file_id)  [semantic order]
        # Only CurseForge-routed records carry integer pairs.
        if chosen.type == "curseforge":
            cf_render_pair = (str(int(chosen.id)), str(int(cf_project_id)))
            cf_identity_pair = (int(cf_project_id), int(chosen.id))
        else:
            cf_render_pair = None
            cf_identity_pair = None
        # Keep an already-correct existing path even when it differs from the
        # lock slug (historical names such as reliable-emi.pw.toml stay put).
        occupant = occupant_for(int(cf_project_id), filename) if cf_project_id else None
        target_rel = occupant or f"{target_dir}/{slug}.pw.toml"
        declared_side = side_overrides.get(slug) or plan_side(project)

        if occupant is None:
            fresh_ident = (-1, -1) if mr_update else cf_identity_pair
            new_ident = (filename, sha1, declared_side, fresh_ident,
                         "mrurl" if mr_update else "meta")
            old_ident = None
            body = plan_render_metafile(
                plan_display_name(project), filename, declared_side, sha1,
                download_url=chosen.url if mr_update else None,
                update_curseforge=None if mr_update else cf_render_pair,
                update_modrinth=mr_update,
            )
        else:
            prev = parse_metafile_text(existing_by_rel[occupant])
            historical_url = prev["url"] is not None and not mr_update
            had_cf_block = prev["cf_file_id"] is not None
            old_route = ("mrurl" if prev["mr_file_id"]
                         else "urlcf" if historical_url and had_cf_block
                         else "url" if historical_url else "meta")
            if mr_update:
                download_url = chosen.url
                emitted_cf, emitted_mr = None, mr_update
                route = "mrurl"
            else:
                download_url = str(prev["url"]) if historical_url else None
                emitted_cf = cf_render_pair if (historical_url and had_cf_block) \
                    or not historical_url else None
                emitted_mr = None
                route = old_route
            body = plan_render_metafile(
                plan_display_name(project), filename, declared_side, sha1,
                download_url=download_url,
                update_curseforge=emitted_cf,
                update_modrinth=emitted_mr,
            )
            ident_ids = ((-1, -1) if mr_update or not had_cf_block
                         else cf_identity_pair)
            new_ident = (filename, sha1, declared_side, ident_ids, route)
            old_ident = (prev["filename"], prev["hash"], prev["side"] or "both",
                         (int(prev["cf_project_id"] or -1),
                          int(prev["cf_file_id"] or -1)),
                         old_route)

        if target_rel in local_retained:
            notes.append(Note("info", target_rel, "kept under --local-retain"))
        elif old_ident is not None and new_ident == old_ident:
            actions.append(KeepMeta(target_rel))
        elif old_ident is not None:
            detail_bits = [f"{slug}: {old_ident[0]} -> {filename}"]
            if old_ident[2] != new_ident[2]:
                detail_bits.append(f"side {old_ident[2]}->{new_ident[2]}")
            if old_ident[4] != new_ident[4]:
                detail_bits.append(f"artifact {old_ident[4]}->{new_ident[4]}")
            actions.append(WriteMeta(target_rel, body, "; ".join(detail_bits)))
        else:
            actions.append(WriteMeta(target_rel, body, f"{slug}: added"))
        claimed.add(target_rel)

    # Leftover metafiles the new release no longer identifies.
    manifest_project_ids = {pid for pid, _ in manifest_pairs}
    for rel, text_of in sorted(existing_by_rel.items()):
        if rel in claimed:
            continue
        ident = parse_metafile_text(text_of)
        has_identity = int(ident["cf_project_id"] or -1) >= 0 \
            or bool(ident["mr_mod_id"])
        if has_identity and ident["mr_mod_id"] is None \
                and int(ident["cf_project_id"] or -1) not in manifest_project_ids:
            actions.append(DeleteMeta(rel, f"removed upstream ({ident['filename']})"))
        elif rel in local_retained:
            notes.append(Note("info", rel, "kept under --local-retain"))
        else:
            notes.append(Note(
                "warn", rel,
                f"unidentified metafile preserved (filename={ident['filename']})",
            ))

    return actions, notes


def plan_payloads(record: ScanRecord) -> tuple[list[Action], list[Note]]:
    actions: list[Action] = []
    notes: list[Note] = []

    with_paths: dict[str, str] = {}  # rel -> zip member path
    # Re-derive zip member paths from the standard layout; scan stored only digests.
    for rel in record.release_entries:
        with_paths[rel] = f"overrides/{rel}"

    incoming = set(with_paths)
    existing = set(record.current_payloads)
    for rel in sorted(incoming - existing):
        actions.append(WritePayload(rel, with_paths[rel], "add"))
    for rel in sorted(incoming & existing):
        if record.current_payloads[rel].digest != record.release_entries[rel].digest:
            actions.append(WritePayload(rel, with_paths[rel], "update"))
    for rel in sorted(existing - incoming):
        if rel in record.prev_override_rels:
            actions.append(DeletePayload(rel, "removed upstream"))
        else:
            notes.append(Note("warn", rel, "local-only payload preserved; review manually"))

    return actions, notes


def plan_version(record: ScanRecord, version: str) -> list[Action]:
    return [] if record.pack_version == version else [SetVersion(version)]


def build_plan(record: ScanRecord, version: str,
               local_retained: frozenset[str],
               side_overrides: Mapping[str, str]) -> ImportPlan:
    meta_actions, meta_notes = plan_metadata(record, local_retained, side_overrides)
    payload_actions, payload_notes = plan_payloads(record)
    notes = (*meta_notes, *payload_notes)
    if record.manifest.minecraft.version != record.lock.mc_versions[0]:
        notes.append(Note("warn", "versions",
                          f"manifest mc={record.manifest.minecraft.version} "
                          f"!= lock {record.lock.mc_versions}"))
    loaders = [loader.id for loader in record.manifest.minecraft.modLoaders]
    lock_loader = next((f"forge-{v}" for k, v in record.lock.loaders.items() if k == "forge"), None)
    if lock_loader and lock_loader not in loaders:
        notes.append(Note("warn", "loader",
                          f"lock pins {lock_loader} but manifest declares {loaders}"))
    actions = (
        *meta_actions,
        *payload_actions,
        *plan_version(record, version),
    )
    return ImportPlan(actions=tuple(actions), notes=notes)


# ---------------------------------------------------------------------------
# Apply boundary: the only phase allowed to mutate disk


def apply_plan(plan: ImportPlan, zip_path: Path, pack_dir: Path,
               packwiz: str) -> None:
    pack_root = pack_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for action in plan.actions:
            match action:
                case KeepMeta():
                    pass
                case WriteMeta(rel=rel, body=body):
                    target = pack_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body)
                case DeleteMeta(rel=rel):
                    (pack_dir / rel).unlink(missing_ok=True)
                case WritePayload(rel=rel, zip_path=member):
                    target = pack_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
                case DeletePayload(rel=rel):
                    target = pack_dir / rel
                    target.unlink(missing_ok=True)
                    parent = target.parent
                    while parent != pack_dir:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                case SetVersion(version=version):
                    pack_toml = pack_dir / "pack.toml"
                    text = pack_toml.read_text()
                    pack_toml.write_text(
                        re.sub(r'^version = ".*"$', f'version = "{version}"', text, count=1, flags=re.MULTILINE)
                    )
                case _:
                    raise RuntimeError(f"unhandled plan action: {action!r}")
        # Every written path must stay inside the pack tree.
        for action in plan.actions:
            rel = getattr(action, "rel", None)
            if rel is not None and isinstance(rel, str):
                resolved = (pack_dir / rel).resolve()
                if not resolved.is_relative_to(pack_root):
                    raise SystemExit(
                        f"FATAL: planned path escapes the pack directory: {rel!r}"
                    )
    if not plan.mutating:
        return
    subprocess.run([packwiz, "refresh"], cwd=pack_dir, check=True)


# ---------------------------------------------------------------------------
# Reporting (pure)


ACTION_ORDER = (KeepMeta, WriteMeta, DeleteMeta, WritePayload, DeletePayload, SetVersion)


def format_report(plan: ImportPlan) -> str:
    lines: list[str] = []

    counts: dict[type, int] = {}
    for action in plan.actions:
        counts[action.__class__] = counts.get(action.__class__, 0) + 1
    for cls in ACTION_ORDER:
        if counts.get(cls):
            lines.append(f"{cls.__name__}: {counts[cls]}")

    for group_name, pred in (
        ("writes", lambda a: isinstance(a, (WriteMeta, WritePayload))),
        ("deletes", lambda a: isinstance(a, (DeleteMeta, DeletePayload))),
    ):
        group = [a.rel for a in plan.actions if pred(a)]
        shown = ", ".join(sorted(group)[:60]) or "-"
        more = "" if len(group) <= 60 else f", … (+{len(group) - 60})"
        lines.append("")
        lines.append(f"== {group_name} ({len(group)}) ==")
        lines.append(shown + more)

    updates = sum(1 for a in plan.actions if isinstance(a, WritePayload) and a.reason == "update")
    adds = sum(1 for a in plan.actions if isinstance(a, WritePayload) and a.reason == "add")
    if adds or updates:
        lines.append("")
        lines.append(f"payload adds: {adds}, payload updates: {updates}")

    lines.append("")
    lines.append("== notes ==")
    if plan.notes:
        lines.extend(str(note) for note in plan.notes)
    else:
        lines.append("-")
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", required=True, type=Path)
    ap.add_argument("--lock", required=True, type=Path)
    ap.add_argument("--prev-zip", type=Path, help="previously imported release zip")
    ap.add_argument("--release-version", required=True)
    ap.add_argument("--expected-sha256")
    ap.add_argument("--local-retain", default="",
                    help="comma-separated metafile basenames to leave untouched "
                         "(local overlays, e.g. distanthorizons.pw.toml)")
    ap.add_argument("--pack", type=Path,
                    default=Path(__file__).resolve().parent.parent / "pack")
    ap.add_argument("--overrides",
                    default=str(Path(__file__).resolve().parent / "import-overrides.json"),
                    help="repo-owned side-correction mapping consumed at plan time")
    ap.add_argument("--packwiz", default="packwiz")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="exit nonzero unless the plan has no mutating actions")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    for required in (args.zip, args.lock):
        if not required.exists():
            raise SystemExit(f"FATAL: missing input {required}")
    if args.apply and args.expected_sha256 is None:
        raise SystemExit("FATAL: --apply requires --expected-sha256 (verify the release)")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    retained_names = frozenset(
        fragment.strip() for fragment in args.local_retain.split(",") if fragment.strip()
    )
    retained_rels = frozenset(
        f"{directory}/{name}" for name in retained_names for directory in META_DIRS
    )
    zip_path = args.zip.resolve()
    overrides_path = Path(args.overrides)
    side_overrides: dict[str, str] = (
        json.loads(overrides_path.read_text()).get("sides", {})
        if overrides_path.exists() else {}
    )

    print(f"scanning {zip_path.name} ...")
    record = scan(args.zip.resolve(), args.lock.resolve(),
                  args.prev_zip.resolve() if args.prev_zip else None,
                  args.pack.resolve(), args.expected_sha256)
    print(f"manifest: mc={record.manifest.minecraft.version} "
          f"loaders={[l.id for l in record.manifest.minecraft.modLoaders]} "
          f"pairs={len(record.manifest.files)}; lock projects={len(record.lock.projects)}")

    plan = build_plan(record, args.release_version, retained_rels, side_overrides)
    print(format_report(plan))

    if args.check:
        if plan.mutating:
            print("\nCHECK FAILED: plan contains mutating actions", file=sys.stderr)
            return 1
        print("\nCHECK PASSED: no mutating actions")
        return 0
    if args.apply:
        print("\napplying ...")
        apply_plan(plan, zip_path, args.pack.resolve(), args.packwiz)
        print("done; run `git status` to review.")
    else:
        print("\ndry run only; rerun with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
