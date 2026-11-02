#!/usr/bin/env python3
"""Run the tests a change can reach, by the lines it changes.

    python3 tools/tests/reach.py check [--all] [--dry-run] [NEXTEST ARGS...]
    python3 tools/tests/reach.py map

`check` compares the working tree with the newest commit in its history
that has a map, and runs (through cargo-nextest, as the suite runs) every
test that could see the change:

- a test that ran a line the change removes or rewrites;
- a test that ran a line beside an insertion (the insertion is inside
  code it ran);
- a test new to the map, or whose own source changed;
- every test, when the change touches what line coverage cannot see: the
  assembly and C under c/, the build script, the manifests and lock, the
  cargo and nextest configuration, or a line of Rust outside any function
  (a constant, a type, a `use`, a module's attribute), since any test may
  depend on it through the compiler, which no reading of the text can
  bound (a constant feeding a constant; a type inferred). An attribute
  on a function selects the function's tests. A comment or a blank line
  is no change. Precision is bought only where it is sound: inside
  function bodies, from the lines tests ran.

A changed line that coverage instruments and no test ran is reported: no
test can catch a mistake there, which is a finding, and running every
test would not change that.

The selected tests run with coverage on, each in its own process, so the
run yields their new line sets; the other tests' sets are carried over
through the diff, each unchanged line to its new number. The result is
the map of the working tree, stored by its tree id (`git write-tree` of
the tree as `git add -A` would stage it), so committing that tree gives
a commit whose map exists. `map` builds a map for the working tree from
scratch, every test run once with coverage.

Maps live under tmp/tests-map/<tree id>.tsv: one line per test and file,
`test<TAB>file<TAB>lines`, and `#instrumented<TAB>file<TAB>lines` for
the lines coverage instruments in each file. A map is a record of one
tree on this machine (the tests another CPU runs through other lines).

Exit status: the tests'. The reasons for every selection are printed, so
a reader can check them against the diff. `--dry-run` prints the
selection and stops, for measuring the rule on a history.
"""
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAPS = os.environ.get("TESTS_MAP_DIR") or os.path.join(ROOT, "tmp", "tests-map")
TARGET = os.path.join(ROOT, "target", "tests-coverage")
# The cargo arguments the suite runs with (CONTRIBUTING: the library's and
# the integration tests, release), with the public features on, as the
# mutation check builds (tools/mutants/check.py): the tests behind them
# are in the map too, so a map serves the mutation check's narrowing.
SUITE = ["--release", "--lib", "--tests", "--features", "serde,zeroize"]
# Changes here reach code coverage cannot see: every test runs.
EVERYTHING = re.compile(r"^(c/|build\.rs$|Cargo\.toml$|Cargo\.lock$|\.cargo/|\.config/|src/.*\.(S|c|h)$)")
# Crates and suites of their own, which the library's tests never run:
# a change there is no change to them. (capi and test_vectors have their
# own suites, run by CONTRIBUTING's commands; clocks is a dev-dependency
# of the examples alone.)
APART = re.compile(r"^(clocks/|capi/|test_vectors/)")


def git(*args, text=True):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=text, check=True).stdout


