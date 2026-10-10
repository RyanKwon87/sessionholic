"""Optional, read-only adapter for an agent-router layout on this host.

Enabled only by the host settings' `agentRouter` section (`projectsFile`, `workspacesRoot`,
`accountPolicy`). It reads files another launcher owns and never writes them:

- `projectsFile`: tab-separated `id`, `label`, `root` rows; blank, `#` and short rows are
  skipped. A project also owns `<workspacesRoot>/<id>`.
- `accountPolicy`: JSON whose `accounts` keys (never their values) and `suspended_accounts`
  name the accounts.
- `.codex-router/<hash>` homes: `hash = sha256("<account>\\0<project>\\0default")[:16]`
  names (project, account); otherwise the first `ag-sessions/*.json` record's `scope`;
  otherwise `미확인 <first 6 characters>`. `.codex` is `기본`, `.codex-<name>` is `<name>`.

Standard library only and Python 3.9 compatible; the collector carries it to other hosts.
"""
import glob
import hashlib
import json
from pathlib import Path

ROUTER_PREFIX = ".codex-router/"
PACKS = ("default",)


def router_hash(account, project, pack="default"):
    """The scope hash the launcher uses for `~/.codex-router/<hash>` homes."""
    return hashlib.sha256(f"{account}\0{project}\0{pack}".encode()).hexdigest()[:16]


class AgentRouter:
    def __init__(self, section):
        section = section or {}
        self.projects_file = section.get("projectsFile")
        self.workspaces_root = section.get("workspacesRoot")
        self.account_policy = section.get("accountPolicy")

    def projects(self):
        """[{id, label, root, roots}] where `roots` adds the project's workspace folder."""
        if not self.projects_file:
            return []
        try:
            lines = Path(self.projects_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        result = []
        for line in lines:
            row = line.split("\t")
            if not line.strip() or line.startswith("#") or len(row) < 3:
                continue
            roots = [row[2]]
            if self.workspaces_root:
                roots.append(str(Path(self.workspaces_root) / row[0]))
            result.append({"id": row[0], "label": row[1], "root": row[2], "roots": roots})
        return result

    def _policy(self):
        if not self.account_policy:
            return {}
        try:
            data = json.loads(Path(self.account_policy).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def accounts(self):
        """Account keys only; the e-mail values are never copied anywhere."""
        try:
            return list(self._policy().get("accounts") or {})
        except TypeError:
            return []

    def suspended(self):
        value = self._policy().get("suspended_accounts") or []
        try:
            return [name for name in value if isinstance(name, str)]
        except TypeError:
            return []

    def labels(self, projects=None, accounts=None):
        """{hash: (project id, account)} for every project, account and pack."""
        projects = self.projects() if projects is None else projects
        accounts = self.accounts() if accounts is None else accounts
        return {router_hash(a, p["id"], pack): (p["id"], a) for a in accounts for p in projects for pack in PACKS}

    @staticmethod
    def first_scope(path):
        for name in sorted(glob.glob(str(Path(path) / "ag-sessions" / "*.json")))[:1]:
            try:
                data = json.loads(Path(name).read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return {}
            scope = data.get("scope") if isinstance(data, dict) else None
            return scope if isinstance(scope, dict) else {}
        return {}

    def home_label(self, key, path, labels=None):
        """(project id or None, account label) for one Codex home."""
        if key.startswith(ROUTER_PREFIX):
            digest = key[len(ROUTER_PREFIX):]
            labels = self.labels() if labels is None else labels
            if digest in labels:
                return labels[digest]
            scope = self.first_scope(path)
            if scope.get("account"):
                return scope.get("project"), scope["account"]
            return None, "미확인 " + digest[:6]
        return None, "기본" if key == ".codex" else key[len(".codex-"):]


def from_settings(config):
    """The adapter for loaded settings, or None when `agentRouter` is not configured."""
    section = config.get("agentRouter")
    return AgentRouter(section) if section else None
