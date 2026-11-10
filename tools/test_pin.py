#!/usr/bin/env python3
"""pin.py's tests: `python tools/test_pin.py`. No network: GitHub is a stand-in, and the zips are
built here, by hand where no zip tool would write them."""

import contextlib
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pin  # noqa: E402

TOC = b"## Interface: 11200\nCore.lua\n"


def file(name, data=b"x", deflated=False, announces=None, mode=0o100644, flags=0, method=None):
    """One entry of a hand-built zip: any name, a size the headers lie about, a link's mode."""
    return (name, data, deflated, announces, mode, flags, method)


def raw_zip(entries):
    """A zip of `entries`: a local header and the data per entry, the central directory, its end."""
    out, central = bytearray(), bytearray()
    for name, data, deflated, announces, mode, flags, method in entries:
        if deflated:
            packer = zlib.compressobj(6, zlib.DEFLATED, -15)
            packed = packer.compress(data) + packer.flush()
        else:
            packed = data
        method = method if method is not None else (8 if deflated else 0)
        size = announces if announces is not None else len(data)
        raw = name.encode("utf-8")
        shared = struct.pack("<5H3I2H", 20, flags, method, 0, 0x21, zlib.crc32(data), len(packed), size, len(raw), 0)
        offset = len(out)
        out += struct.pack("<I", 0x04034B50) + shared + raw + packed
        central += struct.pack("<IH", 0x02014B50, (3 << 8) | 20) + shared
        central += struct.pack("<3HII", 0, 0, 0, (mode << 16) & 0xFFFFFFFF, offset) + raw
    out += central + struct.pack("<I4H2IH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), len(out), 0)
    return bytes(out)


def addon_zip(*folders, text=b"local x = 1\n", toc=TOC):
    """A release zip as an author ships it: each folder with its TOC and one Lua file."""
    return raw_zip([entry for f in folders for entry in (file(f"{f}/{f}.toc", toc), file(f"{f}/Core.lua", text))])


ENTRY = """\
id = "{id}"
name = "Some Addon"
author = "someone"
repo = "someone/{id}"
description = "One line."
category = "Combat"
license = "MIT"
folders = {folders}
requires = {requires}

[source]
type = "release"
asset = "{asset}"

[reborn]
status = "untested"
"""


class Hub:
    """A stand-in for GitHub: each repository's releases, newest first, and the bytes behind each
    asset's address."""

    def __init__(self):
        self.releases, self.files, self.asked, self.fetched = {}, {}, [], []

    def release(self, repo, tag, assets, prerelease=False, draft=False):
        listed = []
        for name, data in assets.items():
            url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
            self.files[url] = data
            listed.append({"name": name, "size": len(data), "browser_download_url": url})
        self.releases.setdefault(repo, []).insert(0, {"tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": listed})

    def api(self, url):
        self.asked.append(url)
        repo = url.removeprefix("https://api.github.com/repos/").split("/releases")[0]
        return self.releases.get(repo, [])

    def fetch(self, url, limit):
        self.fetched.append(url)
        return self.files[url][:limit]


class Repo:
    """An index repository in a temporary folder."""

    def __init__(self, case):
        folder = tempfile.TemporaryDirectory()
        case.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "addons").mkdir()

    def entry(self, addon_id, folders, requires=(), asset=None, **changes):
        text = ENTRY.format(id=addon_id, folders=json.dumps(list(folders)), requires=json.dumps(list(requires)), asset=asset or "Addon-v*.zip")
        for old, new in changes.items():
            text = text.replace(old, new)
        (self.root / "addons" / f"{addon_id}.toml").write_text(text, encoding="utf-8")

    def index(self):
        return json.loads((self.root / "index.json").read_text(encoding="utf-8"))


