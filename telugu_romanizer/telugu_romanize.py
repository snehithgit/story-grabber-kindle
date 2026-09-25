#!/usr/bin/env python3
"""Offline Telugu -> English-letters (romanization) for HTML or text files.

No internet, no pip installs, no AI model. Only Telugu characters are
changed; tags, attributes, scripts and English text are left byte-for-byte.

English words written in Telugu script (ఫోన్, కంప్యూటర్, స్కూల్) are turned
back into real English spelling (phone, computer, school):
  1. my_words.tsv  - your own corrections (always wins)
  2. automatic     - sound-matching against english_words.txt

Examples:
    python telugu_romanize.py page.html -o page_english.html
    python telugu_romanize.py content_output/pages -o pages_english
    python telugu_romanize.py page.html --report check.tsv   # review guesses
    python telugu_romanize.py --text "నా ఫోన్లో కంప్యూటర్ భోజనాలు"
"""

from __future__ import annotations

import argparse
import re
import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- character tables -------------------------------------------------------

CONSONANTS = {
    "క": "k", "ఖ": "kh", "గ": "g", "ఘ": "gh", "ఙ": "n",
    "చ": "ch", "ఛ": "chh", "జ": "j", "ఝ": "jh", "ఞ": "n",
    "ట": "t", "ఠ": "th", "డ": "d", "ఢ": "dh", "ణ": "n",
    "త": "t", "థ": "th", "ద": "d", "ధ": "dh", "న": "n",
    "ప": "p", "ఫ": "ph", "బ": "b", "భ": "bh", "మ": "m",
    "య": "y", "ర": "r", "ఱ": "r", "ల": "l", "ళ": "l", "ఴ": "zh",
    "వ": "v", "శ": "sh", "ష": "sh", "స": "s", "హ": "h",
    "ౘ": "ts", "ౙ": "dz", "ౚ": "r",
}
# (casual, simple) spellings; short vowels are the same in both.
VOWELS = {
    "అ": ("a", "a"), "ఆ": ("aa", "a"), "ఇ": ("i", "i"), "ఈ": ("ee", "i"),
    "ఉ": ("u", "u"), "ఊ": ("oo", "u"), "ఋ": ("ru", "ru"), "ౠ": ("roo", "ru"),
    "ఌ": ("lu", "lu"), "ౡ": ("loo", "lu"), "ఎ": ("e", "e"), "ఏ": ("e", "e"),
    "ఐ": ("ai", "ai"), "ఒ": ("o", "o"), "ఓ": ("o", "o"), "ఔ": ("au", "au"),
}
VOWEL_SIGNS = {
    "ా": ("aa", "a"), "ి": ("i", "i"), "ీ": ("ee", "i"), "ు": ("u", "u"),
    "ూ": ("oo", "u"), "ృ": ("ru", "ru"), "ౄ": ("roo", "ru"), "ె": ("e", "e"),
    "ే": ("e", "e"), "ై": ("ai", "ai"), "ొ": ("o", "o"), "ో": ("o", "o"),
    "ౌ": ("au", "au"), "ౢ": ("lu", "lu"), "ౣ": ("loo", "lu"),
}
VIRAMA = "్"
ANUSVARA = "ం"
HALF_ANUSVARA = "ఁ"
VISARGA = "ః"
ZW = {"‌", "‍"}
IGNORED = {"఼", "ౕ", "ౖ"} | ZW
DIGITS = {chr(0x0C66 + i): str(i) for i in range(10)}

TELUGU_RUN = re.compile(r"[ఀ-౿‌‍]+")
HTML_MARKUP = re.compile(
    r"(?is)(<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>|"
    r"<!--.*?-->|<![^>]*>|<[^>]*>)"
)

# Telugu case endings that get glued onto English words: ఫోన్లో = phone + lo
SUFFIXES = sorted([
    "", "లో", "లోని", "లోకి", "లోనూ", "ను", "ని", "నే", "కి", "కు", "కే", "తో",
    "పై", "గా", "లు", "లను", "లని", "లకు", "లకి", "లలో", "లతో", "లే", "ల",
    "దే", "నీ", "లన్నీ", "ల్లో", "ల్ని",
], key=len, reverse=True)


