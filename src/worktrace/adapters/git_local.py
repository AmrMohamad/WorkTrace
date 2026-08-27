"""Read-only local Git full-snapshot adapter."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from worktrace.adapters.base import (
    NormalizedPage,
    NormalizedRecord,
    Participation,
    ParticipationRole,
    Reference,
    ReferenceStrength,
)
from worktrace.errors import ConfigurationError, PermanentSourceError, ScopeViolation
from worktrace.normalize import (
    Redactor,
    actor_identity,
    build_record,
    exact_jira_keys,
    observed_now,
    parse_git_trailers,
)

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_REVERTED_COMMIT = re.compile(r"(?im)^This reverts commit ([0-9a-f]{40,64})\.?$")
_CHERRY_PICKED_COMMIT = re.compile(r"(?im)^\(cherry picked from commit ([0-9a-f]{40,64})\)$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(.+)$")
_PATH_STATUS = re.compile(r"^[ACDMRTUXB][0-9]{0,3}$")
_READ_ONLY_COMMANDS = frozenset({"for-each-ref", "remote", "rev-list", "rev-parse", "show"})


@dataclass(frozen=True, slots=True)
class LocalGitConfig:
    repository_path: Path
    source_instance: str
    app_id: str
    email_key: bytes
    jira_project_keys: tuple[str, ...] = ()
    allowed_root: Path | None = None
    page_size: int = 100
    command_timeout_seconds: float = 30.0
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True, slots=True)
class _GitPathStatus:
    code: str
    old_path: bytes | None = None


class LocalGitAdapter:
    """Reads existing objects and refs without fetching or changing repository state."""

    def __init__(self, config: LocalGitConfig) -> None:
        if config.page_size < 1:
            raise ConfigurationError("Git page_size must be positive")
        if config.date_from and config.date_to and config.date_from > config.date_to:
            raise ConfigurationError("Git date_from must not be after date_to")
        self._config = config
        self._repo = config.repository_path.resolve(strict=True)
        if config.allowed_root is not None:
            allowed = config.allowed_root.resolve(strict=True)
            if not self._repo.is_relative_to(allowed):
                raise ScopeViolation("Configured Git repository is outside the allowed root")
        self._redactor = Redactor(config.email_key)
        actual_root = Path(self._run_git(("rev-parse", "--show-toplevel")).strip()).resolve()
        if actual_root != self._repo:
            raise ScopeViolation("Configured Git path must be the repository root")

    def _run_git(self, args: Sequence[str]) -> str:
        return self._run_git_bytes(args).decode("utf-8", errors="replace")

    def _run_git_bytes(self, args: Sequence[str]) -> bytes:
        if not args or args[0] not in _READ_ONLY_COMMANDS:
            raise PermanentSourceError("Attempted unsupported Git operation")
        environment = os.environ.copy()
        for variable in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        ):
            environment.pop(variable, None)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(self._repo), *args],
                cwd=self._repo,
                env=environment,
                check=False,
                capture_output=True,
                timeout=self._config.command_timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PermanentSourceError("Unable to read the configured Git repository") from None
        if result.returncode != 0:
            raise PermanentSourceError("Git metadata read failed")
        return result.stdout

    def iter_pages(self) -> Iterator[NormalizedPage]:
        observed_at = observed_now()
        yield from self._commit_pages(observed_at)
        yield from self._ref_pages(observed_at)

    def _commit_pages(self, observed_at: str) -> Iterator[NormalizedPage]:
        arguments = ["rev-list", "--all", "--reverse"]
        if self._config.date_from:
            arguments.append(f"--since={self._config.date_from.isoformat()}T00:00:00Z")
        if self._config.date_to:
            arguments.append(f"--until={self._config.date_to.isoformat()}T23:59:59Z")
        output = self._run_git(tuple(arguments))
        shas = [sha for sha in output.splitlines() if _COMMIT_SHA.fullmatch(sha)]
        if not shas:
            yield NormalizedPage(
                source_kind="git",
                source_instance=self._config.source_instance,
                resource_type="commit",
                cursor=None,
                next_cursor=None,
                is_last=True,
                records=(),
            )
            return
        for offset in range(0, len(shas), self._config.page_size):
            page_shas = shas[offset : offset + self._config.page_size]
            records = tuple(self._commit_record(sha, observed_at) for sha in page_shas)
            next_offset = offset + len(page_shas)
            yield NormalizedPage(
                source_kind="git",
                source_instance=self._config.source_instance,
                resource_type="commit",
                cursor=str(offset),
                next_cursor=str(next_offset) if next_offset < len(shas) else None,
                is_last=next_offset >= len(shas),
                records=records,
            )

    def _commit_record(self, sha: str, observed_at: str) -> NormalizedRecord:
        if not _COMMIT_SHA.fullmatch(sha):
            raise PermanentSourceError("Git returned an invalid object identity")
        format_string = "%H%x00%P%x00%aN%x00%aE%x00%aI%x00%cN%x00%cE%x00%cI%x00%B"
        raw = self._run_git(("show", "-s", "--no-show-signature", f"--format={format_string}", sha))
        fields = raw.rstrip("\n").split("\x00", maxsplit=8)
        if len(fields) != 9:
            raise PermanentSourceError("Git returned malformed commit metadata")
        (
            commit_sha,
            parent_text,
            author_name,
            author_email,
            authored_at,
            committer_name,
            committer_email,
            committed_at,
            message,
        ) = fields
        subject, _, body = message.partition("\n")
        participations: list[Participation] = [
            Participation(
                actor=actor_identity(
                    source_kind="git",
                    source_instance=self._config.source_instance,
                    redactor=self._redactor,
                    provider_actor_id=None,
                    display_name=author_name,
                    email=author_email,
                ),
                role=ParticipationRole.AUTHOR,
                effective_from=authored_at,
            ),
            Participation(
                actor=actor_identity(
                    source_kind="git",
                    source_instance=self._config.source_instance,
                    redactor=self._redactor,
                    provider_actor_id=None,
                    display_name=committer_name,
                    email=committer_email,
                ),
                role=ParticipationRole.COMMITTER,
                effective_from=committed_at,
            ),
        ]
        for trailer in parse_git_trailers(message):
            participations.append(
                Participation(
                    actor=actor_identity(
                        source_kind="git",
                        source_instance=self._config.source_instance,
                        redactor=self._redactor,
                        provider_actor_id=None,
                        display_name=trailer.name,
                        email=trailer.email,
                    ),
                    role=trailer.role,
                    effective_from=committed_at,
                )
            )
        parent_shas = parent_text.split()
        references = [
            Reference(
                reference_type="git_parent",
                target_external_id=parent,
                strength=ReferenceStrength.STRUCTURED,
                target_source_kind="git",
                target_object_type="commit",
            )
            for parent in parent_shas
        ]
        if len(parent_shas) > 1:
            references.extend(
                Reference(
                    reference_type="git_merge_parent",
                    target_external_id=parent,
                    strength=ReferenceStrength.STRUCTURED,
                    target_source_kind="git",
                    target_object_type="commit",
                )
                for parent in parent_shas
            )
        references.extend(
            Reference(
                reference_type="jira_key_mention",
                target_external_id=key,
                strength=ReferenceStrength.EXACT_TEXT,
                target_source_kind="jira",
                target_object_type="issue",
            )
            for key in exact_jira_keys(message, self._config.jira_project_keys)
        )
        references.extend(
            Reference(
                reference_type="git_reverts_commit",
                target_external_id=match.group(1),
                strength=ReferenceStrength.STRUCTURED,
                target_source_kind="git",
                target_object_type="commit",
            )
            for match in _REVERTED_COMMIT.finditer(message)
        )
        references.extend(
            Reference(
                reference_type="git_cherry_picks_commit",
                target_external_id=match.group(1),
                strength=ReferenceStrength.STRUCTURED,
                target_source_kind="git",
                target_object_type="commit",
            )
            for match in _CHERRY_PICKED_COMMIT.finditer(message)
        )
        changed_paths = self._changed_paths(commit_sha)
        return build_record(
            source_kind="git",
            source_instance=self._config.source_instance,
            object_type="commit",
            external_id=commit_sha,
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=committed_at,
            payload={
                "sha": commit_sha,
                "parent_shas": parent_shas,
                "is_merge": len(parent_shas) > 1,
                "merge_parent_shas": parent_shas if len(parent_shas) > 1 else [],
                "authored_at": authored_at,
                "committed_at": committed_at,
                "subject": subject,
                "body": body,
                "changed_paths": changed_paths,
            },
            redactor=self._redactor,
            participations=participations,
            references=references,
            untrusted_text_fields=(
                "subject",
                "body",
                "changed_paths[].path",
                "changed_paths[].old_path",
            ),
        )

    def _changed_paths(self, sha: str) -> list[dict[str, object]]:
        statuses = self._path_statuses(sha)
        raw = self._run_git_bytes(
            (
                "show",
                "--format=",
                "--numstat",
                "-z",
                "--find-renames",
                "--first-parent",
                sha,
            )
        )
        fields = raw.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        changed_paths: list[dict[str, object]] = []
        index = 0
        while index < len(fields):
            header = fields[index]
            index += 1
            parts = header.split(b"\t", maxsplit=2)
            if len(parts) != 3:
                raise PermanentSourceError("Git returned malformed numstat metadata")
            additions_raw, deletions_raw, path_raw = parts
            old_path_raw: bytes | None = None
            if path_raw == b"":
                if index + 1 >= len(fields):
                    raise PermanentSourceError("Git returned a truncated renamed path")
                old_path_raw = fields[index]
                path_raw = fields[index + 1]
                index += 2
            binary = additions_raw == b"-" and deletions_raw == b"-"
            if not binary and (not additions_raw.isdigit() or not deletions_raw.isdigit()):
                raise PermanentSourceError("Git returned invalid numstat counts")
            path, path_encoding = self._decode_git_path(path_raw)
            old_path: str | None = None
            old_path_encoding: str | None = None
            if old_path_raw is not None:
                old_path, old_path_encoding = self._decode_git_path(old_path_raw)
            status = statuses.pop(path_raw, None)
            if status is None:
                raise PermanentSourceError("Git numstat path omitted status metadata")
            if status.old_path is not None and status.old_path != old_path_raw:
                raise PermanentSourceError("Git rename status did not match numstat metadata")
            status_name = {
                "A": "added",
                "B": "pairing_broken",
                "C": "copied",
                "D": "deleted",
                "M": "modified",
                "R": "renamed",
                "T": "type_changed",
                "U": "unmerged",
                "X": "unknown",
            }[status.code[0]]
            changed_paths.append(
                {
                    "path": path,
                    "path_encoding": path_encoding,
                    "old_path": old_path,
                    "old_path_encoding": old_path_encoding,
                    "additions": None if binary else int(additions_raw),
                    "deletions": None if binary else int(deletions_raw),
                    "binary": binary,
                    "status": status_name,
                    "status_code": status.code,
                    "new_file": status.code.startswith("A"),
                    "deleted_file": status.code.startswith("D"),
                    "renamed_file": status.code.startswith("R"),
                }
            )
        if statuses:
            raise PermanentSourceError("Git status metadata omitted numstat paths")
        return changed_paths

    def _path_statuses(self, sha: str) -> dict[bytes, _GitPathStatus]:
        raw = self._run_git_bytes(
            (
                "show",
                "--format=",
                "--name-status",
                "-z",
                "--find-renames",
                "--first-parent",
                sha,
            )
        )
        fields = raw.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        statuses: dict[bytes, _GitPathStatus] = {}
        index = 0
        while index < len(fields):
            try:
                code = fields[index].decode("ascii", errors="strict")
            except UnicodeDecodeError:
                raise PermanentSourceError("Git returned invalid path status metadata") from None
            index += 1
            if _PATH_STATUS.fullmatch(code) is None:
                raise PermanentSourceError("Git returned unsupported path status metadata")
            old_path: bytes | None = None
            if code.startswith(("R", "C")):
                if index + 1 >= len(fields):
                    raise PermanentSourceError("Git returned truncated rename status metadata")
                old_path = fields[index]
                path = fields[index + 1]
                index += 2
            else:
                if index >= len(fields):
                    raise PermanentSourceError("Git returned truncated path status metadata")
                path = fields[index]
                index += 1
            self._decode_git_path(path)
            if old_path is not None:
                self._decode_git_path(old_path)
            if path in statuses:
                raise PermanentSourceError("Git returned duplicate path status metadata")
            statuses[path] = _GitPathStatus(code=code, old_path=old_path)
        return statuses

    @staticmethod
    def _decode_git_path(value: bytes) -> tuple[str, str]:
        if not value or value.startswith(b"/") or b"\x00" in value:
            raise ScopeViolation("Git returned an invalid repository-relative path")
        try:
            return value.decode("utf-8", errors="strict"), "utf-8"
        except UnicodeDecodeError:
            escaped = "".join(
                chr(byte) if 0x20 <= byte <= 0x7E and byte != 0x5C else f"\\x{byte:02x}"
                for byte in value
            )
            return escaped, "escaped-bytes"

    @staticmethod
    def _remote_location(value: str) -> dict[str, str]:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https", "ssh", "git"} and parts.hostname:
            return {
                "kind": parts.scheme,
                "host": parts.hostname.casefold(),
                "path": parts.path,
            }
        scp_match = _SCP_REMOTE.fullmatch(value)
        if scp_match is not None:
            return {
                "kind": "ssh",
                "host": scp_match.group(1).casefold(),
                "path": f"/{scp_match.group(2).lstrip('/')}",
            }
        digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
        return {
            "kind": "local_or_unknown",
            "identity_hash": f"sha256:{digest}",
        }

    def _ref_pages(self, observed_at: str) -> Iterator[NormalizedPage]:
        format_string = (
            "%(refname)%00%(objectname)%00%(objecttype)%00%(creatordate:iso-strict)"
            "%00%(taggername)%00%(taggeremail)"
        )
        raw = self._run_git(
            (
                "for-each-ref",
                f"--format={format_string}",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            )
        )
        records: list[NormalizedRecord] = []
        for line in raw.splitlines():
            fields = line.split("\x00")
            if len(fields) != 6:
                raise PermanentSourceError("Git returned malformed ref metadata")
            ref_name, object_sha, object_type, created_at, creator_name, creator_email = fields
            if created_at:
                created_date = datetime.fromisoformat(
                    created_at.strip().replace("Z", "+00:00")
                ).date()
                if self._config.date_from and created_date < self._config.date_from:
                    continue
                if self._config.date_to and created_date > self._config.date_to:
                    continue
            if not ref_name.startswith(("refs/heads/", "refs/remotes/", "refs/tags/")):
                raise ScopeViolation(
                    "Git returned a ref outside the configured snapshot namespaces"
                )
            target_commit_sha = self._run_git(("rev-list", "-n", "1", ref_name)).strip()
            if not _COMMIT_SHA.fullmatch(target_commit_sha):
                raise PermanentSourceError("Git ref did not resolve to a commit")
            if ref_name.startswith("refs/tags/"):
                ref_kind = "tag"
            elif ref_name.startswith("refs/remotes/"):
                ref_kind = "remote_tracking_branch"
            else:
                ref_kind = "local_branch"
            participations: list[Participation] = []
            if ref_kind == "tag" and object_type == "tag" and creator_name:
                participations.append(
                    Participation(
                        actor=actor_identity(
                            source_kind="git",
                            source_instance=self._config.source_instance,
                            redactor=self._redactor,
                            provider_actor_id=None,
                            display_name=creator_name,
                            email=creator_email.strip("<>") or None,
                        ),
                        role=ParticipationRole.AUTHOR,
                        effective_from=created_at or None,
                    )
                )
            references = [
                Reference(
                    reference_type="git_ref_target",
                    target_external_id=target_commit_sha,
                    strength=ReferenceStrength.STRUCTURED,
                    target_source_kind="git",
                    target_object_type="commit",
                )
            ]
            references.extend(
                Reference(
                    reference_type="jira_key_mention",
                    target_external_id=key,
                    strength=ReferenceStrength.EXACT_TEXT,
                    target_source_kind="jira",
                    target_object_type="issue",
                )
                for key in exact_jira_keys(ref_name, self._config.jira_project_keys)
            )
            remote_identity: dict[str, object] | None = None
            if ref_kind == "remote_tracking_branch":
                remote_name = ref_name.removeprefix("refs/remotes/").partition("/")[0]
                if _REMOTE_NAME.fullmatch(remote_name) is None:
                    raise ScopeViolation("Git returned an invalid remote identity")
                urls = self._run_git(("remote", "get-url", "--all", remote_name)).splitlines()
                remote_identity = {
                    "name": remote_name,
                    "locations": [self._remote_location(url) for url in urls if url],
                    "clone_local_observation": True,
                }
            records.append(
                build_record(
                    source_kind="git",
                    source_instance=self._config.source_instance,
                    object_type="ref",
                    external_id=ref_name,
                    app_id=self._config.app_id,
                    observed_at=observed_at,
                    source_updated_at=created_at or None,
                    payload={
                        "ref_name": ref_name,
                        "ref_kind": ref_kind,
                        "target_object_id": object_sha,
                        "target_object_type": object_type,
                        "target_commit_sha": target_commit_sha,
                        "remote_identity": remote_identity,
                    },
                    redactor=self._redactor,
                    participations=participations,
                    references=references,
                )
            )
        if not records:
            yield NormalizedPage(
                source_kind="git",
                source_instance=self._config.source_instance,
                resource_type="ref",
                cursor=None,
                next_cursor=None,
                is_last=True,
                records=(),
            )
            return
        for offset in range(0, len(records), self._config.page_size):
            page_records = tuple(records[offset : offset + self._config.page_size])
            next_offset = offset + len(page_records)
            yield NormalizedPage(
                source_kind="git",
                source_instance=self._config.source_instance,
                resource_type="ref",
                cursor=str(offset),
                next_cursor=str(next_offset) if next_offset < len(records) else None,
                is_last=next_offset >= len(records),
                records=page_records,
            )
