Brelanbar IT course update
Commit: b161f02
Based on main: 09084045376621a405bc7bba5706e79fcd5b2ab9
Target branch: Hugo-commit

GitHub publishing was blocked: connector 403 (integration permission) and local Git had no login credentials. This bundle contains the tested commit plus main changes missing from the old Hugo-commit branch. It does not change remote main.

1. Extract this ZIP outside your repository (e.g. Downloads/BLB-IT).
2. Open PowerShell in your existing lecture-interpreter repository.
3. Run the script using its absolute path, for example:
   & "$HOME\Downloads\BLB-IT\Apply-IT-Update.ps1"
4. If PowerShell execution policy blocks it, use manual commands below rather than changing your system policy.
5. The script stops on local changes or divergent branches; it does not reset, force push or erase files.
6. When import succeeds, run:
   git push origin Hugo-commit

Manual alternative (replace the bundle path with the real extracted path):
   git status
   git bundle verify "C:\full\path\Hugo-commit-it-courses.bundle"
   git fetch "C:\full\path\Hugo-commit-it-courses.bundle" refs/heads/Hugo-commit
   git switch Hugo-commit
   git merge --ff-only FETCH_HEAD
   git push origin Hugo-commit
Stop if any command fails; do not continue to merge an older FETCH_HEAD after fetch failure.

Restart the backend and frontend. On the lecturer page choose Indonesian, then the IT course. Review the seed terms and add overrides. Full instructions: docs/it-course-glossary.md in the updated repository.

Tests: 43 offline backend tests passed; new glossary UI component tests and existing frontend unit tests passed. Full UI smoke suite has one pre-existing admin lecture-history failure reproduced on untouched main. Live provider accuracy / real classroom / Bluetooth / database integration not tested here. No API keys included.
