"""Warns on a few patterns of prose that sound like a language model
(Claudish) or that work against AGENTS.md's "Communication": contrasts
built on a negation, prohibitions (a rule that names a habit to drop,
where naming the habit to keep steers the reader better), stock
vocabulary, filler and metadiscourse, mid-sentence dashes, and sentences
too long or too chained to hold in mind.

    python3 tools/claudish.py FILE...     # Markdown whole; other files' comment lines
    python3 tools/claudish.py - < draft   # a commit message or comment, before it goes

Each warning names a line, a rule and the words, and the output ends with
the passage of AGENTS.md (the one beside this script) behind each rule
that fired. A warning is a reason to reread the sentence, and judgment
decides what changes: often nothing, sometimes a rewrite of the
paragraph around it. The script always exits 0.
"""
import os
import re
import sys

RULES = [
    ("contrast", r"\bnot\b[^.;:]{1,60}?,\s*but\b|,\s+not\s+(?:a |an |the )?[\w-]+[,.;]|\brather than\b|\binstead of\b|\bnot (?:only|just|merely)\b"),
    ("prohibition", r"\b(?:never|don't|do not|avoid(?:s|ed|ing)?|refrain\w*)\b"),
    ("vocabulary", r"\bload-bearing\b|\bspine\b|\bseams?\b|\bcrisp\w*|\bgenuine(?:ly)?\b|\bhonest\b|\bthe shape of\b|\bdelve\w*|\bthe real (?:question|issue|work|cost|win)\b|\btapestry\b|\bnuanced?\b|\brobust(?:ly)?\b|\bseamless(?:ly)?\b"),
    ("filler", r"\b(?:simply|just|really|actually|quite|very|basically|essentially|truly)\b"),
    ("metadiscourse", r"\b(?:note that|it is worth|it's worth|worth noting|in other words|to be clear|importantly|here's the thing|that said)\b"),
    ("dash", r"\w\s?—\s?\w"),
]
LONG_WORDS, CHAIN_MARKS = 50, 4

# Each rule's passage in AGENTS.md's "Communication", found by a phrase of it.
WHY = {
    "contrast": "Phrase positively or neutrally",
    "prohibition": "A rule names the habit to keep",
    "vocabulary": "warns on a few patterns that sound like a language model",
    "filler": "Sand off filler words",
    "metadiscourse": "Trim metadiscourse",
    "dash": "warns on a few patterns that sound like a language model",
    "long": "The reader has limited working memory",
    "chained": "The reader has limited working memory",
}
AGENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "AGENTS.md")


def passages():
    """Each rule's passage: (AGENTS.md's line number, its text). Stops when
    a phrase is gone from AGENTS.md: the two change together."""
    lines = open(AGENTS).read().split("\n")
    found = {}
    for rule, phrase in WHY.items():
        at = [n for n, line in enumerate(lines, 1) if phrase in line]
        assert at, f"claudish: AGENTS.md no longer says {phrase!r}: change WHY with it"
        found[rule] = (at[0], lines[at[0] - 1].strip().lstrip("-0123456789. "))
    return found


def prose(path, text):
    """The text's prose lines as (line number, text), an empty one
    between paragraphs: a Markdown file's outside code blocks and tables,
    any other file's comment lines."""
    markdown = path == "-" or path.endswith(".md")
    fenced = False
    for n, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if markdown:
            if s.startswith("```"):
                fenced = not fenced
            elif not fenced and not s.startswith("|") and not line.startswith("    "):
                if re.match(r"(?:[-*+]|\d+\.)\s|#", s):
                    yield n, ""  # a list item or heading starts a paragraph of its own
                yield n, s
        else:
            m = re.match(r"(?://[/!]?|#|\*|/\*)\s?(.*)", s)
            yield n, (m.group(1) if m else "")


def sentences(lines):
    """Sentences as (first line, text), joined across a paragraph's lines."""
    words, start = [], None
    for n, s in list(lines) + [(0, "")]:
        if not s:
            text, words, start, first = " ".join(words), [], None, start
            for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z(`*\"])", text):
                if sentence:
                    yield first, sentence
            continue
        start = start or n
        words.append(s)


def lint(path, text, fired):
    count = 0
    for line, s in sentences(prose(path, text)):
        found = [(name, m.group(0)) for name, rule in RULES for m in re.finditer(rule, s, re.I)]
        if len(s.split()) > LONG_WORDS:
            found.append(("long", f"{len(s.split())} words"))
        marks = len(re.findall(r"[:;]\s", s))
        if marks >= CHAIN_MARKS:
            found.append(("chained", f"{marks} colons and semicolons"))
        for name, words in found:
            fired.add(name)
            print(f"{path}:{line}: {name}: {words!r} in: {s[:100]}{'...' if len(s) > 100 else ''}")
        count += len(found)
    return count


why = passages()
fired = set()
total = sum(lint(p, sys.stdin.read() if p == "-" else open(p).read(), fired) for p in sys.argv[1:])
if fired:
    print("Why, in AGENTS.md's \"Communication\":")
    for line, text in sorted({why[name] for name in fired}):
        rules = ", ".join(n for n in WHY if n in fired and why[n] == (line, text))
        print(f"  {rules}: AGENTS.md:{line}: {text[:140]}{'...' if len(text) > 140 else ''}")
print(f"{total} warnings: each a reason to reread, none an error", file=sys.stderr)
