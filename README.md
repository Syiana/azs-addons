# Azeroth Survivors Reborn: the addon index

The list of third-party addons that the game's AddOns window offers to install. It holds no addon:
every addon stays in its author's repository. This repository only says which release of each one
the game may install, and pins that release's zip by size and SHA-256.

```text
addons/<id>.toml      one entry per addon, written by a person, changed by pull request
index.json            generated: the entries plus each addon's pinned release
tools/pin.py          checks the entries and the zips, writes index.json (Python, standard library)
tools/test_pin.py     its tests
.github/workflows/    check (pull requests), pin (daily), drift (nightly)
```

## From an author's release to a player's game

1. An addon has an entry here (`addons/<id>.toml`).
2. Once a day the **pin** workflow looks for a newer release of each addon. It fetches the zip,
   checks it against the rules below and opens a pull request that changes `index.json`: the new
   tag, the zip's address, its size and its SHA-256.
3. A maintainer reviews and merges that pull request.
4. The game's maintainers sign the merged `index.json` with the game's release key and publish it
   beside the game's updates. The key is not in this repository and not in CI; nothing here can
   publish by itself.
5. A player's launcher takes the signed index, and for an install fetches the pinned zip from the
   author's GitHub release. It installs only a file of exactly the pinned size and SHA-256.

So a release reaches players only through a merged pull request and a signature, and an author who
replaces a release's file changes nothing for players: the launcher refuses the new bytes, and the
**drift** workflow opens an issue here.

## The review

The checks are structural. They say that a zip unpacks into its own folders and holds no program;
they do not say that an addon is harmless. An addon is code that runs in the player's game: it can
read chat and send chat as the player.

**Before merging a pin, read what changed in the author's repository between the old tag and the
new one.** Merging is the review. To take a pin back, revert its pull request.

## Adding an addon

Open a pull request with one new file, `addons/<id>.toml`:

```toml
id = "survivor-ui"                  # lower-case letters, digits and -; never changes afterwards
name = "Survivor UI"
author = "morderous"
repo = "morderous/survivor-ui"      # the GitHub repository whose releases hold the zip
description = "One line on what it does."
category = "Unit Frames"            # Unit Frames, Combat, Loot, Map or Chat
license = "GPL-3.0"
folders = ["SurvivorUI", "SurvivorPlates"]   # every folder the zip puts into Interface\AddOns
requires = []                                # ids of addons that are installed first

[source]
type = "release"
asset = "SurvivorUI-v*.zip"         # the release asset's name; * stands for the version

[reborn]                            # how it runs on this game; filled in after a test
status = "untested"                 # works, partial, broken or untested
tested = ""                         # the game version it was tried on
notes = ""
```

Two optional lines go beside `name` for the game's page:

```toml
title = "|cff33ff99Survivor|r UI"   # the name in your colours, as your TOC's ## Title
icon = "INV_Misc_Book_09"           # one of the game's icons, its file name in Interface\Icons
```

- `title` is `name` with the game's colour codes, `|c` and eight hex digits (`AARRGGBB`) and
  `|r`, and nothing else: no texture, no link, no other words. The search goes by `name`.
- `icon` is the file name of one of the game's own icons without its folder or extension, as
  Wowhead shows it on an item's or spell's page. The page cannot show a picture from your zip:
  it is not on the player's disk before the install. Without `icon`, and when the game has no
  icon of that name, the page shows the category's.

A game older than 0.6.5 shows the plain name and the category's icon.

The entry alone offers nothing: the addon appears in the game once the pin workflow has pinned a
release and that pull request is merged and signed. The author is asked first; we link to their
work, we do not ship it.

After editing an entry by hand (a description, the `reborn` block), run
`python tools/pin.py pin --offline` and commit the `index.json` it writes; the check fails
otherwise.

## What a release zip must be

The same rules as the game's launcher, so that whatever is pinned here also installs:

- At the top, the entry's `folders` and optionally a `LICENSE` file. Each folder must have its
  `<Folder>/<Folder>.toc`. The launcher does not extract the root license.
- Each of those TOCs names `## Interface` `11200` or `30300`, the two numbers the game loads
  without its "out of date" tick.
- No path that leaves its folder (`..`, a leading `/`, a drive letter, a backslash), no name
  Windows rewrites, reserves or cannot write, no name listed twice, in any case.
- No symbolic link, no encrypted file, no packing other than stored or deflated.
- No program: `.exe .dll .com .scr .pif .msi .bat .cmd .ps1 .vbs .jar .sh .lnk .hta`.
- At most 5,000 entries, 256 MiB unpacked, 12 folders deep; the zip itself at most 64 MiB.
- Every file unpacks to exactly the size it announces.
- Folder names are letters, digits, `_`, `-`, `.` and `!`, at most 64 characters, no leading dot.
- Exactly one asset of the release matches `source.asset`. A draft or a prerelease is not pinned.

## Running it

Python 3.11 or newer, nothing to install.

```bash
python tools/test_pin.py
```

```bash
python tools/pin.py check
```

```bash
python tools/pin.py check --assets
```

```bash
python tools/pin.py pin
```

`check` reads only this repository. `check --assets` also fetches every pinned zip; `pin` asks
GitHub for the releases (set `GITHUB_TOKEN` to raise the API's rate limit; the token is sent to
the API only, never to a download).

## Setting the repository up

- Settings > Actions > General > "Allow GitHub Actions to create and approve pull requests": the
  pin workflow opens its pull requests with the workflow's own token.
- A pull request opened with that token does not start the check workflow. The pin script has
  checked each zip before pinning it; to see the check run on such a pull request, close and reopen
  it.
- Protect the default branch, so that `index.json` changes only through a reviewed pull request.

## Publishing (the game's maintainers)

After a pin is merged, from the game's repository:

```bash
azs-release addons-sign --index index.json --store <the update store> --key <the release key>
```

It refuses an index the launcher would refuse and one whose serial does not rise, writes
`addons/index.json` and `addons/index.json.sig` into the store, and those two files are uploaded
with the store.
