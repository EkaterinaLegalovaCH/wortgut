#!/usr/bin/env bash
# Download the one-time source data into data/ (never committed — see README).
#
#   ./scripts/fetch_data.sh
#
# Both files are cached: re-running skips anything already present, so this is
# safe to run again after a checkout.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
mkdir -p "$DATA_DIR"

FREQ_URL="https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/de/de_50k.txt"
DICT_VERSION="1.9-fd1"
DICT_URL="https://download.freedict.org/dictionaries/deu-eng/${DICT_VERSION}/freedict-deu-eng-${DICT_VERSION}.src.tar.xz"
# SHA-512 published by freedict.org for this pinned release: the release is
# immutable, so verifying it costs nothing and catches a truncated download.
DICT_SHA512="31c7143ae53074a5f357b80f4a6ccce8fa26f77a237476edca1353d5783293a0a8aef3a47f087de3e0c38f5aa7064196b7347886e1049116dc2964c6532397bc"

download() {
  local url="$1" target="$2"
  if [[ -s "$target" ]]; then
    echo "have  $(basename "$target")"
    return 0
  fi
  echo "fetch $(basename "$target")"
  curl -fSL --retry 3 -o "$target" "$url"
}

download "$FREQ_URL" "$DATA_DIR/de_50k.txt"

if [[ ! -s "$DATA_DIR/deu-eng.tei" ]]; then
  download "$DICT_URL" "$DATA_DIR/freedict-deu-eng-src.tar.xz"

  echo "verify sha512"
  echo "${DICT_SHA512}  $DATA_DIR/freedict-deu-eng-src.tar.xz" | sha512sum -c - >/dev/null \
    || { echo "error: dictionary archive failed its checksum check" >&2; exit 1; }

  echo "unpack deu-eng.tei"
  tar -xJf "$DATA_DIR/freedict-deu-eng-src.tar.xz" -C "$DATA_DIR" deu-eng/deu-eng.tei deu-eng/COPYING
  mv "$DATA_DIR/deu-eng/deu-eng.tei" "$DATA_DIR/deu-eng.tei"
  mv "$DATA_DIR/deu-eng/COPYING" "$DATA_DIR/freedict-COPYING"
  rmdir "$DATA_DIR/deu-eng"
else
  echo "have  deu-eng.tei"
fi

echo
echo "Data ready in $DATA_DIR. Next (one command at a time — SQLite takes one writer):"
echo "  uv run python scripts/import_freq.py"
echo "  uv run python scripts/import_dict.py"