def romanize(text: str, style: str = "casual") -> str:
    """Letter-by-letter romanization of Telugu text."""
    pick = 0 if style == "casual" else 1
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in CONSONANTS:
            out.append(CONSONANTS[ch])
            while nxt and nxt in IGNORED - ZW:      # nukta / length marks
                i += 1
                nxt = text[i + 1] if i + 1 < n else ""
            if nxt in VOWEL_SIGNS:
                out.append(VOWEL_SIGNS[nxt][pick])
                i += 1
            elif nxt == VIRAMA:
                i += 1
            else:
                out.append("a")
        elif ch in VOWELS:
            out.append(VOWELS[ch][pick])
        elif ch in (ANUSVARA, HALF_ANUSVARA):
            follow = CONSONANTS.get(nxt, "")
            out.append("m" if not follow or follow[0] in "pbm" else "n")
        elif ch == VISARGA:
            out.append("h")
        elif ch in DIGITS:
            out.append(DIGITS[ch])
        elif ch in IGNORED or ch in VOWEL_SIGNS or ch == VIRAMA:
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# --- English detection --------------------------------------------------------

# Sound markers shared by both sides:
#   Q = long "aa" (ా)  - English o/ar in shop, boss, doctor, car, class
#   A = "yaa" (్యా)    - English short a in fan, bank, match, cash
#   @ = neutral "a" produced by -tion/-sion (station -> steshan)
VOWEL_CHARS = set("aeiouAQ@")
CLOSE_VOWELS = {
    frozenset("au"): 0.25, frozenset("ao"): 0.35, frozenset("Qo"): 0.5,
    frozenset("Qa"): 0.35, frozenset("QA"): 0.25, frozenset("aA"): 0.35,
    frozenset("a@"): 0.0, frozenset("ae"): 0.4, frozenset("ie"): 0.4,
}


