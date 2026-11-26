#!/usr/bin/env bash
# Fetch the pinned kind and kubectl into .cache/bin, checked against their published sha256.
# Sourced by env.sh. Nothing is installed outside the repo (ADR 0011).
KIND_VERSION=v0.33.0
KUBECTL_VERSION=v1.37.3

fetch_tool() { # url, sha256, destination
  if [ ! -x "$3" ]; then
    echo "Downloading $1" >&2
    curl -sSfL -o "$3.part" "$1"
    echo "$2  $3.part" | shasum -a 256 -c - >/dev/null || {
      echo "checksum mismatch for $1" >&2
      rm -f "$3.part"
      return 1
    }
    chmod +x "$3.part"
    mv "$3.part" "$3"
  fi
}

fetch_tools() {
  local os arch
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  case $(uname -m) in
    x86_64) arch=amd64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) echo "unsupported machine: $(uname -m)" >&2; return 1 ;;
  esac
  case $os-$arch in
  darwin-amd64) KIND_SHA256=5a99f26f57246dc9319dd294803313197a0f34d33c525b3ea8b655db5916ece0
    KUBECTL_SHA256=82db1bc2b466e9f46ed36d609d8c10523a425664e6e648a666df252b9502fe9e ;;
  darwin-arm64) KIND_SHA256=0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f
    KUBECTL_SHA256=7ac1234e7731e902e5ab2153e2ed29bbe7abddd7b0be5ffd94afb7df47da8f27 ;;
  linux-amd64) KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
    KUBECTL_SHA256=9a1b55af2795b25dd288dbef62387db9b5530087ab918d28c6ad067f2e3cb07c ;;
  *) echo "no pinned tools for $os-$arch" >&2; return 1 ;;
  esac
  mkdir -p "$ROOT/.cache/bin"
  fetch_tool "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$os-$arch" \
    "$KIND_SHA256" "$KIND"
  fetch_tool "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl" \
    "$KUBECTL_SHA256" "$KUBECTL"
}