def tree_id():
    """The working tree's tree id, as `git add -A` would stage it (a
    temporary index, so the real one is untouched)."""
    index = os.path.join(git("rev-parse", "--git-dir").strip(), "tests-select-index")
    index = index if os.path.isabs(index) else os.path.join(ROOT, index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    subprocess.run(["git", "-C", ROOT, "read-tree", "HEAD"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", ROOT, "add", "-A"], env=env, check=True, capture_output=True)
    tree = subprocess.run(["git", "-C", ROOT, "write-tree"], env=env, check=True, capture_output=True, text=True).stdout.strip()
    os.remove(index)
    return tree


def map_path(tree):
    return os.path.join(MAPS, tree + ".tsv")


def base_with_map():
    """The tree to compare with: the last map written here (tmp/tests-map/
    latest names its tree; a tree id serves git diff and git show as a
    commit does), else the newest commit in HEAD's history whose tree has
    a map. Its tree id, or None."""
    latest = os.path.join(MAPS, "latest")
    if os.path.exists(latest):
        tree = open(latest).read().strip()
        if os.path.exists(map_path(tree)) and subprocess.run(["git", "-C", ROOT, "cat-file", "-e", tree], capture_output=True).returncode == 0:
            return tree
    for tree in git("log", "--format=%T", "-n", "200").splitlines():
        if os.path.exists(map_path(tree)):
            return tree
    return None


def read_map(tree):
    tests, instrumented = {}, {}
    for line in open(map_path(tree)):
        test, path, lines = line.rstrip("\n").split("\t")
        lines = {int(n) for n in lines.split(",") if n}
        if test == "#instrumented":
            instrumented[path] = lines
        else:
            tests.setdefault(test, {})[path] = lines
    return tests, instrumented


def write_map(tree, tests, instrumented):
    os.makedirs(MAPS, exist_ok=True)
    open(os.path.join(MAPS, "latest"), "w").write(tree + "\n")
    with open(map_path(tree), "w") as f:
        for path in sorted(instrumented):
            f.write(f"#instrumented\t{path}\t{','.join(map(str, sorted(instrumented[path])))}\n")
        for test in sorted(tests):
            for path in sorted(tests[test]):
                f.write(f"{test}\t{path}\t{','.join(map(str, sorted(tests[test][path])))}\n")


def suites():
    """Every test the suite runs: {binary id: [test names]}, from nextest."""
    out = subprocess.run(["cargo", "nextest", "list", *SUITE, "--message-format", "json"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return {binary: sorted(suite["testcases"]) for binary, suite in json.loads(out)["rust-suites"].items()}


def llvm_tool(name):
    """llvm-profdata or llvm-cov: the toolchain's own (rustup component add
    llvm-tools), whose coverage format is the compiler's; else the newest
    of /usr/lib/llvm-*/bin, as bench-hashes check looks for them."""
    sysroot = subprocess.run(["rustc", "--print", "sysroot"], capture_output=True, text=True, check=True).stdout.strip()
    found = glob.glob(os.path.join(sysroot, "lib", "rustlib", "*", "bin", name))
    if not found:
        found = sorted(glob.glob(f"/usr/lib/llvm-*/bin/{name}"), key=lambda p: int(re.search(r"llvm-(\d+)", p).group(1)))[::-1]
    assert found, f"{name}: rustup component add llvm-tools"
    return found[0]


def coverage_run(selection, extra):
    """Run the selected tests ({binary: [names]}) through nextest with
    coverage on, each in its own process and profile; returns (the tests'
    exit status, {test: {file: lines}}, {file: instrumented lines})."""
    env = dict(os.environ, RUSTFLAGS="-C instrument-coverage", CARGO_TARGET_DIR=TARGET)
    profiles = os.path.join(TARGET, "profiles")
    os.makedirs(profiles, exist_ok=True)
    for old in glob.glob(os.path.join(profiles, "*.profraw")):
        os.remove(old)
    status = 0
    results = {}
    tests = [(binary, name) for binary, names in selection.items() for name in names]
    for k, (binary, name) in enumerate(tests, 1):
        raw = os.path.join(profiles, f"{k}.profraw")
        run = subprocess.run(["cargo", "nextest", "run", *SUITE, *extra, "-E", f"binary_id(={binary}) & test(={name})"], cwd=ROOT, env=dict(env, LLVM_PROFILE_FILE=raw), capture_output=True, text=True)
        status = status or run.returncode
        results[f"{binary} {name}"] = (raw, run)
        print(("PASS " if run.returncode == 0 else "FAIL ") + f"{binary} {name}", flush=True)
        if run.returncode:
            print(run.stderr[-2000:], flush=True)
    listing = subprocess.run(["cargo", "test", *SUITE, "--no-run", "--message-format=json"], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    objects = [m["executable"] for m in map(json.loads, listing.splitlines()) if m.get("reason") == "compiler-artifact" and m.get("executable")]
    profdata, llvm_cov = llvm_tool("llvm-profdata"), llvm_tool("llvm-cov")
    covered, instrumented = {}, {}
    for test, (raw, _) in results.items():
        if not os.path.exists(raw):
            covered[test] = {}
            continue
        merged = raw + ".profdata"
        subprocess.run([profdata, "merge", "-sparse", "-o", merged, raw], check=True, capture_output=True)
        command = [llvm_cov, "export", "--format=lcov", "--instr-profile", merged, objects[0]]
        for extra_object in objects[1:]:
            command += ["--object", extra_object]
        lcov = subprocess.run(command, capture_output=True, text=True, check=True).stdout
        files, current = {}, None
        for line in lcov.splitlines():
            if line.startswith("SF:"):
                path = os.path.relpath(line[3:], ROOT)
                current = path if not path.startswith("..") else None
            elif line.startswith("DA:") and current:
                number, count = line[3:].split(",")[:2]
                instrumented.setdefault(current, set()).add(int(number))
                if int(count) > 0:
                    files.setdefault(current, set()).add(int(number))
        covered[test] = files
    return status, covered, instrumented


def code_only(text):
    """Whether a line holds code coverage could have instrumented had it
    been in a function: anything but blank, a comment, the inside of a
    block comment (approximately: `/*` opens one until `*/`), a line of
    brackets and punctuation alone (`}`, `};`, `)`), or an attribute
    (`#[...]`; a `#[cfg` is EVERYTHING's business, see select)."""
    stripped = text.strip()
    if not stripped or stripped.startswith(("//", "*/", "* ", "/*", "#[", "#!")) or stripped == "*":
        return False
    return re.sub(r"[\s\]\[(){};,]", "", stripped) != ""


def attribute(text):
    return text.strip().startswith("#[")


def item_after(lines, start):
    """The first line of code at or after `start` (1-based) in `lines`, past
    attributes, doc comments and blanks: (its number, its text), or None."""
    for n in range(start, len(lines) + 1):
        text = lines[n - 1].strip()
        if text and not text.startswith(("#[", "#!", "//")):
            return n, text
    return None


def fn_lines_from(in_fn, start):
    """The body lines of the function whose `fn` line is `start`: the run
    of in-function lines beginning right after it."""
    body, n = set(), start + 1
    while n in in_fn:
        body.add(n)
        n += 1
    return body


def fn_body_lines(text, const_too=False):
    """The line numbers inside function bodies (from a `fn` item's opening
    brace to its close), by brace depth over the text with strings and
    comments blanked: a change there is a change to one function's code,
    which the tests that ran its instrumented lines beside it can see; a
    change outside (a constant, a type, a trait) may reach any test. A
    `const fn`'s body is left out: the compiler may evaluate it into a
    constant some other test reads without running a line of it (Sam
    Shackleton's control, October 23), so a change there is a change
    outside any function for this rule. With `const_too`, those lines
    come back as a second set."""
    blank = re.sub(r"//[^\n]*|/\*.*?\*/|\"(\\.|[^\"\\])*\"|'(\\.|[^'\\])'", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    inside, in_const, depth, pending, body_depths = set(), set(), 0, None, []
    # A function's qualifiers are the words since the last item ended (a
    # `;`, `{` or `}`), on this line or earlier ones: `const` among them
    # makes it a const fn, in whatever form (`const extern "C" fn`, with
    # the ABI string blanked above; `const` on one line and `fn` on the
    # next: Sam Shackleton's controls, October 24).
    header = ""
    for number, line in enumerate(blank.splitlines(), 1):
        m = re.search(r"\bfn\b", line)
        if m:
            pending = "const" if re.search(r"\bconst\b", header + line[: m.start()]) else "fn"
        header += line + "\n"
        for ch in line:
            if ch == "{":
                depth += 1
                if pending:
                    body_depths.append((depth, pending))
                    pending = None
            elif ch == "}":
                if body_depths and depth == body_depths[-1][0]:
                    body_depths.pop()
                depth -= 1
        if re.search(r"[;{}]", line):
            header = ""
        if ";" in line and pending and "{" not in line:
            pending = None  # a trait's method without a body
        if body_depths:
            (in_const if any(kind == "const" for _, kind in body_depths) else inside).add(number)
    return (inside, in_const) if const_too else inside


def hunks(base):
    """The diff of the working tree against `base`, per file: a list of
    (old start, old count, new start, new count, new lines' text)."""
    out = git("diff", "-U0", "--no-color", "--no-ext-diff", base)
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    files, path, old_path = {}, None, None
    for line in out.splitlines():
        if line.startswith("diff --git"):
            path = old_path = None
        elif line.startswith("--- a/"):
            old_path = line[6:]
        elif line.startswith("+++ /dev/null"):
            # A deleted file: its old lines all change.
            path = old_path
            files.setdefault(path, []).append((1, 10**9, 0, 0, []))
        elif line.startswith("+++ b/"):
            path = line[6:]
            files.setdefault(path, [])
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            old_start, old_count = int(m.group(1)), int(m.group(2) or 1)
            new_start, new_count = int(m.group(3)), int(m.group(4) or 1)
            files[path].append((old_start, old_count, new_start, new_count, []))
        elif line.startswith("+") and path and files[path]:
            files[path][-1][4].append(line[1:])
    for path in untracked:
        files[path] = [(0, 0, 1, 10**9, [l.rstrip("\n") for l in open(os.path.join(ROOT, path), errors="replace")] if os.path.isfile(os.path.join(ROOT, path)) else [])]
    return files


def translate(lines, file_hunks):
    """Old line numbers to new, through a file's hunks; a line a hunk
    removes is dropped."""
    out = set()
    for n in lines:
        shift, gone = 0, False
        for old_start, old_count, new_start, new_count, _ in file_hunks:
            if old_count and old_start <= n < old_start + old_count:
                gone = True
                break
            if n >= old_start + old_count:
                shift += new_count - old_count
        if not gone:
            out.add(n + shift)
    return out


def select(base_tests, instrumented, listed, diff):
    """The tests to run, each with its reason: {test: reason}, and whether
    every test runs (with the reason)."""
    chosen = {}
    everything = None
    for path, file_hunks in diff.items():
        if EVERYTHING.match(path):
            everything = f"{path} changed: coverage cannot see it"
            break
        if not path.endswith(".rs") or APART.match(path):
            # Documents and tools, and crates apart: no test here reads them
            # (the files tests read are in EVERYTHING).
            continue
        covered_by = {}
        for test, files in base_tests.items():
            for n in files.get(path, ()):
                covered_by.setdefault(n, []).append(test)
        old_text = old_text_of(path)
        in_fn, in_const = fn_body_lines("\n".join(old_text), const_too=True)
        for old_start, old_count, new_start, new_count, new_lines in file_hunks:
            for n in range(old_start, min(old_start + old_count, len(old_text) + 1)):
                text = old_text[n - 1]
                if n in in_const and code_only(text):
                    everything = f"{path}:{n} is in a const fn, which the compiler may evaluate for a test that runs none of its lines ({text.strip()[:50]})"
                    break
                if n in covered_by:
                    for t in covered_by[n]:
                        chosen.setdefault(t, f"ran {path}:{n}, which the change removes or rewrites")
                elif n in instrumented.get(path, ()):
                    print(f"unprotected: {path}:{n} changes and no test ran it", flush=True)
                elif attribute(text):
                    # An attribute changes what it sits on. On a function
                    # (a cfg that builds it or not, a test's attribute): the
                    # tests that ran the function. On anything else (a
                    # module, a type, a use): any test may feel it.
                    item = item_after(old_text, n + 1)
                    if item and re.search(r"\bfn\b", item[1]):
                        for m in fn_lines_from(in_fn, item[0]) | {item[0]}:
                            for t in covered_by.get(m, []):
                                chosen.setdefault(t, f"ran {path}:{m}, in the function under a changed attribute at {n}")
                    else:
                        everything = f"{path}:{n} is an attribute on {item[1][:40] if item else 'the end of the file'} ({text.strip()[:50]})"
                elif code_only(text) and n in in_fn:
                    # Inside a function, on a line coverage gave no counter
                    # (`loop {`, `else {`, a match arm's head): the tests
                    # that ran its neighbours ran it.
                    near = [t for m in range(n - 3, n + 4) for t in covered_by.get(m, [])]
                    for t in near:
                        chosen.setdefault(t, f"ran beside {path}:{n}, a changed line of a function coverage gave no counter")
                    if not near:
                        print(f"unprotected: {path}:{n} changes inside a function no test ran", flush=True)
                elif code_only(text):
                    # Outside any function, or in a const fn's body: a use, a
                    # constant, a type, a trait. Which tests depend on it is a question of the
                    # compiler's (a constant feeds a constant feeds a test,
                    # a type is inferred), and line coverage cannot answer
                    # it; names in the text are no closure (Sam Shackleton's
                    # controls, October 23). Every test runs.
                        everything = f"{path}:{n} is code outside any function ({text.strip()[:60]})"
            if any(attribute(t) for t in new_lines) and old_count == 0:
                # An attribute inserted: on the function that follows it (in
                # the insertion, or right after it in the old text), its
                # tests; on anything else, every test.
                item = item_after(new_lines, 1) or item_after(old_text, old_start + 1)
                if item and re.search(r"\bfn\b", item[1]):
                    if item[1] in new_lines:
                        pass  # a new function: reached only through changed lines elsewhere, or a new test
                    else:
                        for m in fn_lines_from(in_fn, item[0]) | {item[0]}:
                            for t in covered_by.get(m, []):
                                chosen.setdefault(t, f"ran {path}:{m}, in the function under an inserted attribute")
                else:
                    everything = f"{path}:{new_start} inserts an attribute on {item[1][:40] if item else 'the end of the file'}"
            if any(code_only(t) for t in new_lines):
                for n in (old_start, old_start + 1) if old_count == 0 else ():
                    for t in covered_by.get(n, []):
                        chosen.setdefault(t, f"ran {path}:{n}, beside an insertion")
                if old_count == 0 and not any(n in covered_by for n in (old_start, old_start + 1)):
                    # An insertion beside no test's lines: new top-level code,
                    # reachable only through a changed line elsewhere, or a
                    # new test (listed below), or a new constant or type.
                    if any(re.match(r"\s*(pub(\([^)]*\))?\s+)?(const|static|type|struct|enum|use|impl|trait|macro_rules)\b", t) or t.strip().startswith("#[cfg") for t in new_lines):
                        everything = f"{path}:{new_start} inserts code outside any test's lines ({[t for t in new_lines if code_only(t)][0].strip()[:60]})"
        if everything:
            break
    for test in listed:
        if test not in base_tests:
            chosen.setdefault(test, "new to the map")
    return chosen, everything


_BASE = {}


def base_commit():
    return _BASE["commit"]


def old_text_of(path):
    """A file's lines at the base tree, read once."""
    texts = _BASE.setdefault("texts", {})
    if path not in texts:
        texts[path] = git("show", f"{base_commit()}:{path}").splitlines() if path in base_paths() else []
    return texts[path]


def base_paths():
    if "paths" not in _BASE:
        _BASE["paths"] = set(git("ls-tree", "-r", "--name-only", _BASE["commit"]).splitlines())
    return _BASE["paths"]


def main():
    args = sys.argv[1:]
    assert args and args[0] in ("check", "map"), __doc__
    command, args = args[0], args[1:]
    listed_by_binary = suites()
    listed = [f"{b} {n}" for b, names in listed_by_binary.items() for n in names]
    tree = tree_id()
    if command == "map" or "--all" in args:
        extra = [a for a in args if a != "--all"]
        print(f"running every test with coverage: {len(listed)} tests", flush=True)
        status, covered, instrumented = coverage_run(listed_by_binary, extra)
        write_map(tree, covered, instrumented)
        print(f"map of tree {tree[:12]} written: {map_path(tree)}")
        sys.exit(status)
    base = base_with_map()
    assert base, "no commit in HEAD's history has a map: python3 tools/tests/reach.py map (every test once, with coverage)"
    _BASE["commit"] = base_tree = base
    base_tests, instrumented = read_map(base_tree)
    diff = hunks(_BASE["commit"])
    chosen, everything = select(base_tests, instrumented, listed, diff)
    if everything:
        print(f"every test runs: {everything}", flush=True)
        chosen = {t: everything for t in listed}
    for test, reason in sorted(chosen.items()):
        print(f"run  {test}: {reason}")
    print(f"{len(chosen)} of {len(listed)} tests, against tree {_BASE['commit'][:12]} ({len(diff)} files changed)", flush=True)
    if "--dry-run" in args:
        return
    if not chosen:
        write_map(tree, {t: {p: translate(l, diff.get(p, [])) for p, l in f.items()} for t, f in base_tests.items() if t in listed}, {p: translate(l, diff.get(p, [])) for p, l in instrumented.items()})
        return
    selection = {}
    for test in chosen:
        binary, name = test.split(" ", 1)
        selection.setdefault(binary, []).append(name)
    status, covered, new_instrumented = coverage_run(selection, args)
    # The new map: the run's sets for the tests that ran, the base's
    # carried through the diff for the rest.
    tests = {}
    for test, files in base_tests.items():
        if test in listed and test not in covered:
            tests[test] = {p: translate(l, diff.get(p, [])) for p, l in files.items()}
    tests.update(covered)
    carried = {p: translate(l, diff.get(p, [])) for p, l in instrumented.items()}
    for p, l in new_instrumented.items():
        carried[p] = l
    if status == 0:
        write_map(tree, tests, carried)
        print(f"map of tree {tree[:12]} written")
    sys.exit(status)


if __name__ == "__main__":
    main()
