#!/usr/bin/env bash
# Устанавливает статический ffmpeg в bin/ffmpeg (для Render: в окружении Python ffmpeg нет).
set -euo pipefail
mkdir -p bin
if [ -x bin/ffmpeg ]; then
  echo "ffmpeg уже установлен"; exit 0
fi
tmp=$(mktemp -d)
curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o "$tmp/ffmpeg.tar.xz"
tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp"
cp "$tmp"/ffmpeg-*-amd64-static/ffmpeg bin/ffmpeg
chmod +x bin/ffmpeg
rm -rf "$tmp"
bin/ffmpeg -version | head -1