class ZipRules(unittest.TestCase):
    def test_an_addons_zip_passes_and_names_its_interfaces(self):
        entries = [
            file("LICENSE", b"License text\n"),
            file("SomeAddon/SomeAddon.toc", TOC),
            file("SomeAddon/Core.lua", b"local x = 1\n" * 400, deflated=True),
            file("SomeAddon/Media/", b"", mode=0o040755),
            file("SomeAddon/Media/Icon.tga", bytes([1, 2, 3])),
            file("SomeAddon_Options/SomeAddon_Options.toc", b"## Interface: 30300\n"),
        ]
        self.assertEqual(pin.check_zip(["SomeAddon", "SomeAddon_Options"], raw_zip(entries)), [11200, 30300])
        # One a zip tool wrote, too.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Lib-1.0/Lib-1.0.toc", TOC)
            archive.writestr("Lib-1.0/lib.lua", "return 1\n")
        self.assertEqual(pin.check_zip(["Lib-1.0"], buffer.getvalue()), [11200])

    def test_what_the_launcher_refuses_is_refused_here(self):
        toc = file("SomeAddon/SomeAddon.toc", TOC)
        cases = {
            "a path that climbs out": [toc, file("SomeAddon/../../evil.lua")],
            "a path from the root": [toc, file("/evil.lua")],
            "a drive letter": [toc, file("C:/evil.lua")],
            "a backslash path": [toc, file("SomeAddon\\..\\..\\evil.lua")],
            "a file beside the folders": [toc, file("readme.txt")],
            "a root license symlink": [toc, file("LICENSE", b"SomeAddon", mode=0o120777)],
            "a root license directory": [toc, file("LICENSE/", mode=0o040755)],
            "a folder the entry does not list": [toc, file("Other/Other.toc", TOC)],
            "the launcher's state": [toc, file(".azs/installed.json")],
            "a device name": [toc, file("SomeAddon/nul.lua")],
            "a name Windows trims": [toc, file("SomeAddon/Core.lua.")],
            "a short name": [toc, file("SomeAddon/CORELU~1.lua")],
            "a name Windows cannot write": [toc, file("SomeAddon/a?.lua")],
            "a name with a NUL in it": [toc, file("SomeAddon/a.exe\0.lua")],
            "a symbolic link": [toc, file("SomeAddon/link", b"../../../WTF", mode=0o120777)],
            "a program": [toc, file("SomeAddon/Setup.EXE", b"MZ")],
            "a script": [toc, file("SomeAddon/tools/run.ps1")],
            "a file listed twice": [toc, file("SomeAddon/a.lua", b"1"), file("SomeAddon/a.lua", b"2")],
            "a file listed twice in two cases": [toc, file("SomeAddon/a.lua", b"1"), file("SomeAddon/A.LUA", b"2")],
            "a folder without its toc": [file("SomeAddon/Core.lua")],
            "a toc of another name": [file("SomeAddon/Other.toc", TOC)],
            "a file where a folder belongs": [toc, file("SomeAddon")],
            "a file too deep": [toc, file("SomeAddon/a/b/c/d/e/f/g/h/i/j/k/l.lua")],
            "an encrypted file": [toc, file("SomeAddon/secret.lua", flags=1)],
            "a packing the launcher does not read": [toc, file("SomeAddon/a.lua", method=12)],
            "a file larger than it announces": [toc, file("SomeAddon/bomb.lua", b"a" * (1 << 16), deflated=True, announces=10)],
            "a file smaller than it announces": [toc, file("SomeAddon/short.lua", b"abc", announces=10)],
            "an interface the game does not load": [file("SomeAddon/SomeAddon.toc", b"## Interface: 110002\n")],
            "a toc without an interface": [file("SomeAddon/SomeAddon.toc", b"Core.lua\n")],
        }
        for what, entries in cases.items():
            with self.subTest(what), self.assertRaises(pin.Refused):
                pin.check_zip(["SomeAddon"], raw_zip(entries))
        with self.assertRaises(pin.Refused, msg="a zip without one of the folders"):
            pin.check_zip(["SomeAddon", "SomeAddon_Options"], raw_zip([toc]))
        with self.assertRaises(pin.Refused, msg="bytes that are no zip"):
            pin.check_zip(["SomeAddon"], b"not a zip at all")

    def test_a_zip_past_the_bounds_is_refused(self):
        data = raw_zip([file("SomeAddon/SomeAddon.toc", TOC), file("SomeAddon/a.lua", bytes(600)), file("SomeAddon/b.lua", bytes(600))])
        for what, bounds in {"more files": {"max_files": 2}, "more bytes": {"max_bytes": 1000}, "deeper": {"max_depth": 1}}.items():
            with self.subTest(what), self.assertRaises(pin.Refused):
                pin.check_zip(["SomeAddon"], data, **bounds)
        self.assertEqual(pin.check_zip(["SomeAddon"], data, max_files=3, max_bytes=2000, max_depth=2), [11200])

    def test_a_toc_is_read_as_the_game_reads_it(self):
        cases = {
            b"## Interface: 11200\n": 11200,
            b"\xef\xbb\xbf## Interface: 30300\r\nCore.lua\r\n": 30300,
            b"##interface:30300": 30300,
            b"  ## INTERFACE :  11200  \n": 11200,
            b"## Interface: 11507, 11508\n": 11507,
            b"## Interface: 11200\n## Interface: 30300\n": 30300,
            b"## Title: x\nCore.lua\n": 0,
            b"## Interface: soon\n": 0,
            b"# Interface: 11200\n": 0,
            b"## Interface: 99999999999999999999\n": 0,
        }
        for content, number in cases.items():
            self.assertEqual(pin.toc_interface(content), number, content)

    def test_paths_and_names_follow_the_launchers_rules(self):
        for ok in ("A/a.lua", "Lib-1.0/x.lua", "!Compat/a.lua", "Data/console.lua", "com10", "A/patch-~.lua"):
            pin.check_path(ok)
        for bad in ("", "/x", "../x", "a/../../x", "a//b", "./a", "C:/x", "a\\b", ".azs/x", ".AZS/x", ".azs./x", "a/b.", "a/b ", "PROGRA~1/x", "CON", "a/nul.txt", "LPT9.x", "con .txt"):
            with self.subTest(bad), self.assertRaises(pin.Refused):
                pin.check_path(bad)
        for ok in ("SomeAddon", "SomeAddon_Options", "Lib-1.0", "!Compat"):
            pin.check_folder(ok)
        for bad in ("", "A/B", "..", ".hidden", "con", "Some Addon", "SomeAddon.", "x" * 65, "Ädd"):
            with self.subTest(bad), self.assertRaises(pin.Refused):
                pin.check_folder(bad)
        self.assertTrue(all(pin.is_id(i) for i in ("a", "survivor-ui", "lib2", "x" * 64)))
        self.assertFalse(any(pin.is_id(i) for i in ("", "A", "-a", "a-", "a_b", "../x", "x" * 65, 5)))


