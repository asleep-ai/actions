#!/usr/bin/env bash
# Shared ktfmt pre-commit hook. ktfmt has no baseline of its own, so this
# script adds a content-hash baseline that exempts untouched legacy files.
#
# pre-commit runs this file from its clone of the hook repository, but with
# the working directory at the caller's repository root. Every path below is
# therefore relative to the caller, and java and ktfmt come from the caller's
# mise configuration, never from this repository.
set -euo pipefail

die() {
  echo "ktfmt hook: $*" >&2
  exit 1
}

# Reject arguments that are not valid UTF-8: the JVM cannot reliably open
# such a name, and mise exec would already have replaced the invalid bytes
# with U+FFFD, so the check must run on the raw arguments.
require_utf8_arguments() {
  local arg
  command -v iconv >/dev/null 2>&1 || die "iconv is required to validate file names"
  if [ "$#" -eq 0 ] || printf '%s\0' "$@" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
    return 0
  fi
  for arg in "$@"; do
    printf '%s' "$arg" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1 ||
      die "ktfmt cannot safely open a path that is not valid UTF-8: $(printf '%q' "$arg")"
  done
  die "ktfmt cannot safely open a path that is not valid UTF-8"
}

if [ -z "${KTFMT_HOOK_IN_MISE-}" ]; then
  command -v mise >/dev/null 2>&1 ||
    die "mise is not on PATH; java and ktfmt come from the project's mise configuration"
  require_utf8_arguments "$@"
  # Re-run inside the caller's mise environment so java is on PATH. mise
  # resolves the configuration from the current directory, which pre-commit
  # sets to the caller's repository root.
  KTFMT_HOOK_IN_MISE=1 exec mise exec -- bash "${BASH_SOURCE[0]}" "$@"
fi

BASELINE='config/ktfmt-baseline.txt'
MISE_TOOL='aqua:Kotlin/ktfmt'
# --enable-editorconfig picks up the caller's indent_size and max_line_length.
STYLE=(--kotlinlang-style --enable-editorconfig)
USAGE='usage: ktfmt.sh [--baseline PATH] [--write | --update-baseline | --bootstrap-baseline] [FILE...]'

TMP="$(mktemp -d)"
PARTIAL=''
cleanup() {
  rm -rf "$TMP"
  if [ -n "$PARTIAL" ]; then rm -f "$PARTIAL"; fi
}
trap cleanup EXIT

usage_error() {
  echo "ktfmt hook: $1" >&2
  echo "$USAGE" >&2
  exit 2
}

# The baseline and the file arguments are relative to the repository root, so
# a run from a subdirectory would silently match nothing.
require_repository_root() {
  local prefix
  prefix="$(git rev-parse --show-prefix 2>/dev/null)" || die "not inside a git work tree"
  [ -z "$prefix" ] || die "run from the repository root, not $prefix"
}

# ktfmt reads its @argfile one argument per line and splits lines on both LF
# and CR, so neither may appear in a path.
require_safe_path() {
  case "$1" in
    *$'\n'* | *$'\r'*) die "ktfmt cannot accept a newline or carriage return in a path: $1" ;;
  esac
}

# ktfmt runs on the JVM, which converts file names with the locale's charset
# and silently skips a file it cannot open. So every name listed in $1 must be
# valid UTF-8, and a non-ASCII name also needs a UTF-8 locale, without which
# the JVM may miss the file or print a mangled name.
require_jvm_safe_names() {
  local path non_ascii
  command -v iconv >/dev/null 2>&1 || die "iconv is required to validate file names"
  if ! iconv -f UTF-8 -t UTF-8 <"$1" >/dev/null 2>&1; then
    while IFS= read -r path; do
      printf '%s' "$path" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1 ||
        die "ktfmt cannot safely open a path that is not valid UTF-8: $(printf '%q' "$path")"
    done <"$1"
    die "ktfmt cannot safely open a path that is not valid UTF-8"
  fi
  non_ascii="$(LC_ALL=C tr -d '\000-\177' <"$1")"
  if [ -n "$non_ascii" ] && [ "$(locale charmap 2>/dev/null)" != UTF-8 ]; then
    die "non-ASCII file names need a UTF-8 locale for ktfmt (for example LANG=C.UTF-8)"
  fi
}

# Gather the FILE arguments into $TMP/files. ktfmt silently skips a path it
# cannot open, so a missing file fails here, baseline or not, instead of
# passing unchecked.
collect_files() {
  local path
  : >"$TMP/files"
  for path in "$@"; do
    require_safe_path "$path"
    printf '%s\n' "$path" >>"$TMP/files"
  done
  require_jvm_safe_names "$TMP/files"
  for path in "$@"; do
    [ -f "$path" ] || die "no such file: $path"
  done
}

