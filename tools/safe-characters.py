"""The characters we consider safe for writing a hash as text: each one, and
any sequence of them, survives every Unicode-clean system unchanged, shows in
the order it is stored, and renders on its own.

    python3 tools/safe-characters.py UCD_DIR

UCD_DIR holds Unicode 14.0.0's data files below
(https://www.unicode.org/Public/14.0.0/ucd/), which the tool checks by
SHA-256; `b3sum -c` checks the BLAKE3 hashes, given as lines of the hash, two
spaces, and the name. Python's unicodedata must be the same version. The rules are conservative: anything
that might cause trouble on some platform is out.

A rule on single characters suffices, with no rule on pairs: the only
interactions between characters that each pass alone are composition (two
characters normalizing to one: the second has a normalization quick-check of
"Maybe"), shaping (joining scripts, Hangul jamo) and reordering (right-to-left
and neutral directions, logical-order exceptions). Each excludes every
character that can take part, on either side, so no sequence of safe
characters interacts.
"""
import collections
import sys
import unicodedata

# Each file's SHA-256 and BLAKE3.
FILES = {
    "ArabicShaping.txt": ("c7698811e9adb6cc98fb996a7de4be2b6532f2ac67e76055cc8afdbf6ee18af3",
                          "fa1d26d40ffff0649910ac500544b721a863fa7c3d35295c7d981c62a2bd4221"),
    "DerivedAge.txt": ("7e058dec02147098bc9c28d86209f0f251bba0538f3b5a705ad02ea3bb709fe0",
                       "f022a5b165f8a8a798847636987d34e9c9349a1453ea5cfc1e53699534cd8d51"),
    "DerivedCoreProperties.txt": ("e3eddd7d469cd1b0feed7528defad1a1cc7c6a9ceb0ae4446a6d10921ed2e7bc",
                                  "ca62e64c59e8a99dd923a7f7bd5022f4a2e8102ac0505303fd4eb99cc55a497f"),
    "DerivedNormalizationProps.txt": ("b2c444c20730b097787fdf50bd7d6dd3fc5256ab8084f5b35b11c8776eca674c",
                                      "6d29c0bc8f173c916aacd435567be2e6d8319c24ec743d5b3ee6584f13fe96b6"),
    "PropList.txt": ("6bddfdb850417a5bee6deff19290fd1b138589909afb50f5a049f343bf2c6722",
                     "3ebb57bf840489327983d011cfb1e4b39e52462142f1e406816789925fee3e78"),
    "Scripts.txt": ("52db475c4ec445e73b0b16915448c357614946ad7062843c563e00d7535c6510",
                    "7d13a1c7e88acc3cd2efdde82de5cf64338bbdf9e48a5af450afb3c80175a83d"),
}

assert unicodedata.unidata_version == "14.0.0", f"unicodedata is {unicodedata.unidata_version}: these rules read 14.0.0"


def ranges(path, want=None):
    """{code point: value} from a UCD file of `range ; value` lines."""
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(";")]
        a, _, b = fields[0].partition("..")
        value = fields[1]
        if want is not None and value != want:
            continue
        for c in range(int(a, 16), int(b or a, 16) + 1):
            out[c] = fields[1:]
    return out


def main(ucd):
    import hashlib
    for name, (sha, _) in FILES.items():
        got = hashlib.sha256(open(f"{ucd}/{name}", "rb").read()).hexdigest()
        assert got == sha, f"{name}: SHA-256 {got}, not Unicode 14.0.0's {sha}"
    age = {c: v[0] for c, v in ranges(f"{ucd}/DerivedAge.txt").items()}
    script = {c: v[0] for c, v in ranges(f"{ucd}/Scripts.txt").items()}
    proplist = collections.defaultdict(set)
    for line in open(f"{ucd}/PropList.txt", encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            r, p = [f.strip() for f in line.split(";")]
            a, _, b = r.partition("..")
            proplist[p].update(range(int(a, 16), int(b or a, 16) + 1))
    ignorable = set(ranges(f"{ucd}/DerivedCoreProperties.txt", "Default_Ignorable_Code_Point"))
    maybe = set()
    for line in open(f"{ucd}/DerivedNormalizationProps.txt", encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(";")]
        if fields[1] in ("NFC_QC", "NFKC_QC") and len(fields) > 2 and fields[2] == "M":
            a, _, b = fields[0].partition("..")
            maybe.update(range(int(a, 16), int(b or a, 16) + 1))
    joining = {c: v[1] for c, v in ranges(f"{ucd}/ArabicShaping.txt").items()}
    hangul_jamo = [(0x1100, 0x11FF), (0xA960, 0xA97F), (0xD7B0, 0xD7FF)]

    def why_not(c):
        """The first rule `c` fails, or None."""
        ch = chr(c)
        if c > 0xFFFF:
            return "outside the Basic Multilingual Plane"
        if 0xD800 <= c <= 0xDFFF or c not in age:
            return "unassigned or a surrogate"
        if unicodedata.category(ch) != "Lo":
            return "not a caseless letter (Lo)"
        if unicodedata.bidirectional(ch) != "L":
            return "not strongly left-to-right"
        if any(unicodedata.normalize(f, ch) != ch for f in ("NFC", "NFD", "NFKC", "NFKD")):
            return "changed by normalization"
        if ch.lower() != ch or ch.upper() != ch or ch.casefold() != ch:
            return "changed by case mapping"
        if c in ignorable or c in proplist["White_Space"] or c in proplist["Pattern_White_Space"]:
            return "ignorable or white space"
        if c in maybe:
            return "may compose with what precedes it"
        if any(a <= c <= b for a, b in hangul_jamo):
            return "a Hangul jamo"
        if joining.get(c, "U") != "U":
            return "joins its neighbours"
        if c in proplist["Logical_Order_Exception"]:
            return "a logical-order exception"
        return None

    reasons = collections.Counter()
    safe = []
    for c in range(0x110000):
        r = why_not(c)
        reasons[r or "safe"] += 1
        if r is None:
            safe.append(c)
    print(f"Unicode {unicodedata.unidata_version}: {len(safe)} safe characters of 1,114,112 code points")
    for r, n in reasons.most_common():
        print(f"  {n:8,}  {r}")
    print("\nThe safe characters by script, and by the Unicode version that assigned them:")
    by_script = collections.Counter(script.get(c, "Unknown") for c in safe)
    for s, n in by_script.most_common():
        print(f"  {n:6,}  {s}")
    by_age = collections.Counter(age[c] for c in safe)
    total = 0
    for v in sorted(by_age, key=lambda v: tuple(int(x) for x in v.split("."))):
        total += by_age[v]
        print(f"  by Unicode {v:5}: {total:6,}")
    # The last cut, for fonts: the smaller scripts above lack a default font on
    # many systems (distinct characters would show as one box), and some of
    # their letters look like punctuation (Latin clicks) or are signs (Vedic,
    # Phags-pa) or belong to joining scripts (Mongolian's Ali Gali). The CJK
    # Unified Ideographs that Unicode 1.1 assigned are in every platform's
    # fonts, one width, one script, and every one passes every rule above.
    cjk = [c for c in safe if 0x4E00 <= c <= 0x9FFF and script.get(c) == "Han" and age[c] == "1.1"]
    assert cjk == list(range(0x4E00, 0x9FA6)), "Unicode 1.1's CJK Unified Ideographs are U+4E00..U+9FA5, every one safe"
    print(f"\nThe alphabet: {len(cjk):,} characters, U+4E00..U+9FA5 (Unicode 1.1's CJK Unified Ideographs)")
    return safe, script, age


if __name__ == "__main__":
    main(sys.argv[1])
