#!/usr/bin/env bash
# Build the tm1craft-client Windows drop-in zip (#12): a PyInstaller
# onedir of the capture CLI plus a README, so public users run it with
# no Python on the box.
#
# Usage: build-windows-exe.sh [TAG]
#   TAG defaults to "dev"; the release workflow passes the release tag
#   (bare version — .cz.toml tag_format is "$version"). Output:
#   dist/tm1craft-client-<TAG>-<plat>-x64.zip holding tm1craft-client/
#   (exe + _internal/ + README.txt).
#
# Runs identically in CI and locally. On windows (git-for-windows bash on
# GitHub runners) it produces the windows-x64 zip; on linux it builds the
# SAME spec as a linux onedir: PyInstaller is not cross-platform, the
# spec is, so the local build validates spec completeness (and is
# smoke-tested the same way).
set -euo pipefail

TAG="${1:-dev}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
spec_file="$repo_root/pyinstaller/tm1craft-client.spec"
templates_dir="$repo_root/.github/scripts/package-templates/exe"
dist_dir="$repo_root/dist"

case "$(uname -s)" in
    Linux*)
        platform="linux"
        exe_name="tm1craft-client"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        platform="windows"
        exe_name="tm1craft-client.exe"
        ;;
    *)
        echo "unsupported build platform: $(uname -s)" >&2
        exit 1
        ;;
esac
zip_path="$dist_dir/tm1craft-client-$TAG-$platform-x64.zip"

echo "==> syncing build environment"
cd "$repo_root"
uv sync --frozen

staging_dir="$(mktemp -d)"
trap 'rm -rf "$staging_dir" 2>/dev/null || true' EXIT

echo "==> building the onedir with pyinstaller"
uv run --no-sync pyinstaller --noconfirm \
    --distpath "$staging_dir" --workpath "$staging_dir/build" \
    "$spec_file"

package_root="$staging_dir/tm1craft-client"
binary="$package_root/$exe_name"
if [ ! -f "$binary" ]; then
    echo "pyinstaller produced no $binary" >&2
    exit 1
fi

echo "==> staging README"
cp "$templates_dir/README.txt" "$package_root/README.txt"

echo "==> smoke test (version, help, list over a staged registry)"
# No TM1 server is involved: --version proves the bundled dist metadata,
# --help the argparse surface, and list over a staged INI the whole
# config-resolution path — the same code dump runs before touching TM1.
"$binary" --version
"$binary" --help > /dev/null

registry_ini="$staging_dir/tm1-client.ini"
cat > "$registry_ini" <<'INI'
[Planning Sample]
base = http://tm1.example.com:12354/api/v1
user = admin
password =

[Sandbox]
base = http://tm1.example.com:12355/api/v1
user = test
password = secret

[service]
upload_url = http://craft.example.com
INI
listing="$("$binary" list --credentials "$registry_ini")"
printf '%s\n' "$listing"
printf '%s' "$listing" | grep -q 'Planning Sample' || {
    echo "smoke: list did not print the Planning Sample section" >&2
    exit 1
}
printf '%s' "$listing" | grep -q 'Sandbox' || {
    echo "smoke: list did not print the Sandbox section" >&2
    exit 1
}
printf '%s' "$listing" | grep -q 'secret' && {
    echo "smoke: list printed the password — must never render" >&2
    exit 1
}
echo "smoke: --version ok, --help ok, list ok over the staged registry (password never printed)"

echo "==> zipping $zip_path"
mkdir -p "$dist_dir"
rm -f "$zip_path"
if [ "$platform" = "windows" ]; then
    # git-for-windows ships no zip; System32 bsdtar writes zips (and with
    # forward-slash entry names, unlike PowerShell's Compress-Archive).
    (cd "$staging_dir" && /c/Windows/System32/tar.exe -a -cf "$zip_path" tm1craft-client)
else
    (cd "$staging_dir" && zip -qr "$zip_path" tm1craft-client)
fi

echo "built $zip_path"
(cd "$staging_dir" && find tm1craft-client -maxdepth 1 | sort)
