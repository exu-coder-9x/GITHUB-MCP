"""GitHub MCP MCP server (stdio transport)."""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any
from urllib.parse import quote
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).resolve().with_name(".env"))

API_BASE = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
API_VERSION = "2022-11-28"
TIMEOUT = float(os.getenv("GITHUB_TIMEOUT", "30"))
mcp = FastMCP("GitHub MCP")


class GitHubAPIError(RuntimeError):
    pass


def _token() -> str:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "GITHUB_TOKEN is not set. Add a fine-grained GitHub token to the local .env file "
            "or the OpenCode MCP environment configuration."
        )
    return token


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
    url = f"{API_BASE}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_token()}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "EXU-GitHub-MCP/1.0",
    }
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            response = client.request(method, url, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        raise GitHubAPIError(f"GitHub network request failed: {exc.__class__.__name__}") from exc

    if response.status_code == 204 or not response.content:
        return {"ok": True, "status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text[:500]}
    if response.is_error:
        message = data.get("message", "Unknown GitHub API error") if isinstance(data, dict) else "GitHub API error"
        details = data.get("errors") if isinstance(data, dict) else None
        raise GitHubAPIError(json.dumps({"status": response.status_code, "message": message, "errors": details}, ensure_ascii=False))
    return data


def _repo(owner: str, repo: str) -> str:
    owner, repo = owner.strip(), repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid owner or repository name.")
    return f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _items(data: Any) -> list:
    return data if isinstance(data, list) else [data]


@mcp.tool()
def github_account() -> dict:
    """Show the authenticated GitHub account and token scopes available to it."""
    return _request("GET", "/user")


@mcp.tool()
def list_repositories(owner: str = "", visibility: str = "all", sort: str = "updated", per_page: int = 30, page: int = 1) -> list:
    """List repositories. Leave owner empty for repositories accessible to the authenticated account."""
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    if visibility not in {"all", "public", "private"}:
        raise ValueError("visibility must be all, public, or private")
    if sort not in {"created", "updated", "pushed", "full_name"}:
        raise ValueError("Unsupported sort value")
    if owner.strip():
        return _request("GET", f"/users/{quote(owner.strip(), safe='')}/repos", params={"type": visibility, "sort": sort, "per_page": per_page, "page": page})
    return _request("GET", "/user/repos", params={"visibility": visibility, "sort": sort, "per_page": per_page, "page": page, "affiliation": "owner,collaborator,organization_member"})


@mcp.tool()
def get_repository(owner: str, repo: str) -> dict:
    """Get details for a repository."""
    return _request("GET", _repo(owner, repo))


@mcp.tool()
def create_repository(name: str, description: str = "", private: bool = True, auto_init: bool = True, homepage: str = "") -> dict:
    """Create a repository under the authenticated user. Private by default."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise ValueError("Repository name must be 1-100 characters and contain only letters, digits, dot, underscore, or hyphen")
    body: dict[str, Any] = {"name": name, "description": description[:350], "private": private, "auto_init": auto_init}
    if homepage:
        body["homepage"] = homepage
    return _request("POST", "/user/repos", body=body)


@mcp.tool()
def fork_repository(owner: str, repo: str, organization: str = "") -> dict:
    """Fork a repository you are allowed to fork. Optionally choose an organization you belong to."""
    body = {"organization": organization} if organization.strip() else {}
    return _request("POST", f"/{_repo(owner, repo)}".replace("/repos/", "/repos/", 1) + "/forks", body=body)


@mcp.tool()
def bulk_fork_repositories(repositories_json: str, organization: str = "", confirm: bool = False) -> dict:
    """Fork up to 20 repositories. repositories_json is a JSON array of {owner, repo}; set confirm=true to execute."""
    try:
        repositories = json.loads(repositories_json)
    except json.JSONDecodeError as exc:
        raise ValueError("repositories_json must be a valid JSON array") from exc
    if not isinstance(repositories, list) or not 1 <= len(repositories) <= 20:
        raise ValueError("Provide a JSON array containing 1-20 repositories")
    normalized = [{"owner": str(x.get("owner", "")), "repo": str(x.get("repo", ""))} for x in repositories if isinstance(x, dict)]
    if len(normalized) != len(repositories):
        raise ValueError("Each repository must be an object with owner and repo fields")
    if not confirm:
        return {"confirmation_required": True, "message": "No forks were created. Review this list, then call again with confirm=true.", "repositories": normalized, "organization": organization}
    results = []
    for item in normalized:
        try:
            body = {"organization": organization} if organization.strip() else {}
            result = _request("POST", f"{_repo(item['owner'], item['repo'])}/forks", body=body)
            results.append({"repository": f"{item['owner']}/{item['repo']}", "ok": True, "result": result})
        except Exception as exc:
            results.append({"repository": f"{item['owner']}/{item['repo']}", "ok": False, "error": str(exc)})
    return {"results": results}


@mcp.tool()
def update_repository(owner: str, repo: str, description: str | None = None, homepage: str | None = None, private: bool | None = None, archived: bool | None = None, default_branch: str | None = None) -> dict:
    """Update repository metadata/settings. Only supplied fields are changed."""
    body: dict[str, Any] = {}
    for key, value in {"description": description, "homepage": homepage, "private": private, "archived": archived, "default_branch": default_branch}.items():
        if value is not None:
            body[key] = value
    if not body:
        raise ValueError("Supply at least one field to update")
    return _request("PATCH", _repo(owner, repo), body=body)


@mcp.tool()
def delete_repository(owner: str, repo: str, confirmation: str) -> dict:
    """Permanently delete a repository. Requires confirmation exactly 'DELETE owner/repo'. This cannot be undone."""
    expected = f"DELETE {owner}/{repo}"
    if confirmation != expected:
        raise ValueError(f"Deletion blocked. To confirm, pass confirmation exactly as: {expected}")
    return _request("DELETE", _repo(owner, repo))


@mcp.tool()
def list_branches(owner: str, repo: str, per_page: int = 30, page: int = 1) -> list:
    """List repository branches."""
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    return _request("GET", f"/{_repo(owner, repo)}/branches", params={"per_page": per_page, "page": page})


@mcp.tool()
def create_branch(owner: str, repo: str, branch_name: str, source_branch: str = "") -> dict:
    """Create a branch from a source branch, or from the repository's default branch if omitted."""
    if not branch_name or len(branch_name) > 240 or branch_name.startswith("-") or ".." in branch_name or " ".join(branch_name.split()) != branch_name:
        raise ValueError("Invalid branch name")
    base = source_branch or _request("GET", _repo(owner, repo)).get("default_branch", "main")
    ref = _request("GET", f"/{_repo(owner, repo)}/git/ref/heads/{quote(base, safe='/')}")
    sha = ref["object"]["sha"]
    return _request("POST", f"/{_repo(owner, repo)}/git/refs", body={"ref": f"refs/heads/{branch_name}", "sha": sha})


@mcp.tool()
def list_issues(owner: str, repo: str, state: str = "open", per_page: int = 30, page: int = 1) -> list:
    """List repository issues. GitHub's endpoint may also include pull requests in the result."""
    if state not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    return _request("GET", f"/{_repo(owner, repo)}/issues", params={"state": state, "per_page": per_page, "page": page})


@mcp.tool()
def create_issue(owner: str, repo: str, title: str, body: str = "", labels_json: str = "[]", assignees_json: str = "[]") -> dict:
    """Create an issue. labels_json and assignees_json must be JSON arrays of strings."""
    if not title.strip():
        raise ValueError("Issue title cannot be empty")
    try:
        labels, assignees = json.loads(labels_json), json.loads(assignees_json)
    except json.JSONDecodeError as exc:
        raise ValueError("labels_json and assignees_json must be valid JSON arrays") from exc
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels) or not isinstance(assignees, list) or not all(isinstance(x, str) for x in assignees):
        raise ValueError("labels_json and assignees_json must be arrays of strings")
    return _request("POST", f"/{_repo(owner, repo)}/issues", body={"title": title.strip(), "body": body, "labels": labels, "assignees": assignees})


@mcp.tool()
def update_issue(owner: str, repo: str, issue_number: int, title: str | None = None, body: str | None = None, state: str | None = None, state_reason: str | None = None) -> dict:
    """Update an issue. Use state='closed' to close it; state_reason may be completed or not_planned."""
    if issue_number < 1:
        raise ValueError("issue_number must be positive")
    fields = {k: v for k, v in {"title": title, "body": body, "state": state, "state_reason": state_reason}.items() if v is not None}
    if not fields:
        raise ValueError("Supply at least one field to update")
    if state is not None and state not in {"open", "closed"}:
        raise ValueError("state must be open or closed")
    if state_reason is not None and state_reason not in {"completed", "not_planned", "reopened"}:
        raise ValueError("Unsupported state_reason")
    return _request("PATCH", f"/{_repo(owner, repo)}/issues/{issue_number}", body=fields)


@mcp.tool()
def list_pull_requests(owner: str, repo: str, state: str = "open", per_page: int = 30, page: int = 1) -> list:
    """List pull requests."""
    if state not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    return _request("GET", f"/{_repo(owner, repo)}/pulls", params={"state": state, "per_page": per_page, "page": page})


@mcp.tool()
def create_pull_request(owner: str, repo: str, title: str, head: str, base: str, body: str = "", draft: bool = False) -> dict:
    """Create a pull request from head branch into base branch."""
    if not title.strip() or not head.strip() or not base.strip():
        raise ValueError("title, head, and base are required")
    return _request("POST", f"/{_repo(owner, repo)}/pulls", body={"title": title.strip(), "head": head, "base": base, "body": body, "draft": draft})


@mcp.tool()
def merge_pull_request(owner: str, repo: str, pull_number: int, merge_method: str = "squash", commit_title: str = "", commit_message: str = "", confirm: bool = False) -> dict:
    """Merge a pull request. Requires confirm=true after reviewing the PR and intended merge method."""
    if pull_number < 1:
        raise ValueError("pull_number must be positive")
    if merge_method not in {"merge", "squash", "rebase"}:
        raise ValueError("merge_method must be merge, squash, or rebase")
    if not confirm:
        return {"confirmation_required": True, "message": "No merge performed. Review the PR and call again with confirm=true.", "repository": f"{owner}/{repo}", "pull_number": pull_number, "merge_method": merge_method}
    body: dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        body["commit_title"] = commit_title
    if commit_message:
        body["commit_message"] = commit_message
    return _request("PUT", f"/{_repo(owner, repo)}/pulls/{pull_number}/merge", body=body)


@mcp.tool()
def read_repository_file(owner: str, repo: str, path: str, ref: str = "") -> dict:
    """Read a repository file (text content is returned decoded from base64)."""
    if not path.strip() or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("path must be a relative repository path without '..'")
    params = {"ref": ref} if ref else None
    data = _request("GET", f"/{_repo(owner, repo)}/contents/{quote(path, safe='/')}", params=params)
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        try:
            data["decoded_content"] = base64.b64decode(data["content"]).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            data["decoded_content"] = None
    return data


@mcp.tool()
def write_repository_file(owner: str, repo: str, path: str, content: str, message: str, branch: str = "", existing_sha: str = "", confirm: bool = False) -> dict:
    """Create or update a UTF-8 text file. Requires confirm=true; updating an existing file requires its existing_sha."""
    if not path.strip() or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("path must be a relative repository path without '..'")
    if not message.strip():
        raise ValueError("Commit message is required")
    if not confirm:
        return {"confirmation_required": True, "message": "No file written. Review the path/content and call again with confirm=true.", "repository": f"{owner}/{repo}", "path": path, "branch": branch or "default branch", "content_length": len(content)}
    body: dict[str, Any] = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
    if branch:
        body["branch"] = branch
    if existing_sha:
        body["sha"] = existing_sha
    return _request("PUT", f"/{_repo(owner, repo)}/contents/{quote(path, safe='/')}", body=body)


@mcp.tool()
def list_collaborators(owner: str, repo: str, affiliation: str = "all", per_page: int = 30, page: int = 1) -> list:
    """List collaborators for a repository. Requires appropriate repository permissions."""
    if affiliation not in {"outside", "direct", "all"}:
        raise ValueError("affiliation must be outside, direct, or all")
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    return _request("GET", f"/{_repo(owner, repo)}/collaborators", params={"affiliation": affiliation, "per_page": per_page, "page": page})


@mcp.tool()
def add_collaborator(owner: str, repo: str, username: str, permission: str = "push", confirm: bool = False) -> dict:
    """Invite a collaborator. permission is pull, triage, push, maintain, or admin. Requires confirm=true."""
    if permission not in {"pull", "triage", "push", "maintain", "admin"}:
        raise ValueError("permission must be pull, triage, push, maintain, or admin")
    if not re.fullmatch(r"[A-Za-z0-9-]+", username):
        raise ValueError("Invalid GitHub username")
    if not confirm:
        return {"confirmation_required": True, "message": "No invitation sent. Confirm the username and permission, then call again with confirm=true.", "repository": f"{owner}/{repo}", "username": username, "permission": permission}
    return _request("PUT", f"/{_repo(owner, repo)}/collaborators/{quote(username, safe='')}", body={"permission": permission})


@mcp.tool()
def remove_collaborator(owner: str, repo: str, username: str, confirmation: str) -> dict:
    """Remove a collaborator. Requires confirmation exactly 'REMOVE username FROM owner/repo'."""
    expected = f"REMOVE {username} FROM {owner}/{repo}"
    if confirmation != expected:
        raise ValueError(f"Removal blocked. To confirm, pass confirmation exactly as: {expected}")
    return _request("DELETE", f"/{_repo(owner, repo)}/collaborators/{quote(username, safe='')}")


@mcp.tool()
def list_releases(owner: str, repo: str, per_page: int = 20, page: int = 1) -> list:
    """List releases for a repository."""
    if not 1 <= per_page <= 100 or page < 1:
        raise ValueError("per_page must be 1-100 and page must be >= 1")
    return _request("GET", f"/{_repo(owner, repo)}/releases", params={"per_page": per_page, "page": page})


@mcp.tool()
def create_release(owner: str, repo: str, tag_name: str, name: str = "", body: str = "", target_commitish: str = "", draft: bool = True, prerelease: bool = False, confirm: bool = False) -> dict:
    """Create a release. Drafts default to true; requires confirm=true to publish/create it."""
    if not tag_name.strip():
        raise ValueError("tag_name is required")
    if not confirm:
        return {"confirmation_required": True, "message": "No release created. Review tag and settings, then call again with confirm=true.", "repository": f"{owner}/{repo}", "tag_name": tag_name, "draft": draft, "prerelease": prerelease}
    body_data: dict[str, Any] = {"tag_name": tag_name, "name": name or tag_name, "body": body, "draft": draft, "prerelease": prerelease}
    if target_commitish:
        body_data["target_commitish"] = target_commitish
    return _request("POST", f"/{_repo(owner, repo)}/releases", body=body_data)


def main() -> None:
    # Keep stdout reserved for MCP protocol; diagnostics go to stderr via the SDK logger.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
