"""How much of the code each assurance technique reaches, approximately.

    python3 tools/qa_coverage.py [--out DIR]

A mechanical inventory, for weighing the techniques against each other,
not a verdict on any of them. It counts lines of code (blank and comment
lines left out) in the library (src/, the C API's capi/src/) and its
assembly (c/), splits the Rust into unsafe code (inside an `unsafe`
block, function, or impl) and safe code, and asks of each technique
which of those lines it exercises on this machine:

- the test suites, the guard-page tests, and the official test vectors: LLVM source coverage (-C instrument-coverage,
  nightly) of each run, the lines it executed;
- AddressSanitizer: the lines the test suites execute, assembly excepted
  (it instruments compiled Rust and C, never assembly);
- ThreadSanitizer: the lines the test suites execute in the files that
  share memory between threads (atomics, the pool, the stream);
- mutation testing: the lines of the functions cargo-mutants plants a
  bug in (`cargo mutants --list` over the files tools/mutants names),
  with the share of their mutants caught on AArch64 (the list of known
  survivors);
- the proofs: the assembly files tools/verify proves (AArch64), less the
  one kernel the proofs leave out;
- the startup self-test: the assembly entry points it runs (every kernel
  this machine has, a test asserts) and its own code.

A line "reached" is a line run at least once (or, for mutation testing,
inside a function with a planted bug); how thoroughly a technique checks
a line it reaches differs between techniques, and the table says nothing
of it. Code compiled out on this machine (another CPU's kernels) is
counted, and reached by no measured run here; the proofs and mutation
testing reach AArch64 code statically.

Needs the nightly toolchain with llvm-tools, and cargo-mutants.
"""

import collections
import glob
import json
import os
import platform
import re
import subprocess
import sys

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
NIGHTLY = "nightly-2026-09-17"
OUT = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(ROOT, "target", "qa-coverage")
# Files whose code threads share memory in: TSan's reach.
SHARED = {"src/lanes.rs", "src/stream.rs", "src/each.rs", "src/self_test.rs"}
# The assembly the proofs cover (tools/verify/README.md), and the kernel
# they leave out: the README's "every assembly kernel on 64-bit Arm but one".
PROVED = {"c/blake3_neon_hybrid_aarch64.S", "c/blake3_sme2_aarch64.S", "c/blake3_sme2_hybrid_aarch64.S"}


def code_lines(path):
    """{line number: is unsafe} for each line of code in a Rust, C or assembly file."""
    text = open(os.path.join(ROOT, path), encoding="utf-8").read().split("\n")
    rust = path.endswith(".rs")
    lines, depth, unsafe_depths, in_block_comment = {}, 0, [], False
    for number, line in enumerate(text, 1):
        stripped = line.strip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        # Blank, comment, attribute, and preprocessor lines are no code.
        if not stripped or stripped.startswith(("//", ";", "#")):
            continue
        # Strings and character literals hold no braces that count.
        bare = re.sub(r'"(\\.|[^"\\])*"' + r"|'(\\.|[^'\\])'", '""', line) if rust else line
        opens_unsafe = rust and re.search(r"\bunsafe\b[^;]*\{", bare) is not None
        lines[number] = bool(unsafe_depths) or opens_unsafe
        if rust:
            for i, ch in enumerate(bare):
                if ch == "{":
                    depth += 1
                    if opens_unsafe and not unsafe_depths:
                        unsafe_depths.append(depth)
                elif ch == "}":
                    if unsafe_depths and depth == unsafe_depths[-1]:
                        unsafe_depths.pop()
                    depth -= 1
    return lines


def inventory():
    files = sorted(glob.glob("src/*.rs", root_dir=ROOT) + glob.glob("capi/src/*.rs", root_dir=ROOT))
    files = [f for f in files if not f.endswith(("/test.rs",))]
    asm = sorted(glob.glob("c/*.S", root_dir=ROOT) + glob.glob("c/*.c", root_dir=ROOT))
    return {f: code_lines(f) for f in files + asm}


