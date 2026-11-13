#!/usr/bin/env bash
# Shared detekt pre-commit hook. It adapts pre-commit's file arguments to
# detekt-cli's comma-separated --input option and adds the caller's
# configuration and baseline.
#
# pre-commit runs this file from its clone of the hook repository, but with
# the working directory at the caller's repository root. Every path below is
# therefore relative to the caller, and java and detekt-cli come from the
# caller's mise configuration, never from this repository.
set -euo pipefail

die() {
  echo "detekt hook: $*" >&2
  exit 1
}

if [ -z "${DETEKT_HOOK_IN_MISE-}" ]; then
  command -v mise >/dev/null 2>&1 ||
    die "mise is not on PATH; java and detekt-cli come from the project's mise configuration"
  # Re-run inside the caller's mise environment so java and detekt-cli are on
  # PATH. mise resolves the configuration from the current directory, which
  # pre-commit sets to the caller's repository root.
  DETEKT_HOOK_IN_MISE=1 exec mise exec -- bash "${BASH_SOURCE[0]}" "$@"
fi

CONFIG='config/detekt.yml'
BASELINE='config/detekt-baseline.xml'
BASELINE_GIVEN=''
BUILD_UPON_DEFAULT_CONFIG=1
PLUGINS=''
JVM_TARGET=''
LANGUAGE_VERSION=''
USAGE='usage: detekt.sh [--config PATHS] [--baseline PATH] [--no-build-upon-default-config] [--plugins JARS]
                 [--jvm-target VERSION] [--language-version VERSION] [FILE...]'

usage_error() {
  echo "detekt hook: $1" >&2
  echo "$USAGE" >&2
  exit 2
}

# The configuration, the baseline and the file arguments are relative to the
# repository root, so a run from a subdirectory would not find them.
require_repository_root() {
  local prefix
  prefix="$(git rev-parse --show-prefix 2>/dev/null)" || die "not inside a git work tree"
  [ -z "$prefix" ] || die "run from the repository root, not $prefix"
}

# detekt takes several paths in one value, separated by commas or semicolons.
# Fail on an entry of $2 that is not a file, naming it as a $1.
require_files() {
  local what="$1" entry entries
  IFS=',;' read -r -a entries <<<"$2"
  for entry in ${entries[@]+"${entries[@]}"}; do
    [ -z "$entry" ] || [ -f "$entry" ] || die "no $what at $entry"
  done
}

files=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    # No `--option=VALUE` form: a tracked file may be named --config=X.kt.
    --config | --baseline | --plugins | --jvm-target | --language-version)
      { [ "$#" -ge 2 ] && [ -n "$2" ]; } || usage_error "$1 needs a value"
      # detekt-cli would take it as the value all the same and fail on it.
      case "$2" in
        -*) usage_error "$1 needs a value, not the option $2 (write a path that starts with - as ./-...)" ;;
      esac
      case "$1" in
        --config | --plugins)
          case "$2" in
            [,\;]* | *[,\;] | *[,\;][,\;]*) usage_error "$1 has an empty entry: $2" ;;
          esac
          ;;
      esac
      case "$1" in
        --config) CONFIG="$2" ;;
        --baseline) BASELINE="$2" BASELINE_GIVEN=1 ;;
        --plugins) PLUGINS="$2" ;;
        --jvm-target) JVM_TARGET="$2" ;;
        --language-version) LANGUAGE_VERSION="$2" ;;
      esac
      shift 2
      ;;
    --no-build-upon-default-config)
      BUILD_UPON_DEFAULT_CONFIG=''
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

require_repository_root
require_files 'detekt configuration' "$CONFIG"
[ -z "$PLUGINS" ] || require_files 'detekt plugin jar' "$PLUGINS"
if [ ! -f "$BASELINE" ]; then
  # The default baseline is optional: a repository without legacy findings
  # needs none. A baseline the caller named must exist.
  [ -z "$BASELINE_GIVEN" ] || die "no detekt baseline at $BASELINE"
  BASELINE=''
fi

inputs=()
for file in ${files[@]+"${files[@]}"}; do
  case "$file" in
    *[,\;]*) die "detekt cannot accept commas or semicolons in paths: $file" ;;
  esac
  [ -f "$file" ] || die "no such file: $file"
  # A value starting with "-" could be read as an option.
  case "$file" in
    -*) inputs+=("./$file") ;;
    *) inputs+=("$file") ;;
  esac
done
# Without --input detekt would scan the whole working directory.
[ "${#inputs[@]}" -gt 0 ] || exit 0

# Whichever detekt-cli is first on PATH inside `mise exec` runs. Unlike the
# ktfmt hook, this one does not ask mise whether the tool is declared: it does
# not know which mise tool the caller gets detekt-cli from.
command -v detekt-cli >/dev/null 2>&1 ||
  die "detekt-cli is not on PATH inside \`mise exec\`; add a tool that provides it under [tools] in mise.toml (see the hook's README)"

detekt=(detekt-cli)
[ -z "$BUILD_UPON_DEFAULT_CONFIG" ] || detekt+=(--build-upon-default-config)
detekt+=(--config "$CONFIG")
# The hook checks whole changed files. The baseline records the findings a
# file already had, so a small edit does not inherit all of them.
[ -z "$BASELINE" ] || detekt+=(--baseline "$BASELINE")
[ -z "$PLUGINS" ] || detekt+=(--plugins "$PLUGINS")
[ -z "$JVM_TARGET" ] || detekt+=(--jvm-target "$JVM_TARGET")
[ -z "$LANGUAGE_VERSION" ] || detekt+=(--language-version "$LANGUAGE_VERSION")
IFS=,
detekt+=(--input "${inputs[*]}")
exec "${detekt[@]}"
