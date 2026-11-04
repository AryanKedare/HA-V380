#!/bin/sh
# Install the V380 Cloud 0.1.6 component; no account credentials needed.
set -eu

REPOSITORY="AryanKedare/HA-V380"
DEFAULT_REF="b6f355244744fc49da55ace51f0d35871eac318e"
config_dir="${HA_CONFIG_DIR:-/config}"
release_ref="$DEFAULT_REF"
ref_supplied=0
source_dir=""
download_dir=""
staging_dir=""
backup_path=""
target_dir=""
lock_dir=""
lock_owned=0
installed=0

usage() {
    cat <<'USAGE'
Usage: sh install.sh [--config-dir DIR] [--ref REF | --source DIR]

  --config-dir DIR  Home Assistant config directory (default: /config, or HA_CONFIG_DIR)
  --ref REF         Download a different GitHub branch, tag or commit
  --source DIR      Install from an extracted repository or component directory, offline
  -h, --help       Show this help

The default download is pinned to the 0.1.6 commit.
The config directory must already contain configuration.yaml.
An existing component is backed up under CONFIG_DIR/.v380-cloud-backups.
Home Assistant is not restarted automatically.
USAGE
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    result=$?
    trap - 0 HUP INT TERM
    if [ "$installed" -eq 0 ] && [ -n "$backup_path" ] &&
       [ ! -e "$target_dir" ] && [ ! -L "$target_dir" ]; then
        if mv -- "$backup_path" "$target_dir"; then
            printf 'Restored the previous V380 Cloud installation.\n' >&2
        else
            printf 'Restore failed. Your previous component is saved at: %s\n' "$backup_path" >&2
            result=1
        fi
    fi
    if [ -n "$staging_dir" ]; then rm -rf -- "$staging_dir" || true; fi
    if [ -n "$download_dir" ]; then rm -rf -- "$download_dir" || true; fi
    if [ "$lock_owned" -eq 1 ]; then rmdir -- "$lock_dir" || true; fi
    exit "$result"
}

trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config-dir|--ref|--source)
            [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value"
            case "$1" in
                --config-dir) config_dir=$2 ;;
                --ref) release_ref=$2; ref_supplied=1 ;;
                --source) source_dir=$2 ;;
            esac
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (use --help)" ;;
    esac
done

if [ -n "$source_dir" ] && [ "$ref_supplied" -eq 1 ]; then
    fail "Use either --source or --ref"
fi
case "$release_ref" in
    ''|-*|/*|*/|*..*|*//*|*[!a-zA-Z0-9._/-]*) fail "Invalid GitHub ref" ;;
esac
[ -d "$config_dir" ] || fail "Config directory does not exist: $config_dir"
config_dir=$(cd -- "$config_dir" && pwd -P)
[ -f "$config_dir/configuration.yaml" ] || fail "No configuration.yaml in $config_dir; set --config-dir to your Home Assistant config directory"

if [ -n "$source_dir" ]; then
    [ -d "$source_dir" ] || fail "Source directory does not exist: $source_dir"
    source_dir=$(cd -- "$source_dir" && pwd -P)
    if [ -d "$source_dir/custom_components/v380_cloud" ]; then
        source_component="$source_dir/custom_components/v380_cloud"
    else
        source_component="$source_dir"
    fi
else
    command -v unzip >/dev/null 2>&1 || fail "Install unzip, or use --source with an extracted repository"
    download_dir=$(mktemp -d "${TMPDIR:-/tmp}/v380-cloud-download.XXXXXX")
    archive="$download_dir/source.zip"
    archive_ref=$(printf '%s' "$release_ref" | sed 's|/|%2F|g')
    url="https://api.github.com/repos/$REPOSITORY/zipball/$archive_ref"
    printf 'Downloading V380 Cloud from %s at %s...\n' "$REPOSITORY" "$release_ref"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
            --proto '=https' --proto-redir '=https' --output "$archive" "$url" || fail "Download failed; the current installation was not changed"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$archive" "$url" || fail "Download failed; the current installation was not changed"
    else
        fail "Install curl or wget, or use --source with an extracted repository"
    fi
    mkdir "$download_dir/extracted"
    unzip -q "$archive" -d "$download_dir/extracted" || fail "Could not extract the GitHub archive"
    set -- "$download_dir/extracted"/*/custom_components/v380_cloud
    [ "$#" -eq 1 ] && [ -d "$1" ] || fail "Archive does not contain exactly one V380 Cloud component"
    source_component=$1
fi

for required in manifest.json __init__.py api.py live.py; do
    [ -f "$source_component/$required" ] || fail "Source component is missing $required"
done
grep -Eq '"domain"[[:space:]]*:[[:space:]]*"v380_cloud"' "$source_component/manifest.json" || fail "Source manifest is not for v380_cloud"
version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$source_component/manifest.json")
[ -n "$version" ] || fail "Source manifest has no version"

components_dir="$config_dir/custom_components"
mkdir -p -- "$components_dir"
target_dir="$components_dir/v380_cloud"
if [ -e "$target_dir" ] && [ ! -d "$target_dir" ]; then
    fail "The installation target is not a directory: $target_dir"
fi
lock_dir="$components_dir/.v380-cloud-install.lock"
mkdir -- "$lock_dir" 2>/dev/null || fail "Installer lock exists at $lock_dir; remove it only if no installer is running"
lock_owned=1

# Stage on the same filesystem so replacing the component is a directory rename.
staging_dir=$(mktemp -d "$components_dir/.v380-cloud-stage.XXXXXX")
cp -R -- "$source_component/." "$staging_dir/"
find "$staging_dir" -type d -name '__pycache__' -prune -exec rm -rf -- {} \;
find "$staging_dir" -type f -name '*.py[co]' -exec rm -f -- {} \;

if [ -e "$target_dir" ] || [ -L "$target_dir" ]; then
    backups_dir="$config_dir/.v380-cloud-backups"
    mkdir -p -- "$backups_dir"
    backup_dir=$(mktemp -d "$backups_dir/v380_cloud-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
    backup_path="$backup_dir/v380_cloud"
    mv -- "$target_dir" "$backup_path"
fi
mv -- "$staging_dir" "$target_dir" || fail "Could not install the staged component"
staging_dir=""
installed=1

printf '\nInstalled V380 Cloud %s in %s\n' "$version" "$target_dir"
if [ -n "$backup_path" ]; then printf 'Previous installation: %s\n' "$backup_path"; fi
printf 'Restart Home Assistant, then open the existing V380 Cloud integration.\n'
printf 'For a first install, add V380 Cloud under Settings > Devices & services.\n'
