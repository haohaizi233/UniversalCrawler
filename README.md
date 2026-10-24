# Git LFS compatibility archive

This orphan branch stores a copy of locally available Git LFS objects as
ordinary Git blobs split into files of at most 40 MiB. The archive preserves
the source repository's original commits and refs; it adds or updates only
`refs/heads/migration/lfs-archive` in the staging repository.

Gitee personal repositories do not provide native Git LFS hosting. This
archive lets a fresh clone restore the object cache so existing Git LFS
pointers can be checked out locally. It does not enable future native LFS
pushes. If new LFS objects become available locally, rerun the archive helper
on the staging repository before pushing the updated archive ref.

## Clone and restore in PowerShell

Run the clone with smudging disabled because the normal branches contain LFS
pointers and Gitee does not host their native LFS objects:

```powershell
$repoName = "UniversalCrawler"
$env:GIT_LFS_SKIP_SMUDGE = "1"
git clone "https://gitee.com/haohaizi554/$repoName.git" $repoName
Remove-Item Env:GIT_LFS_SKIP_SMUDGE
Set-Location $repoName
$restoreScript = Join-Path $env:TEMP "restore-gitee-lfs.py"
git show origin/migration/lfs-archive:restore.py | Set-Content -Encoding utf8 $restoreScript
python $restoreScript --checkout
```

The restore script can be run from anywhere inside the working clone. It
reconstructs `.git/lfs/objects`, validating each chunk's SHA-256, each full
object's SHA-256, and every recorded size. It leaves the working tree alone.
To ask Git LFS to populate pointer files after restoring, pass `--checkout`;
that option skips checkout when the working tree is dirty.

## Archive contents

`manifest.json` lists each local LFS object, its full size, and the SHA-256 and
size of every chunk. `restore.py` verifies those values before installing an
object in the clone's local Git LFS cache.
