#!/usr/bin/env python3
"""The addon index's pin bot. Standard library only.

    pin.py check             every addons/<id>.toml is well-formed and index.json is what they generate
    pin.py check --assets    also fetch every pinned zip: still the pinned bytes, still within the rules
    pin.py pin               pin each addon's newest release; index.json changes only when a pin does
    pin.py pin --offline     bring index.json up to the entries without the network (after a hand edit)

An entry (addons/<id>.toml) is what a person writes. A pin is what this script adds: the newest
release's tag, its zip's address, size and SHA-256, and the interface numbers of the zip's TOCs.
index.json holds both; the game's launcher installs exactly the pinned bytes and nothing else.

The rules are the launcher's (azs-update/src/addons.rs in the game's repository): what passes here
installs there. Change one side and the other follows.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tomllib
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

SCHEMA = 1
# The most a pinned zip may weigh, and the launcher's bounds on unpacking one.
ASSET_LIMIT = 64 << 20
MAX_FILES, MAX_BYTES, MAX_DEPTH = 5000, 256 << 20, 12
# The `## Interface` values the game loads without its "out of date" tick.
INTERFACES = (11200, 30300)
# File types no addon needs and Windows runs.
PROGRAMS = {"exe", "dll", "com", "scr", "pif", "msi", "bat", "cmd", "ps1", "vbs", "jar", "sh", "lnk", "hta"}
STATUSES = ("", "works", "partial", "broken", "untested")
# The categories the game's page has an icon and a translated name for.
CATEGORIES = ("Unit Frames", "Combat", "Loot", "Map", "Chat")
# A TOC is a few lines; one above this is no TOC.
TOC_LIMIT = 1 << 20

ENTRY_KEYS = {"id", "name", "author", "repo", "description", "category", "license", "folders", "requires", "source", "reborn"}
TEXTS = {"name": 80, "author": 80, "description": 400, "category": 40, "license": 40, "version": 40}
REBORN_TEXTS = {"tested": 40, "notes": 400}


class Refused(Exception):
    """An entry, a zip or an index that breaks a rule."""


def need(ok, message):
    if not ok:
        raise Refused(message)


def ascii_lower(text):
    """Lower case as Windows folds file names for our purposes: A to Z only, as the launcher does."""
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def is_control(char):
    return unicodedata.category(char) == "Cc"


# --- the launcher's rules -----------------------------------------------------------------------


def is_id(text):
    """An addon id: lower-case letters, digits and `-` inside, at most 64 characters."""
    return isinstance(text, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", text) is not None


def is_device(part):
    """A Windows device name, alone or with an extension, in any case: con, NUL.txt, com1."""
    stem = ascii_lower(part.split(".")[0].rstrip(" "))
    return re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", stem) is not None


def check_path(path):
    """A path that stays inside the install: relative, `/`-separated, no empty, `.` or `..` part,
    no drive or backslash, no name Windows rewrites or reserves, outside the launcher's state."""
    need(path and "\\" not in path and ":" not in path and not path.startswith("/"), f"{path!r} is not a relative /-path")
    parts = path.split("/")
    for part in parts:
        need(part not in ("", ".", ".."), f"{path!r} leaves its folder")
        need(
            not part.endswith((".", " ")) and not re.search(r"~[0-9]", part) and not is_device(part),
            f"{path!r} has a name Windows rewrites or reserves",
        )
    need(ascii_lower(parts[0].rstrip(". ")) != ".azs", f"{path!r} is inside the launcher's state")


def check_folder(name):
    """An addon folder: one plain name of letters, digits, `_`, `-`, `.` and `!`, no leading dot."""
    need(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_!-][A-Za-z0-9_.!-]{0,63}", name) is not None,
        f"folder {name!r} is not a plain name",
    )
    check_path(name)


def check_text(field, text, limit):
    need(
        isinstance(text, str) and len(text) <= limit and not any(is_control(c) or c == "|" for c in text),
        f"{field} is too long or holds a control character or |",
    )


