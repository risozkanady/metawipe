#!/usr/bin/env bash
# Prints the Homebrew formula for release $1 (e.g. 0.3.1b1) from the SHA256SUMS file $2 of that release.
set -euo pipefail
version=$1 sums=$2
repo=${GITHUB_REPOSITORY:-risozkanady/metawipe}
sha() { awk -v f="metawipe-$version-$1.tar.gz" '$2 == f {print $1}' "$sums"; }
url() { echo "https://github.com/$repo/releases/download/v$version/metawipe-$version-$1.tar.gz"; }
cat <<RUBY
class Metawipe < Formula
  desc "Strip metadata from images, HEIC, videos, PDFs and Office/OpenDocument files"
  homepage "https://github.com/$repo"
  version "$version"
  license "MIT"

  on_macos do
    on_arm do
      url "$(url macos-arm64)"
      sha256 "$(sha macos-arm64)"
    end
    on_intel do
      url "$(url macos-x86_64)"
      sha256 "$(sha macos-x86_64)"
    end
  end

  on_linux do
    on_intel do
      url "$(url linux-x86_64)"
      sha256 "$(sha linux-x86_64)"
    end
  end

  def install
    bin.install "metawipe"
  end

  def caveats
    "Videos need ffmpeg: brew install ffmpeg"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/metawipe --version")
  end
end
RUBY
