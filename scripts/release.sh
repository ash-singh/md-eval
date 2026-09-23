#!/usr/bin/env bash
# Cut a release: set one version everywhere, tag it, then pin the plugins to the tagged commit.
#
#   scripts/release.sh 0.5.0            # two commits + tag v0.5.0, locally
#   git push origin main v0.5.0         # publish (the script prints this)
#
# Plugin users run the CLI from git+https://github.com/ash-singh/md-eval@<commit sha>.
# A commit sha, unlike a tag, is immutable, and uv serves it from its cache without a
# network fetch (about 0.2 s per hook call instead of 1.2 s for a tag). A commit can't
# contain its own sha, so a release is two commits:
#   1. "Release vX.Y.Z": versions bumped, tagged vX.Y.Z (the code plugin users run)
#   2. "Pin plugins to vX.Y.Z": every uvx source in plugins/ set to commit 1's sha
set -euo pipefail

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: scripts/release.sh X.Y.Z" >&2
  exit 2
fi
tag="v$version"
source_url='git+https://github.com/ash-singh/md-eval'

cd "$(git rev-parse --show-toplevel)"

[[ "$(git branch --show-current)" == main ]] || { echo "release from main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "tag $tag already exists" >&2
  exit 1
fi

# Tests must pass before anything is edited or tagged.
uv run pytest -q

# 1. One version number: the Python package and every plugin.
perl -pi -e "s/^version = \"[^\"]*\"/version = \"$version\"/" pyproject.toml
for manifest in plugins/*/.claude-plugin/plugin.json; do
  perl -pi -e "s/\"version\": \"[^\"]*\"/\"version\": \"$version\"/" "$manifest"
done
uv lock --quiet

claude plugin validate . >/dev/null
for dir in plugins/*/; do
  claude plugin validate "$dir" >/dev/null
done

git add -A
git commit -q -m "Release $tag"
git tag -a "$tag" -m "md-eval $tag"
sha=$(git rev-parse "$tag^{commit}")

# 2. Pin every uvx source in the plugins to the tagged commit.
for file in $(grep -rl "$source_url" plugins); do
  perl -pi -e "s#git\+https://github\.com/ash-singh/md-eval(\@[0-9A-Za-z.]+)?#$source_url\@$sha#g" "$file"
done
if grep -rn "$source_url" plugins | grep -v "@$sha"; then
  echo "unpinned or mismatched source above" >&2
  exit 1
fi
uv run pytest -q

git add -A
git commit -q -m "Pin plugins to $tag (${sha:0:7})"

echo "Tagged $tag at ${sha:0:7}; plugins pinned to it. Publish with:"
echo "  git push origin main $tag"
