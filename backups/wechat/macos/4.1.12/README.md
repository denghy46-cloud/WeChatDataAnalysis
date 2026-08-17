# WeChat 4.1.12 macOS backup

This directory preserves the packages needed to reproduce the WeChat version used by WCDA on the capture Mac.

## Packages

| File | Build | Origin | Architectures | Use |
| --- | --- | --- | --- | --- |
| `WeChatMac-4.1.12-build269341-universal-official.dmg` | 269341 | Original Tencent installer found on the host | arm64 + x86_64 | Clean official 4.1.12 installation |
| `WeChatMac-4.1.12-build269365-universal-snapshot.dmg` | 269365 | Snapshot of the installed, official-signature-valid app | arm64 + x86_64 | Exact reproduction of the build validated with WCDA |

Both packages are Universal 2 and therefore support Apple Silicon directly. The 269365 DMG container was created locally, but the `WeChat.app` inside was not modified or re-signed and retains Tencent's Developer ID signature (`5A4RE8SF68`).

These DMGs contain only the application. They do not contain a WeChat account, chat history, database keys, WCDA output, or the WeChat container under `~/Library/Containers`.

## Verify before use

From this directory, run:

```sh
./verify-backups.sh
```

The expected hashes and detailed provenance are recorded in `SHA256SUMS` and `manifest.json`.

## Restore the validated build

1. Fully quit WeChat.
2. Preserve the existing `/Applications/WeChat.app` if it contains a different required version.
3. Mount `WeChatMac-4.1.12-build269365-universal-snapshot.dmg`.
4. Drag `WeChat.app` to `/Applications`.
5. Verify the restored app before launching it:

```sh
codesign --verify --deep --strict /Applications/WeChat.app
defaults read /Applications/WeChat.app/Contents/Info CFBundleShortVersionString
defaults read /Applications/WeChat.app/Contents/Info CFBundleVersion
lipo -archs /Applications/WeChat.app/Contents/MacOS/WeChat
```

The expected output is version `4.1.12`, build `269365`, with `arm64 x86_64` architectures. Then open WCDA and confirm that **Settings → Updates → Prevent WeChat automatic upgrades** is enabled before normal use.

## Git storage policy

The DMGs are intentionally ignored by normal Git because each exceeds GitHub's 100 MiB per-file limit. The manifest, hashes, and instructions are tracked. For another Mac, transfer the DMGs from this local backup directory using trusted private storage and verify `SHA256SUMS` after transfer. Do not distribute the packages publicly without confirming Tencent's redistribution terms.
