from bisect import bisect_right
import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


PATCH_PROTOCOL = "exact_unique_search_v1"


def _git(repo, *args, data=None, env=None):

    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, env=env,
    ).stdout


def _invalid(reason, locations):
    return {"valid": False, "reason": reason, "patch": "", "changed_files": [],
            "patch_protocol": PATCH_PROTOCOL, "locations": locations}


def _safe_path(repo, name):

    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    if any(ord(char) < 32 for char in name):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        return False
    if any(part.lower() == ".git" for part in path.parts):
        return False
    target = repo
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            return False
    return target.resolve().is_relative_to(repo)


def apply_candidate(repo: Path, edits: list[dict], allowed_files: list[str] | None = None,
                    required_groups: list[list[str]] | None = None) -> dict:

    repo = Path(repo).resolve()
    locations = []
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1", GIT_OPTIONAL_LOCKS="0")
    root = Path(_git(repo, "rev-parse", "--show-toplevel", env=env).decode().strip()).resolve()
    if repo != root:
        return _invalid("repository_root_required", locations)
    if not edits:
        return _invalid("empty_candidate", locations)
    allowed = None if allowed_files is None else set(allowed_files)
    originals, modes, spans = {}, {}, {}
    for index, edit in enumerate(edits, 1):
        name = edit.get("file")
        location = {"edit_index": index, "file": name, "source_sha256": None, "match_count": None,
                    "start_byte": None, "end_byte": None, "start_line": None, "end_line": None}
        locations.append(location)
        if "start_line" in edit or "end_line" in edit:
            return _invalid(f"unsupported_line_window:{name}", locations)
        if not _safe_path(repo, name):
            return _invalid(f"unsafe_path:{name}", locations)
        if allowed is not None and name not in allowed:
            return _invalid(f"outside_scope:{name}", locations)
        search, replace = edit.get("search"), edit.get("replace")
        if not isinstance(search, str) or not isinstance(replace, str):
            return _invalid(f"invalid_edit_text:{name}", locations)
        target = repo / name
        if any(parent.exists() and not parent.is_dir() for parent in target.parents if parent != repo):
            return _invalid(f"non_directory_parent:{name}", locations)
        if name not in originals:
            entry = _git(repo, "ls-tree", "-z", "HEAD", "--", name, env=env)
            if entry:
                mode, kind, oid = entry.split(b"\t", 1)[0].split()
                if kind != b"blob" or mode not in {b"100644", b"100755"}:
                    return _invalid(f"unsupported_file_mode:{name}", locations)
                base = _git(repo, "cat-file", "blob", oid.decode(), env=env)
                if not target.is_file() or target.read_bytes() != base:
                    return _invalid(f"modified_target:{name}", locations)
                originals[name], modes[name] = base, mode.decode()
            else:
                if target.exists():
                    return _invalid(f"untracked_target:{name}", locations)
                originals[name], modes[name] = None, "100644"
            spans[name] = []
        original = originals[name]
        if original is None:
            if edit.get("create") is not True or search != "" or not replace:
                return _invalid(f"invalid_create:{name}", locations)
            if spans[name]:
                return _invalid(f"duplicate_or_conflicting_create:{name}", locations)
            spans[name].append((0, 0, replace))
            continue
        if edit.get("create") or not search:
            return _invalid(f"invalid_existing_edit:{name}", locations)
        location["source_sha256"] = hashlib.sha256(original).hexdigest()
        source = original.decode("utf-8", errors="surrogateescape")
        if any(0xDC80 <= ord(char) <= 0xDCFF for char in source):
            return _invalid(f"non_utf8_file:{name}", locations)
        position = source.find(search)
        found, count = position, 0
        while found >= 0:
            count += 1
            found = source.find(search, found + 1)
        location["match_count"] = count
        if position < 0:
            return _invalid(f"search_not_found:{name}", locations)
        if count != 1:
            return _invalid(f"ambiguous_search:{name}", locations)
        line_starts = [0]
        for line in source.splitlines(keepends=True):
            line_starts.append(line_starts[-1] + len(line))
        start_byte = len(source[:position].encode("utf-8"))
        location.update(start_byte=start_byte, end_byte=start_byte + len(search.encode("utf-8")),
                        start_line=bisect_right(line_starts, position),
                        end_line=bisect_right(line_starts, position + len(search) - 1))
        if search == replace:
            return _invalid(f"no_op_edit:{name}", locations)
        span = (position, position + len(search), replace)
        if span in spans[name]:
            return _invalid(f"duplicate_edit:{name}", locations)
        if any(span[0] < previous[1] and previous[0] < span[1] for previous in spans[name]):
            return _invalid(f"overlapping_edits:{name}", locations)
        spans[name].append(span)

    contents = {}
    for name, changes in spans.items():
        source = "" if originals[name] is None else originals[name].decode("utf-8")
        for start, end, replace in sorted(changes, reverse=True):
            source = source[:start] + replace + source[end:]
        contents[name] = source.encode("utf-8")
        if contents[name] == originals[name]:
            return _invalid(f"unchanged_file:{name}", locations)
    changed = set(contents)
    for group in required_groups or []:
        members = set(group)
        if members & changed and not members <= changed:
            return _invalid("incomplete_edit_group:" + ",".join(sorted(members - changed)), locations)
    for name in changed:
        if any(parent.as_posix() in changed for parent in PurePosixPath(name).parents):
            return _invalid(f"file_directory_conflict:{name}", locations)

    original_objects = Path(_git(repo, "rev-parse", "--git-path", "objects", env=env).decode().strip())
    if not original_objects.is_absolute():
        original_objects = repo / original_objects
    with tempfile.TemporaryDirectory(prefix="causalgui-index-") as temporary:
        directory = Path(temporary)
        objects = directory / "objects"
        objects.mkdir()
        alternates = [str(original_objects.resolve())]
        if env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            alternates.append(env["GIT_ALTERNATE_OBJECT_DIRECTORIES"])
        isolated = dict(
            env, GIT_INDEX_FILE=str(directory / "index"),
            GIT_OBJECT_DIRECTORY=str(objects),
            GIT_ALTERNATE_OBJECT_DIRECTORIES=os.pathsep.join(alternates),
        )
        _git(repo, "read-tree", "HEAD", env=isolated)
        records = []
        for name in sorted(contents):
            oid = _git(repo, "hash-object", "-w", "--stdin", data=contents[name], env=isolated).strip()
            records.append(modes[name].encode() + b" " + oid + b"\t" + name.encode("utf-8") + b"\0")
        _git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=isolated)
        patch = _git(
            repo, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--binary", "--src-prefix=a/", "--dst-prefix=b/", "HEAD", "--", *sorted(contents),
            env=isolated,
        ).decode("utf-8")
    if not patch:
        return _invalid("empty_diff", locations)
    return {"valid": True, "reason": "ok", "patch": patch, "changed_files": sorted(contents),
            "patch_protocol": PATCH_PROTOCOL, "locations": locations}
