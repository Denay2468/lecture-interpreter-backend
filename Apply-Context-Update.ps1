$ErrorActionPreference = 'Stop'
$bundle = Join-Path $PSScriptRoot 'Hugo-commit-context.bundle'
git rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0) { throw 'Open the VS Code terminal in your existing Git repository.' }
$changes = git status --porcelain --untracked-files=no
if ($changes) { throw 'Commit or stash your tracked local changes first. Nothing has been overwritten.' }
git bundle verify $bundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed. This update requires the IT course update b161f02.' }
git switch Hugo-commit
if ($LASTEXITCODE -ne 0) { throw 'Could not switch to Hugo-commit.' }
git fetch $bundle Hugo-commit
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed; stopping before merge.' }
$target = git rev-parse FETCH_HEAD
git merge --ff-only $target
if ($LASTEXITCODE -ne 0) { throw 'Branches diverged. Keep your work and resolve the merge before continuing.' }
git log -1 --oneline
Write-Host 'Update applied. Install requirements, run tests, restart the backend, then push origin Hugo-commit. See README.txt.'