JAR=''
resolve_ktfmt() {
  local version dir
  [ -z "$JAR" ] || return 0
  if [ -n "${KTFMT_JAR-}" ]; then
    JAR="$KTFMT_JAR"
  else
    # `mise where` alone falls back to any installed release, so insist that
    # the caller's configuration declares the version it wants.
    version="$(mise current "$MISE_TOOL" 2>/dev/null)" || version=''
    if [ -z "$version" ]; then
      die "the project's mise configuration does not declare $MISE_TOOL; add it under [tools] in mise.toml (pin a version, e.g. \"$MISE_TOOL\" = \"0.64\")"
    fi
    dir="$(mise where "$MISE_TOOL")" || die "$MISE_TOOL $version is not installed; run: mise install"
    # The aqua package installs the release jar as a bare file named ktfmt.
    JAR="$dir/ktfmt"
  fi
  [ -f "$JAR" ] || die "ktfmt jar not found at $JAR"
  command -v java >/dev/null 2>&1 || die "java is not on PATH; declare java under [tools] in mise.toml"
}

# Write the argfile for the paths listed in $1; the status covers every write.
# ktfmt expands an @argfile only when it is the sole argument, so the flags go
# into the file as well. ktfmt has no `--`, so a path starting with "-" gets a
# "./" prefix.
write_argfile() {
  local paths="$1"
  shift
  { printf '%s\n' "${STYLE[@]}" "$@" && LC_ALL=C sed 's#^-#./-#' "$paths"; } >"$TMP/argfile"
}

# Run ktfmt --dry-run on the paths listed in $1, write the files it would
# reformat to $2, sorted, and set KTFMT_STATUS to ktfmt's exit status. Without
# --set-exit-if-changed a dry run exits 0 while listing files, so a failure is
# ktfmt itself failing (a parse error, a crash) and its list is incomplete.
# Every other failure stops the script explicitly: bash ignores set -e inside
# a function called from a condition, so this must not rely on it.
KTFMT_STATUS=0
ktfmt_dry_run() {
  resolve_ktfmt
  write_argfile "$1" --dry-run || die "could not write the ktfmt argument file"
  KTFMT_STATUS=0
  java -jar "$JAR" "@$TMP/argfile" >"$TMP/dry-run" || KTFMT_STATUS=$?
  { LC_ALL=C sed 's#^\./-#-#' "$TMP/dry-run" | LC_ALL=C sort >"$2"; } ||
    die "could not process ktfmt's output"
}

# Print '<git blob hash> <path>' for each path listed in $1, in order.
hash_entries() {
  [ -s "$1" ] || return 0
  tr '\n' '\0' <"$1" | xargs -0 git hash-object -- >"$TMP/hashes"
  paste -d' ' "$TMP/hashes" "$1"
}

# grep into file $1. grep exits 1 when it selects nothing; only a status
# above 1 is an error, and it must never pass as an empty selection.
grep_to() {
  local out="$1" status=0
  shift
  # Compare raw bytes: in a UTF-8 locale GNU grep can drop a line with an
  # invalid sequence while still exiting 0.
  LC_ALL=C grep "$@" >"$out" || status=$?
  [ "$status" -le 1 ] || die "grep failed with status $status"
}

has_baseline() {
  [ -f "$BASELINE" ]
}

write_baseline() {
  local entries="$1" dir
  dir="$(dirname -- "$BASELINE")"
  mkdir -p -- "$dir"
  # Write next to the target and rename, so a failure never half-writes it.
  PARTIAL="$(mktemp "$dir/.ktfmt-baseline.XXXXXX")"
  {
    echo '# Kotlin files ktfmt would reformat that predate the ktfmt hook.'
    echo "# Each line is '<git blob hash> <path>', sorted by path."
    echo '# An entry exempts a file only while it still hashes to the recorded'
    echo '# value, so any edit makes the hook check the whole file again.'
    echo '# Never add or edit entries by hand. Shrink with:'
    echo '#   pre-commit run ktfmt-baseline-refresh --hook-stage manual --all-files'
    cat "$entries"
  } >"$PARTIAL"
  chmod 644 "$PARTIAL"
  mv -f "$PARTIAL" "$BASELINE"
  PARTIAL=''
}

check_files() {
  collect_files "$@"
  if has_baseline; then
    # Baselined legacy files stay exempt until their content changes.
    hash_entries "$TMP/files" >"$TMP/entries"
    grep_to "$TMP/pending-entries" -Fxv -f "$BASELINE" "$TMP/entries"
    LC_ALL=C cut -d' ' -f2- "$TMP/pending-entries" >"$TMP/pending"
  else
    cp "$TMP/files" "$TMP/pending"
  fi
  [ -s "$TMP/pending" ] || return 0
  ktfmt_dry_run "$TMP/pending" "$TMP/changed"
  [ "$KTFMT_STATUS" -eq 0 ] || die "ktfmt failed while checking Kotlin files; fix the formatter error reported above"
  [ -s "$TMP/changed" ] || return 0
  cat "$TMP/changed"
  cat >&2 <<'HINT'
The files listed above are not ktfmt-formatted (--kotlinlang-style --enable-editorconfig).
Format them with: pre-commit run ktfmt-write --hook-stage manual --files <file>...
HINT
  exit 1
}

