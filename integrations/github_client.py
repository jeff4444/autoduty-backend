"""GitHub integration — create branches and PRs with AI-generated fixes.

Supports multi-file commits from the agent's file_edits list.
"""

from github import Github, GithubException, InputGitTreeElement

from config import Config
from models.incident import Incident
from utils.logger import get_logger

log = get_logger("github")


def create_fix_pr(incident: Incident) -> str:
    """Create a GitHub PR with the fix for the given incident.

    Supports multi-file edits via incident.file_edits. Falls back to
    single-file mode using incident.fixed_code for backward compatibility.

    Args:
        incident: An Incident model instance with file_edits or fixed_code populated.

    Returns:
        The URL of the created pull request.
    """
    if not Config.GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN not configured")
    if not Config.GITHUB_REPO:
        raise ValueError("GITHUB_REPO not configured (expected 'owner/repo')")
    if not incident.file_edits and not incident.fixed_code:
        raise ValueError("No fix code available for this incident")

    g = Github(Config.GITHUB_TOKEN)
    repo = g.get_repo(Config.GITHUB_REPO)

    branch_name = f"autoduty/fix-incident-{incident.id}"

    log.info("Creating PR branch '%s'", branch_name)

    # Get the default branch SHA
    base_branch = repo.get_branch(incident.branch or "main")
    base_sha = base_branch.commit.sha

    # Create the new branch
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
        log.info("Branch '%s' created", branch_name)
    except GithubException as e:
        if e.status == 422:
            log.warning("Branch '%s' already exists, updating it", branch_name)
        else:
            raise

    # Commit files
    if incident.file_edits:
        # Multi-file commit using the Git tree API
        _commit_multiple_files(repo, branch_name, incident)
    else:
        # Legacy single-file commit
        _commit_single_file(repo, branch_name, incident)

    # Build the PR body
    pr_body = _build_pr_body(incident)

    # Create the PR
    pr = repo.create_pull(
        title=f"[AutoDuty] Fix: {incident.root_cause[:80] if incident.root_cause else 'Unknown'}",
        body=pr_body,
        head=branch_name,
        base=incident.branch or "main",
    )

    log.info("PR #%s created: %s", pr.number, pr.html_url)
    return pr.html_url


def _commit_multiple_files(repo, branch_name: str, incident: Incident) -> None:
    """Commit multiple file changes in a single git commit using the tree API."""
    # Get the current commit on the branch
    ref = repo.get_git_ref(f"heads/{branch_name}")
    base_commit = repo.get_git_commit(ref.object.sha)
    base_tree = base_commit.tree

    # Build tree elements for each file edit
    tree_elements = []
    for edit in incident.file_edits:
        # Create a blob for the new content
        blob = repo.create_git_blob(edit.new_content, "utf-8")
        tree_elements.append(
            InputGitTreeElement(
                path=edit.file_path,
                mode="100644",
                type="blob",
                sha=blob.sha,
            )
        )
        log.info("  Staging file: %s", edit.file_path)

    # Create a new tree with the changes
    new_tree = repo.create_git_tree(tree_elements, base_tree)

    # Build commit message
    files_list = ", ".join(e.file_path for e in incident.file_edits)
    commit_message = (
        f"fix: AutoDuty fix for incident {incident.id}\n\n"
        f"{incident.fix_description or 'Automated fix'}\n\n"
        f"Files changed: {files_list}"
    )

    # Create the commit
    new_commit = repo.create_git_commit(commit_message, new_tree, [base_commit])

    # Update the branch reference
    ref.edit(new_commit.sha)
    log.info("Multi-file commit created: %s (%d files)", new_commit.sha[:8], len(incident.file_edits))


def _commit_single_file(repo, branch_name: str, incident: Incident) -> None:
    """Legacy single-file commit for backward compatibility."""
    affected_file = incident.affected_file or incident.source_file

    try:
        file_contents = repo.get_contents(affected_file, ref=branch_name)
        file_sha = file_contents.sha
        repo.update_file(
            path=affected_file,
            message=f"fix: AutoDuty fix for incident {incident.id}\n\n{incident.fix_description}",
            content=incident.fixed_code,
            sha=file_sha,
            branch=branch_name,
        )
    except GithubException:
        repo.create_file(
            path=affected_file,
            message=f"fix: AutoDuty fix for incident {incident.id}\n\n{incident.fix_description}",
            content=incident.fixed_code,
            branch=branch_name,
        )


def _build_pr_body(incident: Incident) -> str:
    """Build the pull request body with diffs and sandbox results."""
    # File changes section
    files_section = ""
    if incident.file_edits:
        file_diffs = []
        for edit in incident.file_edits:
            file_diffs.append(f"""
<details>
<summary><code>{edit.file_path}</code></summary>

```diff
{edit.unified_diff}
```
</details>
""")
        files_section = f"""
### Changes ({len(incident.file_edits)} file{'s' if len(incident.file_edits) > 1 else ''})
{"".join(file_diffs)}
"""
    elif incident.affected_file:
        files_section = f"""
### Affected File
`{incident.affected_file}`
"""

    # Sandbox section
    sandbox_status = ""
    if incident.sandbox_reproduced is not None:
        reproduced = "PASS" if incident.sandbox_reproduced else "SKIP"
        verified = "PASS" if incident.sandbox_fix_verified else "FAIL"
        sandbox_status = f"""
## Sandbox Verification
| Check | Result |
|-------|--------|
| Bug Reproduced | {reproduced} |
| Fix Verified | {verified} |

<details>
<summary>Sandbox Terminal Log</summary>

```
{incident.sandbox_output or 'N/A'}
```
</details>
"""

    return f"""## AutoDuty Automated Fix

**Incident ID:** `{incident.id}`
**Root Cause:** {incident.root_cause}
**Fix:** {incident.fix_description}

{files_section}
{sandbox_status}

---
*This PR was automatically generated by [AutoDuty](https://github.com/autoduty) — your AI SRE.*
"""