class Entries(unittest.TestCase):
    def test_an_entry_loads_with_its_defaults(self):
        repo = Repo(self)
        repo.entry("some-addon", ["SomeAddon"])
        (entry,) = pin.load_entries(repo.root)
        self.assertEqual((entry["id"], entry["folders"], entry["requires"]), ("some-addon", ["SomeAddon"], []))
        self.assertEqual(entry["reborn"], {"status": "untested", "tested": "", "notes": ""})

    def test_an_entry_that_breaks_a_rule_is_refused(self):
        changes = {
            "an unknown key": {'license = "MIT"': 'licence = "MIT"'},
            "an id that is not its file's name": {'id = "some-addon"': 'id = "other"'},
            "a colour code in the name": {"Some Addon": "|cffff0000Ours|r"},
            "an empty author": {'author = "someone"': 'author = " "'},
            "a repo that is no owner/name": {'repo = "someone/some-addon"': 'repo = "someone"'},
            "a repo that is an address": {'repo = "someone/some-addon"': 'repo = "https://github.com/someone/x"'},
            "another source than a release": {'type = "release"': 'type = "branch"'},
            "an asset that is no zip": {"Addon-v*.zip": "Addon-v*.exe"},
            "an asset with two stars": {"Addon-v*.zip": "Addon-*-v*.zip"},
            "an asset that is a path": {"Addon-v*.zip": "../Addon-v*.zip"},
            "a status of its own": {'status = "untested"': 'status = "great"'},
            "a description that is too long": {"One line.": "x" * 401},
            "text that is no TOML": {'license = "MIT"': "license = MIT"},
        }
        for what, change in changes.items():
            repo = Repo(self)
            repo.entry("some-addon", ["SomeAddon"], **change)
            with self.subTest(what), self.assertRaises(pin.Refused):
                pin.load_entries(repo.root)
        for what, folders in {"no folder": [], "a folder that is a path": ["A/B"], "a hidden folder": [".x"], "a folder twice": ["A", "a"]}.items():
            repo = Repo(self)
            repo.entry("some-addon", folders)
            with self.subTest(what), self.assertRaises(pin.Refused):
                pin.load_entries(repo.root)

    def test_entries_must_hold_together(self):
        cases = {
            "one folder for two addons": [("a", ["Shared"], []), ("b", ["shared"], [])],
            "a requirement that is no entry": [("a", ["A"], ["gone"])],
            "an addon that requires itself": [("a", ["A"], ["a"])],
            "a circle of requirements": [("a", ["A"], ["b"]), ("b", ["B"], ["c"]), ("c", ["C"], ["a"])],
        }
        for what, entries in cases.items():
            repo = Repo(self)
            for addon_id, folders, requires in entries:
                repo.entry(addon_id, folders, requires)
            with self.subTest(what), self.assertRaises(pin.Refused):
                pin.load_entries(repo.root)
        repo = Repo(self)
        for addon_id, folders, requires in [("skin", ["Skin"], ["ui", "lib"]), ("ui", ["UI"], ["lib"]), ("lib", ["Lib"], [])]:
            repo.entry(addon_id, folders, requires)
        self.assertEqual([e["id"] for e in pin.load_entries(repo.root)], ["lib", "skin", "ui"])


