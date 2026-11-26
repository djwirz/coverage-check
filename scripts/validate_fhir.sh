#!/usr/bin/env bash
# Validate results with HL7's official validator, for FHIR R4 (invariant 6). With no
# argument, examples made by the mapping code; with a directory under the repo, its files.
# Uses Java if there is one, and a Java container if not. Downloads go to .cache/.
set -euo pipefail

VERSION=7.0.7
SHA256=f9beb1ebb8f625aee3296200ca7b47aec67385550225a4d8797acd6b8be9c797
JAVA_IMAGE=eclipse-temurin:21-jre

cd "$(dirname "$0")/.."
jar=.cache/validator_cli-$VERSION.jar
mkdir -p .cache/fhir-packages
if [ ! -f "$jar" ]; then
  curl -sSfL -o "$jar.part" \
    "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/$VERSION/validator_cli.jar"
  mv "$jar.part" "$jar"
fi
echo "$SHA256  $jar" | shasum -a 256 -c - >/dev/null || {
  echo "$jar is not the pinned jar; delete it and run again" >&2
  exit 1
}


# -tx n/a: no terminology server, so the run doesn't depend on one and sends nothing out.
# Codes from outside the R4 packages therefore go unchecked. ISO 4217 is one, and a required
# one, so a fragment of it holding the one currency we write is supplied with -ig.
validate() {
  local args=(-jar "$jar" -version 4.0.1 -tx n/a -ig scripts/fhir/iso4217-usd.json "$1")
  if java -version >/dev/null 2>&1; then
    java -Duser.home="$PWD/.cache/fhir-packages" "${args[@]}"
  else
    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work "$JAVA_IMAGE" \
      java -Duser.home=/work/.cache/fhir-packages "${args[@]}"
  fi
}

if [ $# -gt 0 ]; then
  validate "$1"
  echo "Every result in $1 is valid R4."
  exit 0
fi

rm -rf .cache/fhir-examples
uv run python scripts/fhir_examples.py .cache/fhir-examples/good .cache/fhir-examples/broken
validate .cache/fhir-examples/good

if validate .cache/fhir-examples/broken >.cache/fhir-examples/broken.log 2>&1; then
  echo "The broken example passed: the validator isn't checking anything." >&2
  exit 1
fi
grep -q "insurer" .cache/fhir-examples/broken.log || {
  cat .cache/fhir-examples/broken.log >&2
  echo "The broken example failed, but not for the missing insurer." >&2
  exit 1
}
echo "Every example is valid R4, and the broken one is refused."
