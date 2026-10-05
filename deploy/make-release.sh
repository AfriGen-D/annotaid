#!/usr/bin/env bash
# Build a release tarball to hand to the server's administrator.
#
#   deploy/make-release.sh            # Python 3.9 wheels (Rocky 9 default)
#   PYVER=3.11 deploy/make-release.sh # if the server uses python3.11
#
# Output: dist/annotaid-<version>-<commit>.tar.gz containing
#   annotaid-<version>/          the app (code, static files, starter config, deploy/)
#   annotaid-<version>/wheels/   jsonschema + its dependencies for linux x86_64,
#                                so the server can install without reaching PyPI
#
# Built from the COMMITTED tree (git archive), so uncommitted edits never ship.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
PYVER="${PYVER:-3.9}"
VERSION="$(python3 -c 'import re;print(re.search(r"VERSION = \"(.+?)\"", open("server/__init__.py").read()).group(1))')"
COMMIT="$(git rev-parse --short HEAD)"
NAME="annotaid-${VERSION}"
OUT="dist/${NAME}-${COMMIT}.tar.gz"

if [ -n "$(git status --porcelain -- server static config run.py requirements.txt deploy)" ]; then
  echo "warning: uncommitted changes exist; they are NOT included (building from HEAD ${COMMIT})" >&2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git archive --format=tar --prefix="${NAME}/" HEAD -- \
  server static config run.py requirements.txt deploy README.md docs/api-v2.md \
  | tar -x -C "$WORK"
echo "${VERSION} ${COMMIT} $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$WORK/${NAME}/RELEASE"

python3 -m pip download --quiet \
  --dest "$WORK/${NAME}/wheels" \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
  --python-version "$PYVER" --implementation cp \
  -r requirements.txt

mkdir -p dist
tar -czf "$OUT" -C "$WORK" "$NAME"
echo "built $OUT  (python ${PYVER} wheels: $(ls "$WORK/${NAME}/wheels" | wc -l | tr -d ' '))"
