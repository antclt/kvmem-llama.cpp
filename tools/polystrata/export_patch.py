#!/usr/bin/env python3
"""Mechanically export prepared backend edits without changing its real index."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
manifest_path = ROOT / "backends/versions.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
entry = manifest["polystrata"]
source = ROOT / entry["path"]
with tempfile.TemporaryDirectory(prefix="polystrata-patch-", dir=ROOT.parent) as temp:
    env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp)/"index"))
    def git(*args):
        return subprocess.run(["git","-c","core.autocrlf=false","-c","core.longpaths=true","-C",str(source),*args],
                              env=env,check=True,capture_output=True).stdout
    if git("rev-parse","HEAD").decode().strip() != entry["base_commit"]:
        raise RuntimeError("backend is not at its pinned upstream commit")
    git("read-tree",entry["base_commit"])
    git("add","-A")
    tree = git("write-tree").decode().strip()
    patch = git("diff","--binary",entry["base_commit"],tree)
    (ROOT/entry["patch"]).write_bytes(patch)
    git("read-tree",entry["base_commit"])
    git("apply","--cached","--whitespace=nowarn",str(ROOT/entry["patch"]))
    if git("write-tree").decode().strip() != tree:
        raise RuntimeError("patch does not reconstruct the prepared source tree")
    entry.update(patched_tree=tree,patch_sha256=hashlib.sha256(patch).hexdigest(),
                 local_changes="Opt-in Windows/CUDA qwen35moe F16/Q8 KVMem; bounded Query/replay, PageSoftmax/graph scoring and resident-only Pressure; shared process RAM/SSD and retained histories; per-sequence single-GPU native batches; one to three embedded MTP drafts during solo execution; CPU/GPU vision; pinned upstream base")
    manifest_path.write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8",newline="\n")
    print(json.dumps({"tree":tree,"patch_sha256":entry["patch_sha256"],"roundtrip":True}))