class Pinning(unittest.TestCase):
    def setUp(self):
        self.repo, self.hub = Repo(self), Hub()
        # What the script prints is for a person at the command line.
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)

    def pin(self, **options):
        return pin.pin(self.repo.root, self.hub.api, self.hub.fetch, **options)

    def test_the_newest_release_is_pinned_by_size_and_hash(self):
        self.repo.entry("some-addon", ["SomeAddon"])
        old, new = addon_zip("SomeAddon", text=b"one"), addon_zip("SomeAddon", text=b"two")
        self.hub.release("someone/some-addon", "v1.0", {"Addon-v1.0.zip": old, "Addon-Installer.zip": b"not this one"})
        self.assertEqual(self.pin(), {})
        index = self.repo.index()
        (row,) = index["addons"]
        self.assertEqual((index["schema"], index["serial"]), (1, 1))
        self.assertEqual(
            (row["version"], row["asset_url"], row["size"], row["sha256"], row["interface"]),
            ("v1.0", "https://github.com/someone/some-addon/releases/download/v1.0/Addon-v1.0.zip", len(old), hashlib.sha256(old).hexdigest(), [11200]),
        )
        self.assertEqual(list(row), ["id", "name", "author", "repo", "description", "category", "license", "folders", "requires", "interface", "version", "asset_url", "size", "sha256", "reborn"])
        self.assertEqual(pin.check(self.repo.root, self.hub.fetch), [])

        # Nothing new: the file stays as it is, to the byte, and the zip is not fetched again.
        before, fetched = (self.repo.root / "index.json").read_bytes(), len(self.hub.fetched)
        self.assertEqual(self.pin(), {})
        self.assertEqual(((self.repo.root / "index.json").read_bytes(), len(self.hub.fetched)), (before, fetched))
        self.assertTrue(before.endswith(b"}\n") and b"\r" not in before)

        # A prerelease and a draft are no releases; the next real one is pinned and the serial grows.
        self.hub.release("someone/some-addon", "v2.0", {"Addon-v2.0.zip": new})
        self.hub.release("someone/some-addon", "v3.0-beta", {"Addon-v3.0-beta.zip": b"x"}, prerelease=True)
        self.hub.release("someone/some-addon", "v3.0", {"Addon-v3.0.zip": b"x"}, draft=True)
        self.assertEqual(self.pin(), {})
        index = self.repo.index()
        self.assertEqual((index["serial"], index["addons"][0]["version"], index["addons"][0]["sha256"]), (2, "v2.0", hashlib.sha256(new).hexdigest()))

    def test_a_release_that_breaks_a_rule_keeps_the_pin_before_it(self):
        self.repo.entry("bad", ["Bad"])
        self.repo.entry("good", ["Good"])
        self.hub.release("someone/bad", "v1", {"Addon-v1.zip": addon_zip("Bad")})
        self.hub.release("someone/good", "v1", {"Addon-v1.zip": addon_zip("Good")})
        self.assertEqual(self.pin(), {})
        releases = {
            "a zip with a program": {"Addon-v2.zip": raw_zip([file("Bad/Bad.toc", TOC), file("Bad/run.exe", b"MZ")])},
            "a zip of other folders": {"Addon-v2.zip": addon_zip("Other")},
            "two assets the entry names": {"Addon-v2.zip": addon_zip("Bad"), "Addon-v2-classic.zip": addon_zip("Bad")},
            "a zip larger than GitHub lists": None,
        }
        for number, (what, assets) in enumerate(releases.items(), 2):
            with self.subTest(what):
                tag = f"v{number}"
                if assets is None:
                    self.hub.release("someone/bad", tag, {f"Addon-{tag}.zip": addon_zip("Bad")})
                    self.hub.releases["someone/bad"][0]["assets"][0]["size"] -= 1
                else:
                    self.hub.release("someone/bad", tag, {name.replace("v2", tag): data for name, data in assets.items()})
                self.hub.release("someone/good", tag, {f"Addon-{tag}.zip": addon_zip("Good", text=tag.encode())})
                failed = self.pin()
                self.assertEqual(list(failed), ["bad"], failed)
                rows = {row["id"]: row["version"] for row in self.repo.index()["addons"]}
                self.assertEqual(rows, {"bad": "v1", "good": tag}, "the other addon is pinned all the same")
        self.assertEqual(pin.check(self.repo.root, self.hub.fetch), [])

    def test_an_addon_without_a_pin_is_not_offered_and_neither_is_one_that_requires_it(self):
        self.repo.entry("lib", ["Lib"])
        self.repo.entry("skin", ["Skin"], requires=["lib"])
        self.repo.entry("alone", ["Alone"])
        self.hub.release("someone/skin", "v1", {"Addon-v1.zip": addon_zip("Skin")})
        self.hub.release("someone/alone", "v1", {"Addon-v1.zip": addon_zip("Alone")})
        failed = self.pin()
        self.assertEqual(sorted(failed), ["lib", "skin"])
        self.assertEqual([row["id"] for row in self.repo.index()["addons"]], ["alone"])
        self.hub.release("someone/lib", "v1", {"Addon-v1.zip": addon_zip("Lib")})
        self.assertEqual(self.pin(), {})
        self.assertEqual([row["id"] for row in self.repo.index()["addons"]], ["alone", "lib", "skin"])
        pin.check_index(self.repo.index())
        # An entry that is taken out leaves the index, and with it what requires it.
        (self.repo.root / "addons" / "lib.toml").unlink()
        with self.assertRaises(pin.Refused, msg="skin still requires it"):
            self.pin()
        (self.repo.root / "addons" / "skin.toml").unlink()
        self.assertEqual(self.pin(), {})
        self.assertEqual(([row["id"] for row in self.repo.index()["addons"]], self.repo.index()["serial"]), (["alone"], 3))

    def test_an_edited_entry_reaches_the_index_without_the_network(self):
        self.repo.entry("some-addon", ["SomeAddon"])
        self.hub.release("someone/some-addon", "v1", {"Addon-v1.zip": addon_zip("SomeAddon")})
        self.assertEqual(self.pin(), {})
        self.repo.entry("some-addon", ["SomeAddon"], **{"One line.": "Two lines.", 'status = "untested"': 'status = "works"\ntested = "0.4.11"'})
        (problem,) = pin.check(self.repo.root)
        self.assertIn("pin --offline", problem)
        asked = (len(self.hub.asked), len(self.hub.fetched))
        self.assertEqual(self.pin(offline=True), {})
        self.assertEqual((len(self.hub.asked), len(self.hub.fetched)), asked, "the network was asked")
        index = self.repo.index()
        row = index["addons"][0]
        self.assertEqual((index["serial"], row["description"], row["reborn"]), (2, "Two lines.", {"status": "works", "tested": "0.4.11", "notes": ""}))
        self.assertEqual(pin.check(self.repo.root), [])

    def test_a_pinned_zip_that_changed_is_reported(self):
        self.repo.entry("some-addon", ["SomeAddon"])
        self.hub.release("someone/some-addon", "v1", {"Addon-v1.zip": addon_zip("SomeAddon")})
        self.assertEqual(self.pin(), {})
        url = self.repo.index()["addons"][0]["asset_url"]
        self.hub.files[url] = addon_zip("SomeAddon", text=b"something else")
        (problem,) = pin.check(self.repo.root, self.hub.fetch)
        self.assertIn("source_changed", problem)
        self.assertEqual(pin.check(self.repo.root), [], "without the zips the index is in order")
        # The pin stays: an author who replaces a release's file does not move it.
        self.assertEqual(self.pin(), {})
        self.assertEqual(self.repo.index()["serial"], 1)

    def test_an_index_edited_by_hand_is_reported(self):
        self.repo.entry("some-addon", ["SomeAddon"])
        self.hub.release("someone/some-addon", "v1", {"Addon-v1.zip": addon_zip("SomeAddon")})
        self.assertEqual(self.pin(), {})
        index = self.repo.index()
        for what, change in {
            "another address": {"asset_url": "https://example.com/someone/some-addon/releases/download/v1/Addon-v1.zip"},
            "another repository's zip": {"asset_url": "https://github.com/else/thing/releases/download/v1/Addon-v1.zip"},
            "a short hash": {"sha256": "ab" * 31},
            "a size past the limit": {"size": pin.ASSET_LIMIT + 1},
            "another name than the entry's": {"name": "Other"},
        }.items():
            edited = json.loads(json.dumps(index))
            edited["addons"][0].update(change)
            pin.write_index(self.repo.root, edited)
            with self.subTest(what):
                self.assertEqual(len(pin.check(self.repo.root)), 1)
        pin.write_index(self.repo.root, index)
        self.assertEqual(pin.check(self.repo.root), [])

    def test_the_command_line_says_whether_all_is_in_order(self):
        self.repo.entry("some-addon", ["SomeAddon"])
        self.hub.release("someone/some-addon", "v1", {"Addon-v1.zip": addon_zip("SomeAddon")})
        run = lambda *words: pin.main(["--root", str(self.repo.root), *words], api=self.hub.api, fetch=self.hub.fetch)  # noqa: E731
        self.assertEqual((run("pin"), run("check"), run("check", "--assets"), run("pin", "--offline")), (0, 0, 0, 0))
        self.hub.release("someone/some-addon", "v2", {"Addon-v2.zip": b"no zip"})
        self.assertEqual(run("pin"), 1)
        self.repo.entry("some-addon", [])
        self.assertEqual((run("check"), run("pin")), (1, 1))


class Seed(unittest.TestCase):
    def test_this_repositorys_own_entries_and_index_are_in_order(self):
        self.assertEqual(pin.check(Path(__file__).resolve().parent.parent), [])


if __name__ == "__main__":
    unittest.main()
