"""Scoped GitLab REST v4 full-snapshot adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from worktrace.adapters.base import (
    JSONValue,
    NormalizedPage,
    NormalizedRecord,
    Participation,
    ParticipationRole,
    Reference,
    ReferenceStrength,
    UnavailableObjectDescriptor,
)
from worktrace.adapters.retry import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_retry
from worktrace.errors import (
    ConfigurationError,
    PermanentSourceError,
    ScopeViolation,
    SourceObjectUnavailable,
)
from worktrace.normalize import (
    Redactor,
    actor_identity,
    build_record,
    exact_jira_keys,
    observed_now,
    parse_git_trailers,
)

_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True, slots=True)
class GitLabConfig:
    base_url: str
    source_instance: str
    app_id: str
    project_id: str | int
    email_key: bytes
    date_from: date
    date_to: date
    jira_project_keys: tuple[str, ...] = ()
    user_id: int | None = None
    username: str | None = None
    production_environments: tuple[str, ...] = ()
    relevant_commit_shas: tuple[str, ...] = ()
    max_commit_association_lookups: int = 200
    max_merge_request_hydrations: int = 500
    page_size: int = 100
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identifier(value: object) -> str | None:
    if isinstance(value, str | int):
        return str(value)
    return None


class GitLabAdapter:
    """Reads MRs, releases, and deployments for exactly one configured project."""

    def __init__(self, config: GitLabConfig, client: httpx.Client) -> None:
        project_id = str(config.project_id).strip()
        if not project_id:
            raise ConfigurationError("GitLab project_id must not be empty")
        if config.page_size < 1 or config.page_size > 100:
            raise ConfigurationError("GitLab page_size must be between 1 and 100")
        if config.date_from > config.date_to:
            raise ConfigurationError("GitLab date_from must not be after date_to")
        if config.user_id is None and config.username is None:
            raise ConfigurationError("GitLab discovery requires a configured user identity")
        if config.user_id is not None and config.user_id < 1:
            raise ConfigurationError("GitLab user_id must be positive")
        if config.username is not None and not config.username.strip():
            raise ConfigurationError("GitLab username must not be blank")
        if config.max_commit_association_lookups < 0 or config.max_merge_request_hydrations < 0:
            raise ConfigurationError("GitLab discovery bounds must not be negative")
        relevant_commit_shas = tuple(
            dict.fromkeys(sha.strip().lower() for sha in config.relevant_commit_shas)
        )
        if any(_COMMIT_SHA.fullmatch(sha) is None for sha in relevant_commit_shas):
            raise ConfigurationError("GitLab relevant commit SHAs must be full hexadecimal IDs")
        if self._origin(config.base_url) != self._origin(str(client.base_url)):
            raise ScopeViolation("GitLab client origin does not match configured scope")
        self._config = config
        self._project_id = project_id
        self._encoded_project = quote(project_id, safe="")
        self._client = client
        self._redactor = Redactor(config.email_key)
        self._production_environments = tuple(
            dict.fromkeys(
                environment.strip()
                for environment in config.production_environments
                if environment.strip()
            )
        )
        self._relevant_commit_shas = relevant_commit_shas
        self._verified_user_id: str | None = None
        self._verified_username: str | None = None
        self._verified_email: str | None = None
        self._window_start = datetime.combine(config.date_from, time.min, tzinfo=UTC)
        self._window_end = datetime.combine(
            config.date_to + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ConfigurationError("GitLab base_url must be an HTTP(S) origin")
        if parts.scheme == "http" and parts.hostname.casefold() not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ConfigurationError("GitLab credentials require HTTPS outside loopback tests")
        return parts.scheme, parts.hostname.casefold(), parts.port

    def iter_pages(self) -> Iterator[NormalizedPage]:
        observed_at = observed_now()
        self._verify_identity()
        yield from self._discovered_merge_request_pages(observed_at)
        yield from self._resource_pages("releases", observed_at)
        if not self._production_environments:
            yield NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type="deployments",
                cursor=None,
                next_cursor=None,
                is_last=True,
                records=(),
            )
        else:
            seen_deployments: set[str] = set()
            for environment in self._production_environments:
                yield from self._resource_pages(
                    "deployments",
                    observed_at,
                    extra_params={"status": "success", "environment": environment},
                    seen_external_ids=seen_deployments,
                    cursor_prefix=environment,
                )

    def _verify_identity(self) -> None:
        response = request_with_retry(
            self._client,
            "GET",
            "/api/v4/user",
            policy=self._config.retry_policy,
        )
        try:
            document = response.json()
        except ValueError:
            raise PermanentSourceError("GitLab returned invalid identity JSON") from None
        if not isinstance(document, Mapping):
            raise PermanentSourceError("GitLab returned an invalid identity document")
        user_id = _identifier(document.get("id"))
        username = _text(document.get("username"))
        if user_id is None or username is None:
            raise PermanentSourceError("GitLab identity omitted stable fields")
        if self._config.user_id is not None and user_id != str(self._config.user_id):
            raise ScopeViolation("GitLab authenticated user does not match configured user_id")
        if (
            self._config.username is not None
            and username.casefold() != self._config.username.casefold()
        ):
            raise ScopeViolation("GitLab authenticated user does not match configured username")
        self._verified_user_id = user_id
        self._verified_username = username
        self._verified_email = _text(document.get("email"))

    def _discovered_merge_request_pages(
        self,
        observed_at: str,
    ) -> Iterator[NormalizedPage]:
        assert self._verified_user_id is not None
        endpoint = f"/api/v4/projects/{self._encoded_project}/merge_requests"
        base_params: dict[str, str | int] = {
            "state": "all",
            "order_by": "updated_at",
            "sort": "asc",
            "updated_after": self._iso(self._window_start),
            "updated_before": self._iso(self._window_end - timedelta(microseconds=1)),
        }
        discovered: dict[str, Mapping[str, object]] = {}
        discovery_filters: tuple[dict[str, str], ...] = (
            {"scope": "all", "author_id": self._verified_user_id},
            {"scope": "all", "assignee_id": self._verified_user_id},
            {"scope": "reviews_for_me"},
        )
        for discovery_filter in discovery_filters:
            params = {**base_params, **discovery_filter}
            for document in self._collection_documents(endpoint, params):
                for raw_item in document:
                    if not self._within_window("merge_requests", raw_item):
                        continue
                    value = _mapping(raw_item)
                    iid = self._scoped_merge_request_iid(value)
                    discovered.setdefault(iid, value)

        limitations: list[str] = []
        selection_events: list[dict[str, JSONValue]] = []
        commit_endpoint = f"/api/v4/projects/{self._encoded_project}/repository/commits"
        association_candidates: dict[str, tuple[int, str]] = {
            sha: (1, "") for sha in self._relevant_commit_shas
        }
        author = self._verified_email or self._verified_username
        if author:
            commit_params: dict[str, str | int] = {
                "author": author,
                "since": self._iso(self._window_start),
                "until": self._iso(self._window_end - timedelta(microseconds=1)),
                "all": "true",
            }
            for commits in self._collection_documents(commit_endpoint, commit_params):
                for raw_commit in commits:
                    if not self._subresource_within_window(
                        raw_commit,
                        ("committed_date", "authored_date", "created_at"),
                        "discovery commit",
                    ):
                        continue
                    commit = _mapping(raw_commit)
                    provider_sha = _text(commit.get("id"))
                    if provider_sha is None:
                        raise PermanentSourceError("GitLab discovery commit omitted its id")
                    normalized_sha = provider_sha.casefold()
                    if _COMMIT_SHA.fullmatch(normalized_sha) is None:
                        raise PermanentSourceError("GitLab discovery returned an invalid commit id")
                    committed_at = next(
                        (
                            _text(commit.get(field))
                            for field in ("committed_date", "authored_date", "created_at")
                            if _text(commit.get(field))
                        ),
                        "",
                    )
                    priority, existing_timestamp = association_candidates.get(
                        normalized_sha, (0, "")
                    )
                    association_candidates[normalized_sha] = (
                        priority,
                        max(existing_timestamp, committed_at or ""),
                    )

        association_policy = "relevant_seed_desc_then_committed_at_desc_then_sha_desc"
        ordered_association_shas = sorted(
            association_candidates,
            key=lambda sha: (*association_candidates[sha], sha),
            reverse=True,
        )
        selected_association_shas = ordered_association_shas[
            : self._config.max_commit_association_lookups
        ]
        association_dropped = len(ordered_association_shas) - len(selected_association_shas)
        if association_dropped:
            limitations.append(
                "GitLab commit-to-merge-request association candidates exceeded the configured "
                "lookup bound; explicit local seeds and the newest provider commits were retained."
            )
            selection_events.append(
                {
                    "kind": "gitlab_commit_association_cap",
                    "input_count": len(ordered_association_shas),
                    "selected_count": len(selected_association_shas),
                    "dropped_count": association_dropped,
                    "limit": self._config.max_commit_association_lookups,
                    "selection_policy": association_policy,
                }
            )
        for sha in selected_association_shas:
            self._add_commit_associated_merge_requests(commit_endpoint, sha, discovered)

        hydration_policy = "updated_at_desc_then_iid_desc"
        ordered_iids = sorted(
            discovered,
            key=lambda iid: (self._timestamp_for("merge_requests", discovered[iid]), int(iid)),
            reverse=True,
        )
        selected_iids = ordered_iids[: self._config.max_merge_request_hydrations]
        hydration_dropped = len(ordered_iids) - len(selected_iids)
        if hydration_dropped:
            limitations.append(
                "Discovered GitLab merge requests exceeded the configured hydration bound; "
                "the most recently updated merge requests were retained deterministically."
            )
            selection_events.append(
                {
                    "kind": "gitlab_merge_request_hydration_cap",
                    "input_count": len(ordered_iids),
                    "selected_count": len(selected_iids),
                    "dropped_count": hydration_dropped,
                    "limit": self._config.max_merge_request_hydrations,
                    "selection_policy": hydration_policy,
                }
            )
        if not selected_iids:
            yield NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type="merge_requests",
                cursor=None,
                next_cursor=None,
                is_last=True,
                records=(),
                limitations=tuple(limitations),
                selection_events=tuple(selection_events),
            )
            return
        for index, iid in enumerate(selected_iids):
            hydrate_endpoint = f"{endpoint}/{iid}"
            try:
                response = request_with_retry(
                    self._client,
                    "GET",
                    hydrate_endpoint,
                    policy=self._config.retry_policy,
                    exact_object=True,
                )
            except SourceObjectUnavailable:
                yield NormalizedPage(
                    source_kind="gitlab",
                    source_instance=self._config.source_instance,
                    resource_type="merge_requests",
                    cursor=f"hydrate:{index}",
                    next_cursor=(
                        f"hydrate:{index + 1}" if index + 1 < len(selected_iids) else None
                    ),
                    is_last=index + 1 >= len(selected_iids),
                    records=(),
                    unavailable_objects=(
                        UnavailableObjectDescriptor(
                            kind="gitlab_mr",
                            external_id=f"{self._project_id}:{iid}",
                        ),
                    ),
                    limitations=tuple(limitations) if index == 0 else (),
                    selection_events=tuple(selection_events) if index == 0 else (),
                )
                continue
            try:
                hydrated = response.json()
            except ValueError:
                raise PermanentSourceError("GitLab returned invalid hydrated MR JSON") from None
            if not isinstance(hydrated, Mapping):
                raise PermanentSourceError("GitLab returned an invalid hydrated MR document")
            if self._scoped_merge_request_iid(hydrated) != iid:
                raise ScopeViolation("GitLab hydrated a different merge request")
            reviewer_state_page: NormalizedPage | None = None
            completed_reviewers: tuple[Participation, ...] = ()
            reviewer_states: tuple[dict[str, JSONValue], ...] = ()
            reviewers = hydrated.get("reviewers")
            if isinstance(reviewers, list) and reviewers:
                (
                    reviewer_state_page,
                    completed_reviewers,
                    reviewer_states,
                ) = self._merge_request_reviewers_page(
                    endpoint=f"{hydrate_endpoint}/reviewers",
                    iid=iid,
                    merge_request_updated_at=_text(hydrated.get("updated_at")),
                    observed_at=observed_at,
                )
            record = self._merge_request(
                hydrated,
                observed_at,
                completed_reviewers=completed_reviewers,
                reviewer_states=reviewer_states,
            )
            yield NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type="merge_requests",
                cursor=f"hydrate:{index}",
                next_cursor=(f"hydrate:{index + 1}" if index + 1 < len(selected_iids) else None),
                is_last=index + 1 >= len(selected_iids),
                records=(record,),
                limitations=tuple(limitations) if index == 0 else (),
                selection_events=tuple(selection_events) if index == 0 else (),
            )
            yield from self._merge_request_evidence_pages(
                hydrated,
                observed_at,
                reviewer_state_page=reviewer_state_page,
            )

    def _add_commit_associated_merge_requests(
        self,
        commit_endpoint: str,
        sha: str,
        discovered: dict[str, Mapping[str, object]],
    ) -> None:
        association_endpoint = f"{commit_endpoint}/{quote(sha, safe='')}/merge_requests"
        for merge_requests in self._collection_documents(association_endpoint, {}):
            for raw_item in merge_requests:
                value = _mapping(raw_item)
                iid = self._scoped_merge_request_iid(value)
                discovered.setdefault(iid, value)

    def _collection_documents(
        self,
        endpoint: str,
        params: Mapping[str, str | int],
    ) -> Iterator[list[object]]:
        page = 1
        seen_pages: set[int] = set()
        while True:
            response = request_with_retry(
                self._client,
                "GET",
                endpoint,
                params={**params, "page": page, "per_page": self._config.page_size},
                policy=self._config.retry_policy,
            )
            try:
                document = response.json()
            except ValueError:
                raise PermanentSourceError("GitLab returned invalid collection JSON") from None
            if not isinstance(document, list):
                raise PermanentSourceError("GitLab returned an invalid collection document")
            yield document
            next_page = self._next_page(response, endpoint)
            if next_page is None:
                break
            if next_page < 1 or next_page in seen_pages:
                raise PermanentSourceError("GitLab pagination repeated a page")
            seen_pages.add(next_page)
            page = next_page

    def _scoped_merge_request_iid(self, value: Mapping[str, object]) -> str:
        returned_project = _identifier(value.get("project_id"))
        if returned_project is not None and returned_project != self._project_id:
            raise ScopeViolation("GitLab returned an MR outside the configured project")
        iid = _identifier(value.get("iid"))
        if iid is None or not iid.isdecimal() or int(iid) < 1:
            raise PermanentSourceError("GitLab merge request omitted a valid iid")
        return iid

    def _resource_pages(
        self,
        resource: str,
        observed_at: str,
        *,
        extra_params: Mapping[str, str | int] | None = None,
        seen_external_ids: set[str] | None = None,
        cursor_prefix: str | None = None,
    ) -> Iterator[NormalizedPage]:
        page = 1
        seen_pages: set[int] = set()
        while True:
            endpoint = f"/api/v4/projects/{self._encoded_project}/{resource}"
            params: dict[str, str | int] = {"page": page, "per_page": self._config.page_size}
            if extra_params:
                params.update(extra_params)
            if resource == "merge_requests":
                params.update(
                    {
                        "scope": "all",
                        "state": "all",
                        "order_by": "updated_at",
                        "sort": "asc",
                        "updated_after": self._iso(self._window_start),
                        "updated_before": self._iso(self._window_end - timedelta(microseconds=1)),
                    }
                )
            elif resource == "deployments":
                params.update(
                    {
                        "order_by": "updated_at",
                        "sort": "asc",
                        "updated_after": self._iso(self._window_start - timedelta(microseconds=1)),
                        "updated_before": self._iso(self._window_end),
                    }
                )
            else:
                # GitLab v4 has no release date filter. Descending order plus
                # client filtering lets us stop as soon as a page is older than the window.
                params.update({"order_by": "released_at", "sort": "desc"})
            response = request_with_retry(
                self._client,
                "GET",
                endpoint,
                params=params,
                policy=self._config.retry_policy,
            )
            try:
                document = response.json()
            except ValueError:
                raise PermanentSourceError("GitLab returned invalid JSON") from None
            if not isinstance(document, list):
                raise PermanentSourceError("GitLab returned an invalid collection document")
            scoped_items = tuple(
                item
                for item in document
                if self._within_window(resource, item)
                and (resource != "deployments" or self._deployment_selected(item))
            )
            records_list: list[NormalizedRecord] = []
            for item in scoped_items:
                record = self._normalize(resource, item, observed_at)
                if seen_external_ids is not None:
                    if record.identity.external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(record.identity.external_id)
                records_list.append(record)
            records = tuple(records_list)
            next_page = self._next_page(response, endpoint)
            if resource == "releases" and self._release_page_is_older(document):
                next_page = None
            if next_page is not None:
                if next_page < 1 or next_page in seen_pages:
                    raise PermanentSourceError("GitLab pagination repeated a page")
                seen_pages.add(next_page)
            yield NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type=resource,
                cursor=f"{cursor_prefix}:{page}" if cursor_prefix else str(page),
                next_cursor=(
                    f"{cursor_prefix}:{next_page}"
                    if cursor_prefix and next_page is not None
                    else str(next_page)
                    if next_page is not None
                    else None
                ),
                is_last=next_page is None,
                records=records,
            )
            if resource == "merge_requests":
                for merge_request in scoped_items:
                    yield from self._merge_request_evidence_pages(
                        _mapping(merge_request), observed_at
                    )
            if next_page is None:
                break
            page = next_page

    def _merge_request_evidence_pages(
        self,
        merge_request: Mapping[str, object],
        observed_at: str,
        *,
        reviewer_state_page: NormalizedPage | None = None,
    ) -> Iterator[NormalizedPage]:
        iid = _identifier(merge_request.get("iid"))
        if iid is None or not iid.isdecimal() or int(iid) < 1:
            raise PermanentSourceError("GitLab merge request omitted a valid iid")
        base = f"/api/v4/projects/{self._encoded_project}/merge_requests/{iid}"
        if reviewer_state_page is not None:
            yield reviewer_state_page
        yield from self._merge_request_collection_pages(
            endpoint=f"{base}/commits",
            resource_type="merge_request_commits",
            iid=iid,
            observed_at=observed_at,
        )
        yield from self._merge_request_collection_pages(
            endpoint=f"{base}/discussions",
            resource_type="merge_request_discussions",
            iid=iid,
            observed_at=observed_at,
        )
        yield self._merge_request_changes_page(
            endpoint=f"{base}/changes",
            iid=iid,
            merge_request_updated_at=_text(merge_request.get("updated_at")),
            observed_at=observed_at,
        )

    def _merge_request_reviewers_page(
        self,
        *,
        endpoint: str,
        iid: str,
        merge_request_updated_at: str | None,
        observed_at: str,
    ) -> tuple[
        NormalizedPage,
        tuple[Participation, ...],
        tuple[dict[str, JSONValue], ...],
    ]:
        response = request_with_retry(
            self._client,
            "GET",
            endpoint,
            policy=self._config.retry_policy,
        )
        try:
            document = response.json()
        except ValueError:
            raise PermanentSourceError("GitLab returned invalid MR reviewer-state JSON") from None
        if not isinstance(document, list):
            raise PermanentSourceError("GitLab returned an invalid MR reviewer-state document")
        records: list[NormalizedRecord] = []
        completed_reviewers: list[Participation] = []
        reviewer_states: list[dict[str, JSONValue]] = []
        for raw_state in document:
            state_document = _mapping(raw_state)
            actor_document = _mapping(state_document.get("user"))
            actor_id = _identifier(actor_document.get("id"))
            if actor_id is None or not actor_id.isdecimal() or int(actor_id) < 1:
                raise PermanentSourceError("GitLab MR reviewer state omitted a valid user id")
            review_state = _text(state_document.get("state"))
            if review_state is None:
                raise PermanentSourceError("GitLab MR reviewer state omitted its state")
            assigned_at = _text(state_document.get("created_at"))
            actor = actor_identity(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                redactor=self._redactor,
                provider_actor_id=actor_id,
                display_name=_text(actor_document.get("name")),
                username=_text(actor_document.get("username")),
                email=_text(actor_document.get("public_email"))
                or _text(actor_document.get("email")),
            )
            if review_state == "reviewed":
                completed_reviewers.append(
                    Participation(actor=actor, role=ParticipationRole.REVIEWER)
                )
            reviewer_states.append(
                {
                    "reviewer_actor_id": actor.source_actor_id,
                    "review_state": review_state,
                    "assigned_at": assigned_at,
                }
            )
            records.append(
                build_record(
                    source_kind="gitlab",
                    source_instance=self._config.source_instance,
                    object_type="merge_request_reviewer_state",
                    external_id=f"{self._project_id}:{iid}:reviewer:{actor_id}",
                    app_id=self._config.app_id,
                    observed_at=observed_at,
                    source_updated_at=merge_request_updated_at or assigned_at,
                    payload={
                        "project_id": self._project_id,
                        "mr_iid": iid,
                        "reviewer_actor_id": actor.source_actor_id,
                        "review_state": review_state,
                        "assigned_at": assigned_at,
                    },
                    redactor=self._redactor,
                    references=(self._merge_request_reference(iid, "gitlab_mr_reviewer_state"),),
                )
            )
        records.sort(key=lambda record: record.identity.external_id)
        completed_reviewers.sort(key=lambda item: item.actor.source_actor_id)
        reviewer_states.sort(key=lambda item: str(item["reviewer_actor_id"]))
        return (
            NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type="merge_request_reviewer_states",
                cursor=f"{iid}:1",
                next_cursor=None,
                is_last=True,
                records=tuple(records),
            ),
            tuple(completed_reviewers),
            tuple(reviewer_states),
        )

    def _merge_request_collection_pages(
        self,
        *,
        endpoint: str,
        resource_type: str,
        iid: str,
        observed_at: str,
    ) -> Iterator[NormalizedPage]:
        page = 1
        seen_pages: set[int] = set()
        while True:
            response = request_with_retry(
                self._client,
                "GET",
                endpoint,
                params={"page": page, "per_page": self._config.page_size},
                policy=self._config.retry_policy,
            )
            try:
                document = response.json()
            except ValueError:
                raise PermanentSourceError("GitLab returned invalid MR evidence JSON") from None
            if not isinstance(document, list):
                raise PermanentSourceError("GitLab returned an invalid MR evidence collection")
            records: list[NormalizedRecord] = []
            for item in document:
                if resource_type == "merge_request_commits":
                    if not self._subresource_within_window(
                        item, ("committed_date", "authored_date"), "MR commit"
                    ):
                        continue
                    records.append(self._merge_request_commit(iid, item, observed_at))
                else:
                    records.extend(self._merge_request_discussion(iid, item, observed_at))
            next_page = self._next_page(response, endpoint)
            if next_page is not None:
                if next_page < 1 or next_page in seen_pages:
                    raise PermanentSourceError("GitLab MR evidence pagination repeated a page")
                seen_pages.add(next_page)
            yield NormalizedPage(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                resource_type=resource_type,
                cursor=f"{iid}:{page}",
                next_cursor=f"{iid}:{next_page}" if next_page is not None else None,
                is_last=next_page is None,
                records=tuple(records),
            )
            if next_page is None:
                break
            page = next_page

    def _subresource_within_window(
        self,
        item: object,
        fields: tuple[str, ...],
        resource: str,
    ) -> bool:
        value = _mapping(item)
        raw = next((_text(value.get(field)) for field in fields if _text(value.get(field))), None)
        if raw is None:
            raise PermanentSourceError(f"GitLab {resource} omitted its scope timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise PermanentSourceError(f"GitLab {resource} returned an invalid timestamp") from None
        if parsed.tzinfo is None:
            raise PermanentSourceError(f"GitLab {resource} timestamp omitted its timezone")
        return self._window_start <= parsed.astimezone(UTC) < self._window_end

    def _merge_request_commit(
        self,
        iid: str,
        raw_commit: object,
        observed_at: str,
    ) -> NormalizedRecord:
        commit = _mapping(raw_commit)
        sha = _text(commit.get("id"))
        if sha is None:
            raise PermanentSourceError("GitLab MR commit omitted its stable identity")
        authored_at = _text(commit.get("authored_date"))
        committed_at = _text(commit.get("committed_date"))
        message = _text(commit.get("message")) or ""
        title = _text(commit.get("title")) or ""
        participations: list[Participation] = []
        for role, name_field, email_field, effective_from in (
            (
                ParticipationRole.AUTHOR,
                "author_name",
                "author_email",
                authored_at,
            ),
            (
                ParticipationRole.COMMITTER,
                "committer_name",
                "committer_email",
                committed_at,
            ),
        ):
            name = _text(commit.get(name_field))
            email = _text(commit.get(email_field))
            if name or email:
                participations.append(
                    Participation(
                        actor=actor_identity(
                            source_kind="gitlab",
                            source_instance=self._config.source_instance,
                            redactor=self._redactor,
                            provider_actor_id=None,
                            display_name=name,
                            email=email,
                        ),
                        role=role,
                        effective_from=effective_from,
                    )
                )
        for trailer in parse_git_trailers(message):
            participations.append(
                Participation(
                    actor=actor_identity(
                        source_kind="gitlab",
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
        references: list[Reference] = [self._merge_request_reference(iid, "gitlab_mr_commit")]
        references.extend(
            Reference(
                reference_type="jira_key_mention",
                target_external_id=key,
                strength=ReferenceStrength.EXACT_TEXT,
                target_source_kind="jira",
                target_object_type="issue",
            )
            for key in exact_jira_keys("\n".join((title, message)), self._config.jira_project_keys)
        )
        return build_record(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            object_type="merge_request_commit",
            external_id=f"{self._project_id}:{iid}:{sha}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=committed_at or authored_at,
            payload={
                "project_id": self._project_id,
                "mr_iid": iid,
                "sha": sha,
                "short_id": _text(commit.get("short_id")),
                "title": title,
                "message": message,
                "authored_at": authored_at,
                "committed_at": committed_at,
            },
            redactor=self._redactor,
            participations=participations,
            references=references,
            untrusted_text_fields=("title", "message"),
        )

    def _merge_request_discussion(
        self,
        iid: str,
        raw_discussion: object,
        observed_at: str,
    ) -> list[NormalizedRecord]:
        discussion = _mapping(raw_discussion)
        discussion_id = _text(discussion.get("id"))
        notes = discussion.get("notes")
        if discussion_id is None or not isinstance(notes, list):
            raise PermanentSourceError("GitLab discussion omitted its stable identity or notes")
        result: list[NormalizedRecord] = []
        for raw_note in notes:
            if not self._subresource_within_window(
                raw_note, ("updated_at", "created_at"), "discussion note"
            ):
                continue
            note = _mapping(raw_note)
            note_id = _identifier(note.get("id"))
            if note_id is None:
                raise PermanentSourceError("GitLab discussion note omitted its stable identity")
            author = self._actor_participation(
                note.get("author"),
                ParticipationRole.AUTHOR,
                effective_from=_text(note.get("created_at")),
            )
            result.append(
                build_record(
                    source_kind="gitlab",
                    source_instance=self._config.source_instance,
                    object_type="merge_request_discussion_note",
                    external_id=f"{self._project_id}:{iid}:{note_id}",
                    app_id=self._config.app_id,
                    observed_at=observed_at,
                    source_updated_at=_text(note.get("updated_at"))
                    or _text(note.get("created_at")),
                    payload={
                        "project_id": self._project_id,
                        "mr_iid": iid,
                        "discussion_id": discussion_id,
                        "note_id": note_id,
                        "note_type": _text(note.get("type")),
                        "body": _text(note.get("body")),
                        "created_at": _text(note.get("created_at")),
                        "updated_at": _text(note.get("updated_at")),
                        "resolved": note.get("resolved")
                        if isinstance(note.get("resolved"), bool)
                        else None,
                    },
                    redactor=self._redactor,
                    participations=(author,) if author else (),
                    references=(self._merge_request_reference(iid, "gitlab_mr_discussion"),),
                    untrusted_text_fields=("body",),
                )
            )
        return result

    def _merge_request_changes_page(
        self,
        *,
        endpoint: str,
        iid: str,
        merge_request_updated_at: str | None,
        observed_at: str,
    ) -> NormalizedPage:
        response = request_with_retry(
            self._client,
            "GET",
            endpoint,
            policy=self._config.retry_policy,
        )
        try:
            document = response.json()
        except ValueError:
            raise PermanentSourceError("GitLab returned invalid MR changes JSON") from None
        if not isinstance(document, Mapping):
            raise PermanentSourceError("GitLab returned an invalid MR changes document")
        returned_project = _identifier(document.get("project_id"))
        returned_iid = _identifier(document.get("iid"))
        if returned_project is not None and returned_project != self._project_id:
            raise ScopeViolation("GitLab returned MR changes outside the configured project")
        if returned_iid is not None and returned_iid != iid:
            raise ScopeViolation("GitLab returned MR changes for a different merge request")
        raw_changes = document.get("changes")
        if not isinstance(raw_changes, list):
            raise PermanentSourceError("GitLab MR changes omitted the changes list")
        changed_paths = []
        for raw_change in raw_changes:
            change = _mapping(raw_change)
            old_path = _text(change.get("old_path"))
            new_path = _text(change.get("new_path"))
            if old_path is None or new_path is None:
                raise PermanentSourceError("GitLab MR change omitted a path")
            changed_paths.append(
                {
                    "old_path": old_path,
                    "new_path": new_path,
                    "new_file": bool(change.get("new_file"))
                    if isinstance(change.get("new_file"), bool)
                    else False,
                    "renamed_file": bool(change.get("renamed_file"))
                    if isinstance(change.get("renamed_file"), bool)
                    else False,
                    "deleted_file": bool(change.get("deleted_file"))
                    if isinstance(change.get("deleted_file"), bool)
                    else False,
                }
            )
        changes_count = _text(document.get("changes_count"))
        raw_overflow = document.get("overflow")
        overflow = raw_overflow if isinstance(raw_overflow, bool) else False
        limitation = (
            "GitLab truncated the changed-path list for this merge request; retained paths "
            "cannot support a complete scope claim."
        )
        record = build_record(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            object_type="merge_request_changed_paths",
            external_id=f"{self._project_id}:{iid}:changed-paths",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=merge_request_updated_at,
            payload={
                "project_id": self._project_id,
                "mr_iid": iid,
                "changes_count": changes_count,
                "overflow": overflow,
                "scope_complete": not overflow,
                "limitations": [limitation] if overflow else [],
                "changed_paths": changed_paths,
            },
            redactor=self._redactor,
            references=(self._merge_request_reference(iid, "gitlab_mr_changed_paths"),),
            untrusted_text_fields=("changed_paths[].old_path", "changed_paths[].new_path"),
        )
        selection_events: tuple[dict[str, JSONValue], ...] = ()
        limitations: tuple[str, ...] = ()
        if overflow:
            reported_minimum = 0
            count_is_lower_bound = False
            if changes_count:
                count_is_lower_bound = changes_count.endswith("+")
                numeric_count = changes_count.removesuffix("+")
                if numeric_count.isdecimal():
                    reported_minimum = int(numeric_count)
            selection_events = (
                {
                    "kind": "gitlab_changed_paths_overflow",
                    "mr_iid": iid,
                    "retained_count": len(changed_paths),
                    "reported_changes_count": changes_count,
                    "dropped_count_at_least": max(
                        reported_minimum - len(changed_paths),
                        1,
                    ),
                    "reported_count_is_lower_bound": count_is_lower_bound,
                    "selection_policy": "provider_returned_prefix",
                },
            )
            limitations = (limitation,)
        return NormalizedPage(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            resource_type="merge_request_changed_paths",
            cursor=f"{iid}:1",
            next_cursor=None,
            is_last=True,
            records=(record,),
            limitations=limitations,
            selection_events=selection_events,
            records_selection_biased=overflow,
        )

    def _merge_request_reference(self, iid: str, reference_type: str) -> Reference:
        return Reference(
            reference_type=reference_type,
            target_external_id=f"{self._project_id}:{iid}",
            strength=ReferenceStrength.STRUCTURED,
            target_source_kind="gitlab",
            target_object_type="merge_request",
        )

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def _timestamp_for(self, resource: str, item: object) -> datetime:
        value = _mapping(item)
        if resource == "merge_requests":
            raw = _text(value.get("updated_at")) or _text(value.get("created_at"))
        elif resource == "releases":
            raw = _text(value.get("released_at")) or _text(value.get("created_at"))
        else:
            raw = (
                _text(value.get("updated_at"))
                or _text(value.get("finished_at"))
                or _text(value.get("created_at"))
            )
        if raw is None:
            raise PermanentSourceError("GitLab object omitted its scope timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise PermanentSourceError("GitLab object returned an invalid timestamp") from None
        if parsed.tzinfo is None:
            raise PermanentSourceError("GitLab object timestamp omitted its timezone")
        return parsed.astimezone(UTC)

    def _within_window(self, resource: str, item: object) -> bool:
        timestamp = self._timestamp_for(resource, item)
        return self._window_start <= timestamp < self._window_end

    def _deployment_selected(self, item: object) -> bool:
        value = _mapping(item)
        if _text(value.get("status")) != "success":
            return False
        environment = _mapping(value.get("environment"))
        environment_name = _text(value.get("environment_name")) or _text(environment.get("name"))
        if environment_name is None:
            raise PermanentSourceError("GitLab deployment omitted its environment name")
        configured = {name.casefold() for name in self._production_environments}
        return environment_name.casefold() in configured

    def _release_page_is_older(self, document: list[object]) -> bool:
        return bool(document) and all(
            self._timestamp_for("releases", item) < self._window_start for item in document
        )

    def _next_page(self, response: httpx.Response, endpoint: str) -> int | None:
        link_header = response.headers.get("Link")
        if link_header is not None:
            next_link = response.links.get("next")
            if next_link is None or not isinstance(next_link.get("url"), str):
                return None
            target = urlsplit(urljoin(self._config.base_url, str(next_link["url"])))
            configured = urlsplit(self._config.base_url)
            target_origin = target.scheme, (target.hostname or "").casefold(), target.port
            configured_origin = (
                configured.scheme,
                (configured.hostname or "").casefold(),
                configured.port,
            )
            if target_origin != configured_origin:
                raise ScopeViolation("GitLab pagination escaped the configured origin")
            if target.path != endpoint:
                raise ScopeViolation("GitLab pagination escaped the configured project endpoint")
            try:
                pages = parse_qs(target.query, strict_parsing=True).get("page", [])
            except ValueError:
                raise PermanentSourceError("GitLab next link contained an invalid query") from None
            if len(pages) != 1:
                raise PermanentSourceError("GitLab next link omitted a single page number")
            try:
                return int(pages[0])
            except ValueError:
                raise PermanentSourceError("GitLab next link contained an invalid page") from None
        next_header = response.headers.get("X-Next-Page", "").strip()
        if not next_header:
            return None
        try:
            return int(next_header)
        except ValueError:
            raise PermanentSourceError("GitLab returned invalid pagination metadata") from None

    def _normalize(
        self,
        resource: str,
        item: object,
        observed_at: str,
    ) -> NormalizedRecord:
        value = _mapping(item)
        returned_project = _identifier(value.get("project_id"))
        if returned_project is not None and returned_project != self._project_id:
            raise ScopeViolation("GitLab returned an object outside the configured project")
        if resource == "merge_requests":
            return self._merge_request(value, observed_at)
        if resource == "releases":
            return self._release(value, observed_at)
        if resource == "deployments":
            return self._deployment(value, observed_at)
        raise AssertionError(f"Unsupported fixed GitLab resource: {resource}")

    def _actor_participation(
        self,
        value: object,
        role: ParticipationRole,
        *,
        effective_from: str | None = None,
    ) -> Participation | None:
        actor = _mapping(value)
        actor_id = _identifier(actor.get("id"))
        if actor_id is None:
            return None
        return Participation(
            actor=actor_identity(
                source_kind="gitlab",
                source_instance=self._config.source_instance,
                redactor=self._redactor,
                provider_actor_id=actor_id,
                display_name=_text(actor.get("name")),
                username=_text(actor.get("username")),
                email=_text(actor.get("public_email")) or _text(actor.get("email")),
            ),
            role=role,
            effective_from=effective_from,
        )

    def _merge_request(
        self,
        value: Mapping[str, object],
        observed_at: str,
        *,
        completed_reviewers: tuple[Participation, ...] = (),
        reviewer_states: tuple[dict[str, JSONValue], ...] = (),
    ) -> NormalizedRecord:
        iid = _identifier(value.get("iid"))
        if iid is None:
            raise PermanentSourceError("GitLab merge request omitted its iid")
        participations: list[Participation] = []
        author = self._actor_participation(
            value.get("author"),
            ParticipationRole.AUTHOR,
            effective_from=_text(value.get("created_at")),
        )
        if author:
            participations.append(author)
        assignees = value.get("assignees")
        if isinstance(assignees, list):
            for actor in assignees:
                participation = self._actor_participation(actor, ParticipationRole.ASSIGNEE)
                if participation:
                    participations.append(participation)
        reviewers = value.get("reviewers")
        reviewer_assignments: list[dict[str, JSONValue]] = []
        if isinstance(reviewers, list):
            for actor in reviewers:
                actor_id = _identifier(_mapping(actor).get("id"))
                if actor_id is None or not actor_id.isdecimal() or int(actor_id) < 1:
                    raise PermanentSourceError("GitLab reviewer assignment omitted a valid user id")
                reviewer_assignments.append({"reviewer_actor_id": actor_id})
        participations.extend(completed_reviewers)
        merger_value = value.get("merge_user") or value.get("merged_by")
        merger = self._actor_participation(
            merger_value,
            ParticipationRole.MERGER,
            effective_from=_text(value.get("merged_at")),
        )
        if merger:
            participations.append(merger)

        references: list[Reference] = []
        for reference_type, field in (
            ("gitlab_source_head", "sha"),
            ("gitlab_merge_commit", "merge_commit_sha"),
            ("gitlab_squash_commit", "squash_commit_sha"),
        ):
            sha = _text(value.get(field))
            if sha:
                references.append(self._git_reference(reference_type, sha, "commit"))
        title = _text(value.get("title")) or ""
        description = _text(value.get("description")) or ""
        source_branch = _text(value.get("source_branch")) or ""
        for key in exact_jira_keys(
            "\n".join((title, description, source_branch)),
            self._config.jira_project_keys,
        ):
            references.append(
                Reference(
                    reference_type="jira_key_mention",
                    target_external_id=key,
                    strength=ReferenceStrength.EXACT_TEXT,
                    target_source_kind="jira",
                    target_object_type="issue",
                )
            )
        labels = value.get("labels")
        milestone = _mapping(value.get("milestone"))
        return build_record(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            object_type="merge_request",
            external_id=f"{self._project_id}:{iid}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=_text(value.get("updated_at")),
            payload={
                "project_id": self._project_id,
                "iid": iid,
                "title": title,
                "description": description,
                "state": _text(value.get("state")),
                "draft": (
                    bool(value.get("draft")) if isinstance(value.get("draft"), bool) else False
                ),
                "created_at": _text(value.get("created_at")),
                "updated_at": _text(value.get("updated_at")),
                "merged_at": _text(value.get("merged_at")),
                "closed_at": _text(value.get("closed_at")),
                "source_branch": source_branch,
                "target_branch": _text(value.get("target_branch")),
                "sha": _text(value.get("sha")),
                "merge_commit_sha": _text(value.get("merge_commit_sha")),
                "squash_commit_sha": _text(value.get("squash_commit_sha")),
                "labels": [label for label in labels if isinstance(label, str)]
                if isinstance(labels, list)
                else [],
                "milestone": _text(milestone.get("title")),
                "reviewer_assignments": reviewer_assignments,
                "reviewer_states": list(reviewer_states),
            },
            redactor=self._redactor,
            participations=participations,
            references=references,
            untrusted_text_fields=("title", "description", "source_branch"),
        )

    def _release(self, value: Mapping[str, object], observed_at: str) -> NormalizedRecord:
        tag_name = _text(value.get("tag_name"))
        if tag_name is None:
            raise PermanentSourceError("GitLab release omitted its tag name")
        author = self._actor_participation(
            value.get("author"),
            ParticipationRole.RELEASE_AUTHOR,
            effective_from=_text(value.get("released_at")),
        )
        commit_id = _text(_mapping(value.get("commit")).get("id"))
        references = (
            [self._git_reference("gitlab_release_commit", commit_id, "commit")] if commit_id else []
        )
        references.append(self._git_reference("gitlab_release_tag", tag_name, "ref"))
        return build_record(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            object_type="release",
            external_id=f"{self._project_id}:{tag_name}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=_text(value.get("released_at")) or _text(value.get("created_at")),
            payload={
                "project_id": self._project_id,
                "tag_name": tag_name,
                "name": _text(value.get("name")),
                "description": _text(value.get("description")),
                "created_at": _text(value.get("created_at")),
                "released_at": _text(value.get("released_at")),
                "commit_id": commit_id,
            },
            redactor=self._redactor,
            participations=(author,) if author else (),
            references=references,
            untrusted_text_fields=("name", "description"),
        )

    def _deployment(self, value: Mapping[str, object], observed_at: str) -> NormalizedRecord:
        deployment_id = _identifier(value.get("id"))
        if deployment_id is None:
            raise PermanentSourceError("GitLab deployment omitted its id")
        deployable = _mapping(value.get("deployable"))
        deployer = self._actor_participation(
            value.get("user") or deployable.get("user"),
            ParticipationRole.DEPLOYER,
            effective_from=_text(value.get("created_at")),
        )
        sha = _text(value.get("sha"))
        references = [self._git_reference("gitlab_deployment_commit", sha, "commit")] if sha else []
        environment = _mapping(value.get("environment"))
        environment_name = _text(value.get("environment_name")) or _text(environment.get("name"))
        return build_record(
            source_kind="gitlab",
            source_instance=self._config.source_instance,
            object_type="deployment",
            external_id=f"{self._project_id}:{deployment_id}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=_text(value.get("updated_at")) or _text(value.get("finished_at")),
            payload={
                "project_id": self._project_id,
                "id": deployment_id,
                "iid": _identifier(value.get("iid")),
                "status": _text(value.get("status")),
                "sha": sha,
                "ref": _text(value.get("ref")),
                "created_at": _text(value.get("created_at")),
                "updated_at": _text(value.get("updated_at")),
                "finished_at": _text(value.get("finished_at")),
                "environment_name": environment_name,
                "environment": {
                    "id": _identifier(environment.get("id")),
                    "name": _text(environment.get("name")),
                    "slug": _text(environment.get("slug")),
                    "tier": _text(environment.get("tier")),
                },
            },
            redactor=self._redactor,
            participations=(deployer,) if deployer else (),
            references=references,
            untrusted_text_fields=("ref", "environment_name", "environment.name"),
        )

    @staticmethod
    def _git_reference(reference_type: str, external_id: str, object_type: str) -> Reference:
        return Reference(
            reference_type=reference_type,
            target_external_id=external_id,
            strength=ReferenceStrength.STRUCTURED,
            target_source_kind="git",
            target_object_type=object_type,
        )