def _collapse(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def english_sound(word: str) -> str:
    """Rough 'how would this be written in Telugu' form of an English word.

    Upper-case I U O E mark long vowels while the rules run, so the
    short-vowel rules below leave them alone; they are lowered at the end.
    """
    w = word.lower()
    rules = [
        (r"^kn", "n"), (r"^wr", "r"), (r"^ps", "s"), (r"mb$", "m"),
        (r"tion|ssion|sion", "sh@n"), (r"ture", "ch@r"),
        (r"ai(?=l)", "EyI"), (r"ai(?=[^n])", "E"), (r"ay$", "E"),
        (r"igh", "aI"), (r"gh", ""), (r"oa", "O"), (r"ph", "f"), (r"ck", "k"),
        (r"sch", "sk"), (r"chr", "kr"), (r"^ch(?=r|l)", "k"), (r"qu", "kv"), (r"q", "k"), (r"x", "ks"),
        (r"tch", "ch"), (r"c(?=[eiy])", "s"), (r"c(?!h)", "k"), (r"dge", "j"), (r"g(?=[eiy])", "j"),
        (r"ou|ow(?=[nl])", "aU"), (r"ow$", "O"),
        (r"th", "t"), (r"wh", "v"), (r"w", "v"), (r"z", "j"),
        (r"ee|ea|ie", "I"), (r"oo", "U"), (r"y$", "I"),
        (r"ar(?=[^aeiouIUOE]|$)", "Qr"),                        # car, market
        (r"[eoiu]r(?=[^aeiouIUOE]|$)", "@r"),                   # computer, doctor, sir
        # magic e: base->bes, line->lain, phone->fon, tube->tyub
        (r"a([^aeiou@IUOE])e$", r"E\1"), (r"i([^aeiou@IUOE])e$", r"aI\1"),
        (r"o([^aeiou@IUOE])e$", r"O\1"), (r"u([^aeiou@IUOE])e$", r"yU\1"),
        (r"(?<=[^aeiouIUOE])e$", ""),
        # 'a' before one consonant + vowel: station, manager, paper -> e
        (r"(?<=[^aeiou])a(?=(?:sh|ch|[^aeiouyAQ@IUOE])[aeiou@IUOE])", "E"),
        # short vowels in closed syllables: fan->fAn, shop->shQp, bus->bas
        (r"a(?=[^aeiouyAQ@IUOEr](?:[^aeiouyAQ@IUOE]|$))", "A"),
        (r"o(?=[^aeiouyAQ@IUOE](?:[^aeiouyAQ@IUOE]|$))", "Q"),
        (r"u(?=[^aeiouyAQ@IUOE](?:[^aeiouyAQ@IUOE]|$))", "a"),
    ]
    for pattern, repl in rules:
        w = re.sub(pattern, repl, w)
    w = w.replace("I", "i").replace("U", "u").replace("O", "o").replace("E", "e")
    return _collapse(w)


def telugu_sound(latin: str) -> str:
    """Normalize a romanized Telugu word to the same rough form."""
    w = latin.lower()
    for pattern, repl in [
        (r"(?<=[^aeiou])yaa", "A"), (r"(?<=[^aeiou])y(?=oo|u)", ""),  # ఫ్యాన్=fan, ప్యూ=pu
        (r"aa", "Q"), (r"ee", "i"), (r"oo", "u"), (r"chh", "ch"),
        (r"kh", "k"), (r"gh", "g"), (r"jh", "j"), (r"th", "t"), (r"dh", "d"),
        (r"ph", "f"), (r"bh", "b"),
        (r"ar(?=[^aeiouAQ]|$)", "@r"),
    ]:
        w = re.sub(pattern, repl, w)
    return _collapse(w)


def skeleton(sound: str) -> str:
    """Consonant outline used to find candidate words quickly."""
    s = sound.replace("sh", "S").replace("ch", "C")
    return re.sub(r"[aeiouyAQ@]", "", s)


def _gap_costs(s: str) -> list[float]:
    """Cost of dropping/adding each letter. A vowel next to another vowel is
    cheap (office=ఆఫీస్ 'afis'); one between consonants is not (kiran≠crown)."""
    costs = []
    for i, ch in enumerate(s):
        if ch not in VOWEL_CHARS:
            costs.append(1.0)
            continue
        near = (i > 0 and s[i - 1] in VOWEL_CHARS) or (i + 1 < len(s) and s[i + 1] in VOWEL_CHARS)
        costs.append(0.3 if near else 0.8)
    return costs


def distance(a: str, b: str) -> float:
    """Edit distance tuned for vowels: similar vowels cost little."""
    ga, gb = _gap_costs(a), _gap_costs(b)
    prev = [0.0]
    for g in gb:
        prev.append(prev[-1] + g)
    for i, ca in enumerate(a):
        cur = [prev[0] + ga[i]]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                sub = 0.0
            elif ca in VOWEL_CHARS and cb in VOWEL_CHARS:
                sub = CLOSE_VOWELS.get(frozenset((ca, cb)), 0.5)
            else:
                sub = 1.0
            cur.append(min(prev[j - 1] + sub, prev[j] + ga[i], cur[j - 1] + gb[j - 1]))
        prev = cur
    return prev[-1]


class EnglishMatcher:
    def __init__(self, wordlist: Path | None, dictionary: Path | None, auto: bool,
                 dataset: Path | None = None):
        self.user: dict[str, str] = {}
        self.dataset: dict[str, str] = {}
        self.index: dict[str, list[tuple[int, str, str]]] = {}
        if dictionary and dictionary.is_file():
            for line in dictionary.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = re.split(r"\t|\s{2,}|=", line.strip(), maxsplit=1)
                if len(parts) == 2:
                    self.user[parts[0].strip()] = parts[1].strip()
        if dataset and dataset.is_file():
            for line in dataset.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    self.dataset[parts[0].strip()] = parts[1].strip()
        if auto and wordlist and wordlist.is_file():
            rank = 0
            for line in wordlist.read_text(encoding="utf-8").splitlines():
                word = line.strip().lower()
                if not word or word.startswith("#") or not word.isalpha() or len(word) < 2:
                    continue
                sound = english_sound(word)
                key = skeleton(sound)
                if len(key) >= 2:
                    self.index.setdefault(key, []).append((rank, word, sound))
                rank += 1
            self.size = rank
        self.detected: dict[str, tuple[str, str, float]] = {}

    @lru_cache(maxsize=100_000)
    def guess(self, stem: str) -> tuple[str, float] | None:
        """English word for a Telugu stem, or None."""
        if not self.index:
            return None
        sound = telugu_sound(romanize(stem))
        key = skeleton(sound)
        if len(key) < 2:
            return None
        best: tuple[float, str] | None = None
        for rank, word, esound in self.index.get(key, ()):
            dist = distance(sound, esound)
            score = dist + rank / 25_000          # prefer common words
            if best is None or score < best[0]:
                best = (score, word, dist)
        if best is None:
            return None
        limit = max(0.5, 0.08 * len(sound))     # reject loose/accidental matches
        return (best[1], best[2]) if best[2] <= limit else None


def split_loanword(word: str) -> list[tuple[str, str]]:
    """Possible (stem, suffix) splits where the stem ends like an English word.

    Telugu words end in a vowel; English words written in Telugu end in a
    bare consonant (ఫోన్, స్కూల్). Forms like బస్సు go in my_words.tsv.
    """
    clean = "".join(ch for ch in word if ch not in ZW)
    splits = []
    for suffix in SUFFIXES:
        if suffix and not clean.endswith(suffix):
            continue
        stem = clean[: len(clean) - len(suffix)] if suffix else clean
        if len(stem) >= 3 and stem.endswith(VIRAMA):
            splits.append((stem, suffix))
    return splits


def english_with_suffix(english: str, suffix: str, style: str, sep: str) -> str:
    if not suffix:
        return english
    rest = romanize(suffix, style)
    if suffix.startswith("లు"):                     # plural
        tail = suffix[2:]
        return english + "s" + (sep + romanize(tail, style) if tail else "")
    if suffix.startswith("ల") and len(suffix) > 1 and not suffix.startswith("లో"):
        return english + "s" + sep + romanize(suffix[1:], style)  # లలో = s lo
    return english + sep + rest


def convert_word(word: str, style: str, matcher: EnglishMatcher | None, sep: str) -> str:
    if matcher:
        clean = "".join(ch for ch in word if ch not in ZW)
        if clean in matcher.user:                          # whole-word override
            value = matcher.user[clean]
            return romanize(word, style) if value == "-" else value
        for suffix in SUFFIXES:                            # dictionary word + ending
            if suffix and clean.endswith(suffix) and clean[: -len(suffix)] in matcher.user:
                value = matcher.user[clean[: -len(suffix)]]
                if value != "-":
                    return english_with_suffix(value, suffix, style, sep)
        if clean in matcher.dataset:
            return matcher.dataset[clean]
        for stem, suffix in split_loanword(word):
            if stem in matcher.user:
                value = matcher.user[stem]
                if value == "-":
                    break
                return english_with_suffix(value, suffix, style, sep)
            hit = matcher.guess(stem)
            if hit:
                english, score = hit
                matcher.detected[stem] = (romanize(stem, style), english, score)
                return english_with_suffix(english, suffix, style, sep)
    return romanize(word, style)


def convert(content: str, style: str, both: bool,
            matcher: EnglishMatcher | None = None, sep: str = " ") -> str:
    def replace(match: re.Match[str]) -> str:
        latin = convert_word(match.group(0), style, matcher, sep)
        return f"{match.group(0)} ({latin})" if both else latin
    return TELUGU_RUN.sub(replace, content)


def convert_html(content: str, style: str = "casual", both: bool = False,
                 matcher: EnglishMatcher | None = None, sep: str = " ") -> str:
    """Romanize visible HTML text while preserving markup and raw code."""
    parts = HTML_MARKUP.split(content)
    return "".join(
        part if part.startswith("<") else convert(part, style, both, matcher, sep)
        for part in parts
    )


# --- files ------------------------------------------------------------------

def output_path(src: Path, out: Path | None, base: Path | None) -> Path:
    if out is None:
        return src.with_name(f"{src.stem}.latin{src.suffix}")
    if base is None:
        return out if out.suffix else out / src.name
    return out / src.relative_to(base)


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline Telugu -> Latin romanization for HTML/text files.")
    ap.add_argument("input", nargs="?", type=Path, help="HTML/text file or folder")
    ap.add_argument("-o", "--output", type=Path, help="output file or folder")
    ap.add_argument("--style", choices=["casual", "simple"], default="casual",
                    help="casual: bhojanaalu (default) | simple: bhojanalu")
    ap.add_argument("--both", action="store_true", help="keep Telugu and add romanization in brackets")
    ap.add_argument("--in-place", action="store_true", help="overwrite the original files")
    ap.add_argument("--ext", default=".html,.htm,.txt,.md", help="extensions to process in a folder")
    ap.add_argument("--dict", type=Path, default=HERE / "my_words.tsv",
                    help="your corrections: Telugu<TAB>english per line (default: my_words.tsv)")
    ap.add_argument("--words", type=Path, default=HERE / "english_words.txt",
                    help="English word list, most common first (default: english_words.txt)")
    ap.add_argument("--dataset", type=Path, default=HERE / "tenglish_words.tsv",
                    help="word map built from indiehackers/tenglish_dataset")
    ap.add_argument("--no-english", action="store_true", help="plain letter-by-letter only")
    ap.add_argument("--no-auto", action="store_true", help="use only my_words.tsv, no automatic guessing")
    ap.add_argument("--suffix-sep", default=" ", help="between English word and Telugu ending (default: space → 'phone lo')")
    ap.add_argument("--report", type=Path, help="write detected English words to this TSV for review")
    ap.add_argument("--text", help="convert this text and print it")
    args = ap.parse_args()

    matcher = None
    if not args.no_english:
        matcher = EnglishMatcher(args.words, args.dict, auto=not args.no_auto,
                                 dataset=args.dataset)
        if not args.no_auto and not matcher.index:
            print(f"Note: {args.words} not found; only {args.dict.name} corrections are used.", file=sys.stderr)

    if args.text is not None:
        print(convert(args.text, args.style, args.both, matcher, args.suffix_sep))
        return 0
    if args.input is None:
        ap.error("give an input file/folder or --text")

    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}
    if args.input.is_dir():
        files = [p for p in sorted(args.input.rglob("*"))
                 if p.suffix.lower() in exts and ".latin." not in p.name]
        base = args.input
        if args.output and args.output.resolve().is_relative_to(args.input.resolve()):
            files = [p for p in files if not p.resolve().is_relative_to(args.output.resolve())]
    elif args.input.is_file():
        files, base = [args.input], None
    else:
        print(f"Not found: {args.input}", file=sys.stderr)
        return 1

    changed = 0
    for src in files:
        text = src.read_text(encoding="utf-8", errors="replace")
        result = (
            convert_html(text, args.style, args.both, matcher, args.suffix_sep)
            if src.suffix.lower() in {".html", ".htm"}
            else convert(text, args.style, args.both, matcher, args.suffix_sep)
        )
        dst = src if args.in_place else output_path(src, args.output, base)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(result, encoding="utf-8")
        changed += result != text
    print(f"Processed {len(files)} file(s); {changed} contained Telugu.")

    if matcher and matcher.detected:
        print(f"Detected {len(matcher.detected)} English word(s) written in Telugu.")
        if args.report:
            lines = ["# telugu\tletter-by-letter\tenglish_guess\tscore (lower = surer)",
                     "# Copy wrong lines into my_words.tsv as: telugu<TAB>correct  (or '-' = not English)"]
            for stem, (latin, eng, score) in sorted(matcher.detected.items(), key=lambda kv: -kv[1][2]):
                lines.append(f"{stem}\t{latin}\t{eng}\t{score:.2f}")
            args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"Review list: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