def check_fields(addon):
    """What a person writes of an addon: the fields an entry and an index row share."""
    need(is_id(addon.get("id")), f"id {addon.get('id')!r} is not lower-case letters, digits and -")
    for field, limit in TEXTS.items():
        if field != "version":
            check_text(field, addon.get(field), limit)
    reborn = addon.get("reborn")
    need(isinstance(reborn, dict) and set(reborn) <= {"status", "tested", "notes"}, "reborn is not { status, tested, notes }")
    for field, limit in REBORN_TEXTS.items():
        check_text(f"reborn.{field}", reborn.get(field), limit)
    need(reborn.get("status") in STATUSES, f"reborn.status {reborn.get('status')!r} is not works, partial, broken or untested")
    need(addon["name"].strip() and addon["author"].strip(), "name and author must not be empty")
    repo = addon.get("repo")
    plain = r"(?!\.{1,2}$)[A-Za-z0-9_.-]+"
    need(isinstance(repo, str) and re.fullmatch(f"{plain}/{plain}", repo) is not None, f"repo {repo!r} is not owner/name")
    folders = addon.get("folders")
    need(isinstance(folders, list) and folders, "it lists no folder")
    for folder in folders:
        check_folder(folder)
    need(len({ascii_lower(f) for f in folders}) == len(folders), "it lists a folder twice")
    requires = addon.get("requires")
    need(isinstance(requires, list) and all(is_id(r) for r in requires), "requires is not a list of ids")


