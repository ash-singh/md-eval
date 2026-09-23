#!/usr/bin/env bash
# Cut a release: set one version everywhere, pin the plugins to its tag, commit and tag.
#
#   scripts/release.sh 0.2.0            # commit + tag v0.2.0 locally
#   git push origin main v0.2.0         # publish (the script prints this)
#
# Plugin users run the CLI from git+https://github.com/ash-singh/md-eval@v<version>,
# so pushes to main don't reach them until a release bumps the plugin versions.
set -euo pipefail

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: scripts/release.sh X.Y.Z" >&2
  exit 2
fi
tag="v$version"

cd "$(git rev-parse --show-toplevel)"

[[ "$(git branch --show-current)" == main ]] || { echo "release from main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "tag $tag already exists" >&2
  exit 1
fi

# One version number: the Python package and both plugins.
perl -pi -e "s/^version = \"[^\"]*\"/version = \"$version\"/" pyproject.toml
for manifest in plugins/*/.claude-plugin/plugin.json; do
  perl -pi -e "s/\"version\": \"[^\"]*\"/\"version\": \"$version\"/" "$manifest"
done

# Pin every uvx source in the plugins to this release's tag.
pinned=$(grep -rl 'git+https://github.com/ash-singh/md-eval' plugins)
for file in $pinned; do
  perl -pi -e "s#git\+https://github\.com/ash-singh/md-eval(\@v[0-9.]+)?#git+https://github.com/ash-singh/md-eval\@$tag#g" "$file"
done

uv lock --quiet

# Check the manifests and that every pin points at this tag.
claude plugin validate . >/dev/null
for dir in plugins/*/; do
  claude plugin validate "$dir" >/dev/null
done
if grep -rn 'git+https://github.com/ash-singh/md-eval' plugins | grep -v "@$tag"; then
  echo "unpinned or mismatched source above" >&2
  exit 1
fi

git add -A
git commit -q -m "Release $tag"
git tag -a "$tag" -m "md-eval $tag"

echo "Tagged $tag. Publish with:"
echo "  git push origin main $tag"
