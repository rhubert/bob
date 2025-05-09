#!/bin/bash -e
. ../../test-lib.sh 2>/dev/null || { echo "Must run in script directory!" ; exit 1 ; }

cleanup
rm -rf default.yaml

# setup local archive
trap 'rm -rf "${archiveDir}" "${srcDir}" default.yaml' EXIT
archiveDir=$(mktemp -d)
srcDir=$(mktemp -d)

# setup sources for checkouts
pushd $srcDir
mkdir -p git_scm
pushd git_scm
git init -b master .
git config user.email "bob@bob.bob"
git config user.name test
echo "Hello World!" > hello.txt
git add hello.txt
git commit -m "hello"
echo "foo" > foo.txt
git add foo.txt
git commit -m "foo"

GIT_URL=$(pwd)
GIT_COMMIT=$(git rev-parse HEAD)
popd #git_scm

mkdir -p tar
pushd tar
dd if=/dev/zero of=test.dat bs=1K count=1
tar cvf test.tar test.dat
TAR_URL=$(pwd)/test.tar
TAR_SHA1=$(sha1sum test.tar | cut -d ' ' -f1)
popd #tar
popd # srcDir

function run_src_upload_tests () {
  # cleanup
  rm -rf work $archiveDir/*

  cat > default.yaml <<EOF
archive:
  -
    name: "local"
    backend: file
    path: "$(mangle_path "$archiveDir")"
    flags: [src-download, src-upload]
    src-upload-filtered: True
EOF
  set -x
  run_bob dev root -DTAR_URL=${TAR_URL} -DTAR_SHA1=${TAR_SHA1} -DGIT_URL=${GIT_URL} -DGIT_COMMIT=${GIT_COMMIT} --upload

  rm dev -rf
  run_bob dev root -DTAR_URL=${TAR_URL} -DTAR_SHA1=${TAR_SHA1} -DGIT_URL=${GIT_URL} -DGIT_COMMIT=${GIT_COMMIT} --download yes
  expect_exist dev/src/git/1/workspace/hello.txt
  expect_not_exist dev/src/git/1/workspace/.git

}

run_src_upload_tests
