#!/usr/bin/env python3
"""Build an offline Telugu→Latin word map from indiehackers/tenglish_dataset.

Only the 209k-row telugu_asr split is downloaded. Rows whose Telugu and Latin
word counts align exactly vote on each word spelling; the most frequent
spelling wins. The resulting TSV is small, deterministic, and used offline.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as parquet


SOURCE = (
    "https://huggingface.co/api/datasets/indiehackers/tenglish_dataset/"
    "parquet/telugu_asr/train/0.parquet"
)
TELUGU_WORD = re.compile(r"[ఀ-౿‌‍]+")
LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*|[0-9]+")


def download(source: str, target: Path) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(source, timeout=120) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def build(source_file: Path, output: Path, minimum_votes: int) -> tuple[int, int]:
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    rows = 0
    parquet_file = parquet.ParquetFile(source_file)
    for batch in parquet_file.iter_batches(columns=["text", "translit"], batch_size=4096):
        texts = batch.column("text").to_pylist()
        transliterations = batch.column("translit").to_pylist()
        for text, translit in zip(texts, transliterations):
            rows += 1
            telugu = TELUGU_WORD.findall(str(text or ""))
            latin = LATIN_WORD.findall(str(translit or ""))
            if not telugu or len(telugu) != len(latin):
                continue
            for source_word, latin_word in zip(telugu, latin):
                votes[source_word][latin_word.lower()] += 1

    lines = [
        "# Derived from indiehackers/tenglish_dataset (telugu_asr/train)",
        "# https://huggingface.co/datasets/indiehackers/tenglish_dataset",
        "# telugu\tromanized",
    ]
    for word in sorted(votes):
        spelling, count = votes[word].most_common(1)[0]
        if count >= minimum_votes:
            lines.append(f"{word}\t{spelling}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows, len(lines) - 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path,
                        default=Path(__file__).with_name("tenglish_words.tsv"))
    parser.add_argument("--minimum-votes", type=int, default=1)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        source_file = Path(temporary) / "telugu_asr.parquet"
        print("Downloading indiehackers/tenglish_dataset telugu_asr split...")
        checksum = download(SOURCE, source_file)
        rows, words = build(source_file, args.output, max(1, args.minimum_votes))
    print(f"Built {args.output} with {words:,} words from {rows:,} rows.")
    print(f"Source SHA-256: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