def coverage(name, args, filters=(), env_extra=None):
    """The lines a test run executes (cargo-nextest, each test in its own
    process, each writing its own profile): {file: set(lines)}. `args` are
    cargo's, `filters` the tests' names to run."""
    target = os.path.join(OUT, "target-" + name)
    profiles = os.path.join(OUT, "profiles-" + name)
    os.makedirs(profiles, exist_ok=True)
    for old in glob.glob(os.path.join(profiles, "*.profraw")):
        os.remove(old)
    env = dict(os.environ, RUSTFLAGS="-C instrument-coverage", CARGO_TARGET_DIR=target, LLVM_PROFILE_FILE=os.path.join(profiles, "%p-%m.profraw"), **(env_extra or {}))
    run = subprocess.run(["cargo", "+" + NIGHTLY, "nextest", "run", "--release"] + args + list(filters), cwd=ROOT, env=env, capture_output=True, text=True)
    if run.returncode != 0:
        sys.exit(f"qa_coverage: {name}: the tests failed\n{run.stderr[-3000:]}")
    sysroot = subprocess.run(["rustc", "+" + NIGHTLY, "--print", "sysroot"], capture_output=True, text=True, check=True).stdout.strip()
    tools = glob.glob(os.path.join(sysroot, "lib", "rustlib", "*", "bin"))[0]
    merged = os.path.join(OUT, name + ".profdata")
    subprocess.run([os.path.join(tools, "llvm-profdata"), "merge", "-sparse", "-o", merged] + glob.glob(os.path.join(profiles, "*.profraw")), check=True)
    # Every test executable the run built, as llvm-cov's objects.
    listing = subprocess.run(["cargo", "+" + NIGHTLY, "test", "--release", "--no-run", "--message-format=json"] + args, cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    objects = [m["executable"] for m in map(json.loads, listing.splitlines()) if m.get("reason") == "compiler-artifact" and m.get("executable")]
    command = [os.path.join(tools, "llvm-cov"), "export", "--format=lcov", "--instr-profile", merged, objects[0]]
    for extra in objects[1:]:
        command += ["--object", extra]
    lcov = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    reached, compiled, current = collections.defaultdict(set), collections.defaultdict(set), None
    for line in lcov.splitlines():
        if line.startswith("SF:"):
            path = os.path.relpath(line[3:], ROOT)
            current = path if not path.startswith("..") else None
        elif line.startswith("DA:") and current:
            number, count = line[3:].split(",")[:2]
            compiled[current].add(int(number))
            if int(count) > 0:
                reached[current].add(int(number))
    return reached, compiled


def mutated():
    """{file: set(lines)} inside functions cargo-mutants mutates, and (mutants, survivors)."""
    known = open(os.path.join(ROOT, "tools", "mutants", "known-aarch64.txt"), encoding="utf-8").read().splitlines()
    files = next(l.split(":", 1)[1].split() for l in known if l.startswith(("files:", "# files:")))
    args = ["cargo", "mutants", "--list", "--json"]
    for f in files:
        args += ["--file", f]
    listed = json.loads(subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout)
    lines = collections.defaultdict(set)
    for mutant in listed:
        span = (mutant.get("function") or {}).get("span")
        if span:
            lines[mutant["file"]].update(range(span["start"]["line"], span["end"]["line"] + 1))
    survivors = sum(1 for l in known if l and not l.startswith(("#", "files:")) and "\t" in l)
    return lines, len(listed), survivors


def main():
    os.makedirs(OUT, exist_ok=True)
    code = inventory()
    is_asm = lambda f: f.startswith("c/")
    # The fork's own kernels are AArch64's; x86-64's are upstream's, unchanged.
    kind = lambda f, unsafe: ("arm" if "aarch64" in f or "neon" in f else "x86") if is_asm(f) else "unsafe" if unsafe else "safe"
    total = collections.Counter()
    for f, lines in code.items():
        for unsafe in lines.values():
            total[kind(f, unsafe)] += 1

    def tally(reached, keep=lambda f: True):
        got = collections.Counter()
        for f, lines in code.items():
            if not keep(f):
                continue
            for number, unsafe in lines.items():
                if number in reached.get(f, ()):
                    got[kind(f, unsafe)] += 1
        return got

    print("qa_coverage: running the suites with coverage (a few minutes)", file=sys.stderr)
    tests, compiled = coverage("tests", ["--lib", "--tests"])
    guard, _ = coverage("guard", ["--lib"], ["guard"])
    vectors, _ = coverage("vectors", ["--manifest-path", "test_vectors/Cargo.toml"])
    # Everything compiled on this machine: a line the tests' binaries hold.
    mutants, mutant_count, survivors = mutated()
    asm_all = {f: set(lines) for f, lines in code.items() if is_asm(f)}

    here = tally(compiled)
    rows = [
        (f"Test suites ({platform.machine()} here)", tally(tests), "every line run once or more by the library's and integration tests"),
        ("Official test vectors", tally(vectors), "lines run while checking the published vectors"),
        ("Guard pages", tally(guard), "lines run with inputs and outputs against unmapped pages; assembly reached, not counted"),
        ("AddressSanitizer", tally(tests, lambda f: not is_asm(f)), "the suites' lines, assembly excepted"),
        ("ThreadSanitizer", tally(tests, lambda f: f in SHARED), "the suites' lines in files threads share memory in"),
        (f"Mutation testing ({mutant_count} mutants, {mutant_count - survivors} caught on AArch64)", tally(mutants), "lines in functions with a planted bug"),
        ("Proofs (AArch64 assembly)", tally({f: s for f, s in asm_all.items() if f in PROVED}), "every Arm assembly file, each kernel proved"),
    ]
    width = max(len(r[0]) for r in rows)
    cols = [("safe", "safe Rust"), ("unsafe", "unsafe Rust"), ("arm", "Arm assembly"), ("x86", "x86 assembly")]
    print(f"{'technique':{width}}" + "".join(f"  {title:>15}" for _, title in cols))
    print(f"{'lines of code':{width}}" + "".join(f"  {total[k]:>15}" for k, _ in cols))
    print(f"{'  of them compiled on this machine':{width}}" + "".join(f"  {here[k] if k in ('safe', 'unsafe') else '':>15}" for k, _ in cols))
    pct = lambda n, d: f"{n:>6} {100 * n / d:5.1f}%" if d else "-"
    for name, got, _ in rows:
        print(f"{name:{width}}" + "".join(f"  {pct(got[k], total[k]):>15}" for k, _ in cols))
    print()
    for name, _, what in rows:
        print(f"  {name}: {what}")


if __name__ == "__main__":
    main()
