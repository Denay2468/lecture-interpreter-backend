$ErrorActionPreference = 'Stop'
function Run-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw 'Git stopped. Resolve the message above before continuing.' }
}
# Run while the terminal's current directory is your existing repository.
Run-Git rev-parse --show-toplevel
$bundlePath = Join-Path $PSScriptRoot 'Hugo-commit-it-courses.bundle'
if (-not (Test-Path -LiteralPath $bundlePath)) { throw 'Extract the entire ZIP first; the bundle must be next to this script.' }
$changes = & git status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect working tree.' }
if ($changes) { throw 'Working tree has local changes. Commit or stash them first; this script does not overwrite or remove them.' }
Run-Git bundle verify $bundlePath
Run-Git fetch $bundlePath refs/heads/Hugo-commit
$incomingCommit = & git rev-parse FETCH_HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve imported commit.' }
& git show-ref --verify --quiet refs/heads/Hugo-commit
if ($LASTEXITCODE -eq 0) {
    Run-Git switch Hugo-commit
    Run-Git merge --ff-only $incomingCommit
} else {
    Run-Git switch -c Hugo-commit $incomingCommit
}
Run-Git log -1 --oneline
Write-Host 'Update applied locally. To publish to GitHub: git push origin Hugo-commit'
