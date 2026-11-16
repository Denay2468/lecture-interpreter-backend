param([string]$RepoPath = (Get-Location).Path)
$ErrorActionPreference = 'Stop'
$bundlePath = Join-Path $PSScriptRoot 'Hugo-commit-accuracy.bundle'
if (-not (Test-Path -LiteralPath $bundlePath)) { throw 'Bundle missing. Extract the complete ZIP first.' }
Set-Location -LiteralPath $RepoPath
git rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0) { throw 'Open the project repository in the VS Code terminal first.' }
$dirty = git status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect repository.' }
if ($dirty) { throw 'There are tracked local changes. Commit or stash your work before applying. No files were replaced.' }
git bundle verify $bundlePath
if ($LASTEXITCODE -ne 0) { throw 'Prerequisite missing: run git fetch origin, then retry. This update requires the IT course update b161f02.' }
git switch Hugo-commit
if ($LASTEXITCODE -ne 0) { throw 'Cannot switch to Hugo-commit. Resolve the Git message above first.' }
git fetch $bundlePath refs/heads/Hugo-commit
if ($LASTEXITCODE -ne 0) { throw 'Bundle fetch failed. Merge was NOT attempted.' }
$incomingCommit = git rev-parse FETCH_HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot read imported commit.' }
git merge --ff-only $incomingCommit
if ($LASTEXITCODE -ne 0) { throw 'Fast-forward not possible. Keep your changes; do not force push. Share git status and git log --oneline -5.' }
git log -1 --oneline
Write-Host 'Update applied. Run the tests from README.txt, restart the backend, then push with: git push origin Hugo-commit'
