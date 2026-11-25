#!/usr/bin/env python3
"""Split audiodata.js into a small manifest + folder-aligned track chunks.

The player understands both shapes: one whole `tracks` array (what mugal.py writes) and a
`chunks` manifest (what this script writes). Splitting is a pure post-process: nothing is
rescanned, so the display-repair table (fix), cover art and everything else already in
audiodata.js survive untouched.

Why it matters on a slow disk / network share: a 100k-track library is a ~17 MB file that the
player must read before it can show anything. With chunks (≈1.2 MB each, cut on folder
boundaries) the first screen only reads the manifest plus the chunk that holds the folder
being opened; the rest streams in afterwards, in the background.

The `fix` table is also mirrored to .mugal/display-fix.json, because mugal.py cannot rebuild
it: after a rescan, run this script again and the repairs come back automatically.

Usage
    python split-data.py [PLAYER_DIR]            # report only
    python split-data.py [PLAYER_DIR] --write    # write audiodata.js + audiodata.N.js
    python split-data.py --target 2000000 --write

PLAYER_DIR is the folder holding audiodata.js (default: current dir, then the parent of this
script, then this script's folder). The original one-file data is kept as audiodata-full.js,
so `mv audiodata-full.js audiodata.js` reverts the split.
"""
import argparse
import json
import os
import shutil
import sys

TARGET = 1_200_000                                  # bytes of JSON per chunk
MANIFEST = "audiodata.js"
FULL = "audiodata-full.js"
FIX_SIDECAR = os.path.join(".mugal", "display-fix.json")
DATA_KEY = "window.__AUDIO_DATA="
CHUNK_KEY = "window.__AUDIO_CHUNKS["


def player_dir(arg=None):
    if arg:
        return os.path.abspath(arg)
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.getcwd(), os.path.dirname(here), here):
        if os.path.exists(os.path.join(d, MANIFEST)):
            return d
    return os.getcwd()


def read_js_json(path):
    """Parse a file that assigns one JSON value to a name (`x={...}`) — object literal or JSON.parse."""
    with open(path, encoding="utf-8") as f:
        src = f.read().strip()
    if DATA_KEY in src:
        body = src[src.rindex(DATA_KEY) + len(DATA_KEY):]
    elif CHUNK_KEY in src:
        body = src[src.index("=", src.rindex(CHUNK_KEY)) + 1:]
    else:
        raise SystemExit(f"{path}: unknown layout")
    body = body.rstrip().rstrip(";")
    if body.startswith("JSON.parse("):
        body = json.loads(body[len("JSON.parse("):-1])       # escaped string → JSON text
    return json.loads(body)


def tracks_of(man, out):
    """Track rows: the array itself, or the chunk files of an already split manifest."""
    if man.get("tracks") is not None:
        return man["tracks"]
    rows = []
    for c in man["chunks"]:
        rows.extend(read_js_json(os.path.join(out, c["f"])))
    return rows


def plan_chunks(rows, target):
    """Cut on folder boundaries once a chunk would exceed `target` bytes."""
    seen, order, cur = set(), [], None
    for r in rows:
        fi = r[0]
        if fi != cur:
            if fi in seen:
                raise SystemExit(f"folder {fi} is not contiguous — mugal must sort tracks by folder")
            seen.add(fi); order.append(fi); cur = fi
    fcount = [0] * (max(order) + 1)
    for r in rows:
        fcount[r[0]] += 1
    sizes = [len(json.dumps(r, ensure_ascii=False, separators=(",", ":"))) + 1 for r in rows]
    plan, pos, start, size = [], 0, 0, 0
    for fi in order:
        c = fcount[fi]
        here = sum(sizes[pos:pos + c])
        if size and size + here > target:
            plan.append((start, pos)); start, size = pos, 0
        pos += c; size += here
    if pos > start:
        plan.append((start, pos))
    return plan, fcount


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("player", nargs="?", help="folder holding audiodata.js")
    ap.add_argument("--target", type=int, default=TARGET, help=f"bytes per chunk (default {TARGET:,})")
    ap.add_argument("--write", action="store_true", help="write the files (default: report only)")
    a = ap.parse_args(argv)

    out = player_dir(a.player)
    data_path = os.path.join(out, MANIFEST)
    if not os.path.exists(data_path):
        raise SystemExit(f"no {MANIFEST} in {out}")
    man_in = read_js_json(data_path)
    rows = tracks_of(man_in, out)
    folders = man_in["folders"]
    if man_in.get("count", len(rows)) != len(rows):
        raise SystemExit("count mismatch")
    if rows and max(r[0] for r in rows) >= len(folders):
        raise SystemExit("a track points at a folder that does not exist")

    plan, fcount = plan_chunks(rows, a.target)

    man = {k: v for k, v in man_in.items() if k != "tracks"}
    man["fcount"] = fcount
    man["chunks"] = [{"f": f"audiodata.{i}.js", "a": a0, "b": b0} for i, (a0, b0) in enumerate(plan)]

    side = os.path.join(out, FIX_SIDECAR)
    fix = dict(man_in.get("fix") or {})
    if fix:                                                   # carry repairs over to the next scan
        old = {}
        if os.path.exists(side):
            try:
                with open(side, encoding="utf-8") as f:
                    old = json.load(f)
            except (OSError, ValueError):
                old = {}
        old.update(fix)
        os.makedirs(os.path.dirname(side), exist_ok=True)
        with open(side, "w", encoding="utf-8", newline="\n") as f:
            json.dump(old, f, ensure_ascii=False, indent=0)
        fix = old
    elif os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            fix = json.load(f)
        print(f"restored {len(fix):,} display repairs from {FIX_SIDECAR}")
    if fix:
        man["fix"] = fix

    man_text = DATA_KEY + json.dumps(man, ensure_ascii=False, separators=(",", ":")) + ";\n"
    chunks = [f"window.__AUDIO_CHUNKS=window.__AUDIO_CHUNKS||[];window.__AUDIO_CHUNKS[{i}]="
              + json.dumps(rows[a0:b0], ensure_ascii=False, separators=(",", ":")) + ";\n"
              for i, (a0, b0) in enumerate(plan)]

    total = len(man_text.encode()) + sum(len(c.encode()) for c in chunks)
    print(f"{len(rows):,} tracks · {len(folders):,} folders · fix {len(fix):,}")
    print(f"manifest {len(man_text.encode())/1e6:.2f} MB + {len(plan)} chunks "
          f"(avg {sum(len(c.encode()) for c in chunks)/len(plan)/1e6:.2f} MB) = {total/1e6:.2f} MB")
    for i, (a0, b0) in enumerate(plan):
        print(f"  {i:>2}  tracks {a0:>7,}–{b0:>7,}  {len(chunks[i].encode())/1e6:.2f} MB  "
              f"folders {rows[a0][0]}..{rows[b0 - 1][0]}")

    if not a.write:
        print("report only — pass --write to write the files")
        return 0

    if man_in.get("tracks") is not None and not os.path.exists(os.path.join(out, FULL)):
        shutil.copyfile(data_path, os.path.join(out, FULL))
        print(f"kept the one-file data as {FULL}")
    with open(data_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(man_text)
    for i, text in enumerate(chunks):
        with open(os.path.join(out, f"audiodata.{i}.js"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    for i in range(len(chunks), 64):                          # drop chunks from an earlier, finer split
        stale = os.path.join(out, f"audiodata.{i}.js")
        if os.path.exists(stale):
            os.remove(stale)
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