write_files() {
  [ "$#" -gt 0 ] || usage_error "--write needs at least one file"
  collect_files "$@"
  resolve_ktfmt
  write_argfile "$TMP/files" || die "could not write the ktfmt argument file"
  java -jar "$JAR" "@$TMP/argfile"
}

# A refresh is a ratchet: it keeps an entry only while the file is still
# tracked, still hashes to the recorded value and is still reported by
# ktfmt, so it can drop exemptions but never add one.
refresh_baseline() {
  if ! has_baseline; then
    echo "ktfmt hook: no baseline at $BASELINE; nothing to refresh" >&2
    return 0
  fi
  grep_to "$TMP/old" -E '^[0-9a-f]{40,64} ' "$BASELINE"
  LC_ALL=C cut -d' ' -f2- "$TMP/old" | LC_ALL=C sort -u >"$TMP/old-paths"
  git ls-files -z | tr '\0' '\n' | LC_ALL=C sort -u >"$TMP/tracked"
  LC_ALL=C comm -12 "$TMP/old-paths" "$TMP/tracked" | while IFS= read -r path; do
    if [ -f "$path" ] && [ ! -L "$path" ]; then printf '%s\n' "$path"; fi
  done >"$TMP/candidates"
  require_jvm_safe_names "$TMP/candidates"
  hash_entries "$TMP/candidates" >"$TMP/current"
  grep_to "$TMP/unchanged" -Fx -f "$TMP/old" "$TMP/current"
  : >"$TMP/next"
  if [ -s "$TMP/unchanged" ]; then
    LC_ALL=C cut -d' ' -f2- "$TMP/unchanged" >"$TMP/unchanged-paths"
    ktfmt_dry_run "$TMP/unchanged-paths" "$TMP/changed"
    [ "$KTFMT_STATUS" -eq 0 ] || die "ktfmt failed while scanning baselined files; $BASELINE left unchanged"
    # Re-hash what ktfmt still reports; only entries that match exactly stay.
    hash_entries "$TMP/changed" >"$TMP/still"
    grep_to "$TMP/next" -Fx -f "$TMP/unchanged" "$TMP/still"
  fi
  write_baseline "$TMP/next"
}

bootstrap_baseline() {
  local path
  if [ -e "$BASELINE" ] || [ -L "$BASELINE" ]; then
    die "--bootstrap-baseline refuses to overwrite existing $BASELINE"
  fi
  : >"$TMP/tracked"
  while IFS= read -r -d '' path; do
    require_safe_path "$path"
    if [ -f "$path" ] && [ ! -L "$path" ]; then printf '%s\n' "$path" >>"$TMP/tracked"; fi
  done < <(git ls-files -z -- '*.kt' '*.kts')
  require_jvm_safe_names "$TMP/tracked"
  : >"$TMP/changed"
  if [ -s "$TMP/tracked" ]; then
    ktfmt_dry_run "$TMP/tracked" "$TMP/changed"
    [ "$KTFMT_STATUS" -eq 0 ] || die "ktfmt failed while scanning tracked Kotlin files; no baseline written"
  fi
  hash_entries "$TMP/changed" >"$TMP/next"
  write_baseline "$TMP/next"
}

mode=check
files=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --baseline)
      [ "$#" -ge 2 ] || usage_error "--baseline needs a path"
      BASELINE="$2"
      shift 2
      ;;
    # No `--baseline=PATH` form: a tracked file may be named --baseline=X.kt.
    --write | --update-baseline | --bootstrap-baseline)
      [ "$mode" = check ] || usage_error "choose one of --write, --update-baseline, --bootstrap-baseline"
      mode="${1#--}"
      shift
      ;;
    --)
      shift
      files+=("$@")
      break
      ;;
    -*)
      # pre-commit passes file names after the options without a `--`.
      [ -f "$1" ] || usage_error "unknown option: $1"
      files+=("$1")
      shift
      ;;
    *)
      files+=("$1")
      shift
      ;;
  esac
done
[ -n "$BASELINE" ] || usage_error "--baseline needs a path"

require_repository_root
case "$mode" in
  check) check_files ${files[@]+"${files[@]}"} ;;
  write) write_files ${files[@]+"${files[@]}"} ;;
  update-baseline | bootstrap-baseline)
    [ "${#files[@]}" -eq 0 ] || usage_error "--$mode takes no file arguments"
    if [ "$mode" = update-baseline ]; then refresh_baseline; else bootstrap_baseline; fi
    ;;
esac