def check_pin(addon):
    """What this script adds to an index row: the pinned release."""
    check_text("version", addon.get("version"), TEXTS["version"])
    need(addon["version"].strip(), "version must not be empty")
    interface = addon.get("interface")
    need(
        isinstance(interface, list) and all(i in INTERFACES for i in interface),
        f"interface {interface!r} holds a value the game does not load",
    )
    url, releases = addon.get("asset_url"), f"https://github.com/{addon['repo']}/releases/download/"
    need(isinstance(url, str) and url.startswith(releases), f"asset_url is not under {releases}")
    parts = url[len(releases):].split("/")
    need(
        len(parts) >= 2
        and ascii_lower(parts[-1]).endswith(".zip")
        and all(re.fullmatch(r"(?!\.{1,2}$)[A-Za-z0-9_.+~%-]+", part) for part in parts),
        f"asset_url {url} is not <tag>/<file>.zip under the repository's releases",
    )
    size = addon.get("size")
    need(isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= ASSET_LIMIT, f"size {size!r} is not between 1 byte and {ASSET_LIMIT}")
    sha256 = addon.get("sha256")
    need(isinstance(sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is not None, "sha256 is not 64 hex digits")


def check_set(addons):
    """What a set of addons must hold together, entries or index rows: an id and a folder belong
    to one addon, and what an addon requires is another addon of the set, without a circle."""
    ids, folders = set(), set()
    for addon in addons:
        need(addon["id"] not in ids, f"{addon['id']} is listed twice")
        ids.add(addon["id"])
        for folder in addon["folders"]:
            need(ascii_lower(folder) not in folders, f"{addon['id']}: folder {folder} belongs to two addons")
            folders.add(ascii_lower(folder))
    by_id = {addon["id"]: addon for addon in addons}
    for addon in addons:
        for required in addon["requires"]:
            need(required in ids and required != addon["id"], f"{addon['id']} requires {required}, which is not another addon here")

    def visit(addon_id, path):
        need(addon_id not in path, f"{addon_id} requires itself through {', '.join(path)}")
        for required in by_id[addon_id]["requires"]:
            visit(required, path + [addon_id])

    for addon in addons:
        visit(addon["id"], [])


def check_index(index):
    """index.json as the launcher parses it."""
    need(isinstance(index, dict) and set(index) == {"schema", "serial", "addons"}, "index.json is not { schema, serial, addons }")
    need(type(index["schema"]) is int and index["schema"] >= 1, "index.json has no schema number")
    need(type(index["serial"]) is int and index["serial"] >= 0, "index.json has no serial")
    need(isinstance(index["addons"], list), "index.json lists no addons")
    for addon in index["addons"]:
        try:
            need(isinstance(addon, dict), "it is no object")
            check_fields(addon)
            check_pin(addon)
        except Refused as e:
            raise Refused(f"index.json, addon {addon.get('id') if isinstance(addon, dict) else '?'}: {e}") from None
    check_set(index["addons"])


# --- a release zip ------------------------------------------------------------------------------


def entry_name(info):
    """A zip entry's name as the launcher's zip reader decodes it: UTF-8 when the bytes are, CP437
    otherwise, whatever the entry's flag says, and not cut or rewritten as Python's reader does."""
    name = info.orig_filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except UnicodeError:
            pass
    return name


def toc_interface(content):
    """A TOC's `## Interface` as the game's version gate reads it (benilla-ui `toc.rs`): a line is
    trimmed, `## Key: Value` is a directive whatever the key's case, the last one of a key counts,
    and of the value the leading digits. 0 without one, which the game does not load."""
    value = 0
    for raw in content.decode("utf-8", "replace").removeprefix("﻿").split("\n"):
        line = raw.strip()
        if line.startswith("##") and ":" in line:
            key, _, rest = line[2:].partition(":")
            if ascii_lower(key.strip()) == "interface":
                digits = re.match(r"[0-9]*", rest.strip()).group()
                value = int(digits) if digits and int(digits) < 1 << 32 else 0
    return value


def check_zip(folders, data, max_files=MAX_FILES, max_bytes=MAX_BYTES, max_depth=MAX_DEPTH):
    """The launcher's rules for an addon's release zip, `data`, that is to hold exactly `folders`.
    Every entry lies under one of the folders by a safe path, is named once, is a plain file (no
    symbolic link, no encrypted entry, no program) stored or deflated, unpacks to the size it
    announces, and all stay within the bounds. Each folder has its `<Folder>/<Folder>.toc` with an
    interface number the game loads. Returns those numbers, sorted."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, NotImplementedError) as e:
        raise Refused(f"it is no zip: {e}") from None
    infos = archive.infolist()
    need(len(infos) <= max_files, f"it holds {len(infos)} entries, more than {max_files}")
    names = [info.orig_filename for info in infos]
    need(len(set(names)) == len(names), "it lists a name twice")
    tocs, seen, total = {}, set(), 0
    for info in infos:
        name = entry_name(info)
        is_dir = name.endswith(("/", "\\"))
        path = name[:-1] if name.endswith("/") else name
        check_path(path)
        need(not any(is_control(c) or c in '<>"|?*' for c in path), f"{name!r} holds a character Windows takes in no file name")
        parts = path.split("/")
        need(parts[0] in folders, f"{name} lies outside the folders ({', '.join(folders)})")
        need(len(parts) <= max_depth, f"{name} lies more than {max_depth} folders deep")
        # A Unix mode is in the upper half of the attributes when the entry was made on Unix.
        mode = info.external_attr >> 16 if info.create_system == 3 else 0
        need(mode & 0o120000 != 0o120000, f"{name} is a symbolic link")
        if is_dir:
            continue
        need(len(parts) >= 2, f"{name} is a file where a folder belongs")
        need(not info.flag_bits & 1, f"{name} is encrypted")
        need(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), f"{name} is packed with method {info.compress_type}, not stored or deflated")
        extension = ascii_lower(path.rsplit(".", 1)[1]) if "." in path else ""
        need(extension not in PROGRAMS, f"{name} is a program")
        need(ascii_lower(path) not in seen, f"it lists {name} twice")
        seen.add(ascii_lower(path))
        total += info.file_size
        need(total <= max_bytes, f"it unpacks to more than {max_bytes} bytes")
        # Unpack it as the launcher will: no further than the size it announces, its checksum right.
        try:
            with archive.open(info) as member:
                content = member.read(info.file_size + 1)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError, RuntimeError) as e:
            raise Refused(f"{name} does not unpack: {e}") from None
        need(len(content) == info.file_size, f"{name} unpacks to another size than the {info.file_size} bytes it announces")
        if len(parts) == 2 and parts[1] == f"{parts[0]}.toc":
            need(len(content) <= TOC_LIMIT, f"{name} is no TOC")
            tocs[parts[0]] = toc_interface(content)
            need(tocs[parts[0]] in INTERFACES, f"{name} has interface {tocs[parts[0]]}, which the game does not load")
    for folder in folders:
        need(folder in tocs, f"it lacks {folder}/{folder}.toc")
    return sorted(set(tocs.values()))


# --- entries and the index ----------------------------------------------------------------------


def load_entry(path):
    """One addons/<id>.toml, checked."""
    try:
        entry = tomllib.loads(path.read_text(encoding="utf-8"))
        need(set(entry) <= ENTRY_KEYS, f"unknown key {', '.join(sorted(set(entry) - ENTRY_KEYS))}")
        for key in ("description", "category", "license"):
            entry.setdefault(key, "")
        entry.setdefault("requires", [])
        reborn = entry.setdefault("reborn", {})
        need(isinstance(reborn, dict), "reborn is not a table")
        for key in ("status", "tested", "notes"):
            reborn.setdefault(key, "")
        check_fields(entry)
        need(path.name == f"{entry['id']}.toml", f"its file is not named {entry['id']}.toml")
        need(entry["category"] in CATEGORIES, f"category {entry['category']!r} is not one of {', '.join(CATEGORIES)}")
        source = entry.get("source")
        need(isinstance(source, dict) and set(source) == {"type", "asset"}, "source is not { type, asset }")
        need(source["type"] == "release", "source.type is not release")
        asset = source["asset"]
        need(
            isinstance(asset, str)
            and re.fullmatch(r"[A-Za-z0-9_.+*-]+", asset) is not None
            and asset.count("*") <= 1
            and ascii_lower(asset).endswith(".zip"),
            f"source.asset {asset!r} is not a .zip file name with at most one * for the version",
        )
        return entry
    except (Refused, tomllib.TOMLDecodeError, UnicodeError) as e:
        raise Refused(f"{path.name}: {e}") from None


def load_entries(root):
    """Every entry, by id, checked alone and together."""
    entries = [load_entry(path) for path in sorted((root / "addons").glob("*.toml"))]
    check_set(entries)
    return entries


def load_index(root):
    """index.json, or an empty index before the first pin."""
    path = root / "index.json"
    if not path.exists():
        return {"schema": SCHEMA, "serial": 0, "addons": []}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as e:
        raise Refused(f"index.json is not JSON: {e}") from None
    check_index(index)
    return index


def index_row(entry, pin):
    """An index row: the entry, and the pin's five fields."""
    return {
        "id": entry["id"],
        "name": entry["name"],
        "author": entry["author"],
        "repo": entry["repo"],
        "description": entry["description"],
        "category": entry["category"],
        "license": entry["license"],
        "folders": list(entry["folders"]),
        "requires": list(entry["requires"]),
        "interface": list(pin["interface"]),
        "version": pin["version"],
        "asset_url": pin["asset_url"],
        "size": pin["size"],
        "sha256": pin["sha256"],
        "reborn": {key: entry["reborn"][key] for key in ("status", "tested", "notes")},
    }


def write_index(root, index):
    """index.json, the same bytes on every machine: these are what gets signed."""
    text = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    (root / "index.json").write_bytes(text.encode("utf-8"))


def newest_release(entry, api):
    """The entry's newest release with an asset its `source.asset` names: (tag, the asset). A draft
    or a prerelease is no release; two assets of one release matching is an entry to fix."""
    pattern = re.compile(re.escape(entry["source"]["asset"]).replace(r"\*", ".*"))
    releases = api(f"https://api.github.com/repos/{entry['repo']}/releases?per_page=30")
    need(isinstance(releases, list), "GitHub lists no releases")
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        found = [asset for asset in release.get("assets", []) if pattern.fullmatch(asset["name"])]
        need(len(found) <= 1, f"release {release['tag_name']} has {len(found)} assets matching {entry['source']['asset']}")
        if found:
            return release["tag_name"], found[0]
    raise Refused(f"no release has an asset matching {entry['source']['asset']}")


def fetch_pinned(row, fetch):
    """The zip an index row pins, if GitHub still serves those bytes."""
    data = fetch(row["asset_url"], row["size"] + 1)
    need(
        len(data) == row["size"] and hashlib.sha256(data).hexdigest() == row["sha256"].lower(),
        f"source_changed: {row['asset_url']} is no longer the {row['size']} bytes with SHA-256 {row['sha256']}",
    )
    return data


def pin_entry(entry, old, api, fetch):
    """The index row of `entry` at its newest release. `old`, its row so far, is kept when that
    release is the pinned one: whether its bytes changed since is `check --assets`'s to say."""
    tag, asset = newest_release(entry, api)
    url, size = asset["browser_download_url"], asset["size"]
    if old is not None and (old["version"], old["asset_url"]) == (tag, url):
        return index_row(entry, old)
    need(type(size) is int and 1 <= size <= ASSET_LIMIT, f"{asset['name']} weighs {size} bytes, not between 1 and {ASSET_LIMIT}")
    data = fetch(url, size + 1)
    need(len(data) == size, f"{url} is not the {size} bytes GitHub lists")
    interface = check_zip(entry["folders"], data)
    row = index_row(entry, {"interface": interface, "version": tag, "asset_url": url, "size": size, "sha256": hashlib.sha256(data).hexdigest()})
    check_pin(row)
    return row


def pin(root, api, fetch, offline=False):
    """Bring index.json up to the entries and, unless `offline`, to each addon's newest release.
    An addon whose pin fails keeps the pin it has; the others go on. Returns what failed, per id.
    The serial grows by one when anything in the index changed, and only then is it written."""
    entries, index = load_entries(root), load_index(root)
    old = {row["id"]: row for row in index["addons"]}
    rows, failed = [], {}
    for entry in entries:
        before = old.get(entry["id"])
        try:
            row = pin_entry(entry, before, api, fetch) if not offline else None
        except (Refused, OSError, KeyError, TypeError, ValueError) as e:
            failed[entry["id"]] = str(e) or type(e).__name__
            row = None
        if row is None and before is not None:
            row = index_row(entry, before)
        if row is not None:
            rows.append(row)
    # An addon without a pin is not offered, and neither is one that requires it.
    while True:
        ids = {row["id"] for row in rows}
        kept = [row for row in rows if all(required in ids for required in row["requires"])]
        if len(kept) == len(rows):
            break
        for row in rows:
            if row not in kept:
                failed.setdefault(row["id"], "it requires an addon that has no pin")
        rows = kept
    if rows != index["addons"]:
        new = {"schema": SCHEMA, "serial": index["serial"] + 1, "addons": rows}
        check_index(new)
        write_index(root, new)
        print(f"index.json: serial {new['serial']}, {len(rows)} addons")
        for row in rows:
            before = old.get(row["id"])
            if before is None or before["sha256"] != row["sha256"]:
                print(f"  pinned {row['id']} {row['version']} ({row['size']} bytes, sha256 {row['sha256']})")
        for gone in sorted(set(old) - {row["id"] for row in rows}):
            print(f"  dropped {gone}")
    else:
        print(f"index.json is current: serial {index['serial']}, {len(rows)} addons")
    return failed


def check(root, fetch=None):
    """What is wrong with the entries and index.json, as a list of lines; with `fetch`, also with
    the pinned zips as GitHub serves them now."""
    try:
        entries, index = load_entries(root), load_index(root)
    except Refused as e:
        return [str(e)]
    by_id, problems = {entry["id"]: entry for entry in entries}, []
    for row in index["addons"]:
        entry = by_id.get(row["id"])
        if entry is None:
            problems.append(f"index.json lists {row['id']}, which has no entry: run tools/pin.py pin --offline")
        elif index_row(entry, row) != row:
            problems.append(f"index.json is not what addons/{row['id']}.toml says: run tools/pin.py pin --offline")
        elif fetch is not None:
            try:
                interface = check_zip(row["folders"], fetch_pinned(row, fetch))
                need(interface == row["interface"], f"its TOCs name interface {interface}, the index {row['interface']}")
            except (Refused, OSError) as e:
                problems.append(f"{row['id']} {row['version']}: {e}")
    return problems


# --- the network and the command line -----------------------------------------------------------

AGENT = "azs-addons-pin"


def http_api(url):
    """A GitHub API answer. The workflow's token goes to the API alone, for its rate limit."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": AGENT})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def http_fetch(url, limit):
    """At most `limit` bytes of a release asset, without the token: GitHub redirects to a file
    host, and a request's headers travel with the redirect."""
    need(url.startswith("https://github.com/"), f"{url} is not on GitHub")
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(limit)


def main(argv=None, api=http_api, fetch=http_fetch):
    parser = argparse.ArgumentParser(description="Validate and pin the addon index.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="the index repository")
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check", help="validate the entries and index.json")
    checking.add_argument("--assets", action="store_true", help="also fetch and check every pinned zip")
    pinning = commands.add_parser("pin", help="pin each addon's newest release")
    pinning.add_argument("--offline", action="store_true", help="only bring index.json up to the entries")
    args = parser.parse_args(argv)
    if args.command == "check":
        problems = check(args.root, fetch if args.assets else None)
        for line in problems:
            print(line, file=sys.stderr)
        if not problems:
            print("the entries and index.json are in order" + (", the pinned zips too" if args.assets else ""))
        return 1 if problems else 0
    try:
        failed = pin(args.root, api, fetch, args.offline)
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    for addon_id, why in failed.items():
        print(f"{addon_id}: {why}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
