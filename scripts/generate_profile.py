#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILE_JSON_PATH = ROOT / "profile" / "profile.json"
PORTRAIT_PATH = ROOT / "profile" / "portrait.txt"
STATS_CACHE_PATH = ROOT / "profile" / "github_stats_cache.json"
ASSETS_DIR = ROOT / "assets"

FONT_FAMILY = (
    "Consolas, 'Liberation Mono', Menlo, Monaco, "
    "'Courier New', monospace"
)
FONT_SIZE = 16
LINE_HEIGHT = 20
CHAR_WIDTH = FONT_SIZE * 0.60

PADDING_X = 18
TOP_BAR_HEIGHT = 48
BODY_TOP = 76
BOTTOM_PADDING = 24
GAP_X = 36
MIN_WIDTH = 980
MIN_RIGHT_BLOCK_CHARS = 60

THEMES = {
    "dark": {
        "page_bg": "#0d1117",
        "card_bg": "#161b22",
        "card_stroke": "#30363d",
        "text": "#c9d1d9",
        "muted": "#8b949e",
        "key": "#ffa657",
        "value": "#a5d6ff",
        "cc": "#616e7f",
        "add": "#3fb950",
        "delete": "#f85149",
        "dot1": "#ff5f56",
        "dot2": "#ffbd2e",
        "dot3": "#27c93f",
    },
    "light": {
        "page_bg": "#ffffff",
        "card_bg": "#f6f8fa",
        "card_stroke": "#d0d7de",
        "text": "#24292f",
        "muted": "#57606a",
        "key": "#9a6700",
        "value": "#0969da",
        "cc": "#6e7781",
        "add": "#1a7f37",
        "delete": "#cf222e",
        "dot1": "#ff5f56",
        "dot2": "#ffbd2e",
        "dot3": "#27c93f",
    },
}


@dataclass(frozen=True)
class Segment:
    text: str
    css_class: str | None = None
    element_id: str | None = None


@dataclass(frozen=True)
class GithubStats:
    repos: int
    contributed: int
    stars: int
    commits: int
    followers: int
    loc_add: int
    loc_del: int

    @property
    def loc_net(self) -> int:
        return self.loc_add - self.loc_del


class GithubClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "kitnew-profile-generator",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub GraphQL request failed with HTTP {exc.code}: {error_body}"
            ) from exc

        if body.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
        return body["data"]


def escape_xml(value: object) -> str:
    return html.escape(str(value), quote=False)


def format_number(value: int) -> str:
    return f"{value:,}"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def read_portrait(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Portrait file not found: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    while lines and not lines[-1]:
        lines.pop()

    if not lines:
        raise ValueError("portrait.txt is empty")
    if any("\t" in line for line in lines):
        raise ValueError("portrait.txt must use spaces, not tabs")

    # Preserve leading spaces. Trailing spaces are unnecessary for SVG rendering
    # and would incorrectly increase the calculated portrait width.
    return [line.rstrip() for line in lines]


def load_cache(path: Path, username: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "username": username, "repositories": {}}

    try:
        cache = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "username": username, "repositories": {}}

    if cache.get("version") != 1 or cache.get("username") != username:
        return {"version": 1, "username": username, "repositories": {}}
    if not isinstance(cache.get("repositories"), dict):
        cache["repositories"] = {}
    return cache


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_repository_inventory(
    client: GithubClient,
    username: str,
) -> tuple[str, int, int, int, list[dict[str, Any]]]:
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        id
        followers { totalCount }
        owned: repositories(first: 1, ownerAffiliations: [OWNER]) {
          totalCount
        }
        repositories(
          first: 100
          after: $cursor
          ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          totalCount
          nodes {
            nameWithOwner
            owner { login }
            name
            stargazers { totalCount }
            defaultBranchRef {
              target {
                ... on Commit {
                  oid
                  history { totalCount }
                }
              }
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """

    cursor: str | None = None
    repositories: list[dict[str, Any]] = []
    owner_id = ""
    followers = 0
    owned_count = 0
    contributed_count = 0

    while True:
        data = client.graphql(query, {"login": username, "cursor": cursor})
        user = data.get("user")
        if user is None:
            raise RuntimeError(f"GitHub user does not exist: {username}")

        owner_id = user["id"]
        followers = int(user["followers"]["totalCount"])
        owned_count = int(user["owned"]["totalCount"])
        connection = user["repositories"]
        contributed_count = int(connection["totalCount"])
        repositories.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return owner_id, followers, owned_count, contributed_count, repositories


def fetch_authored_repo_stats(
    client: GithubClient,
    owner: str,
    name: str,
    user_id: str,
) -> tuple[int, int, int]:
    query = """
    query($owner: String!, $name: String!, $userId: ID!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor, author: {id: $userId}) {
                totalCount
                nodes {
                  additions
                  deletions
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    cursor: str | None = None
    commit_count = 0
    additions = 0
    deletions = 0
    first_page = True

    while True:
        data = client.graphql(
            query,
            {
                "owner": owner,
                "name": name,
                "userId": user_id,
                "cursor": cursor,
            },
        )
        repository = data.get("repository")
        if repository is None or repository.get("defaultBranchRef") is None:
            return 0, 0, 0

        target = repository["defaultBranchRef"].get("target")
        if not target or "history" not in target:
            return 0, 0, 0

        history = target["history"]
        if first_page:
            commit_count = int(history["totalCount"])
            first_page = False

        for commit in history.get("nodes") or []:
            additions += int(commit["additions"])
            deletions += int(commit["deletions"])

        page_info = history["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return commit_count, additions, deletions


def fetch_github_stats(client: GithubClient, username: str) -> GithubStats:
    user_id, followers, repos, contributed, repositories = fetch_repository_inventory(
        client, username
    )
    cache = load_cache(STATS_CACHE_PATH, username)
    old_repo_cache = cache["repositories"]
    new_repo_cache: dict[str, Any] = {}

    stars = 0
    total_commits = 0
    total_additions = 0
    total_deletions = 0

    for repository in repositories:
        name_with_owner = repository["nameWithOwner"]
        repo_owner = repository["owner"]["login"]
        repo_name = repository["name"]
        if repo_owner.casefold() == username.casefold():
            stars += int(repository["stargazers"]["totalCount"])

        default_branch = repository.get("defaultBranchRef")
        target = default_branch.get("target") if default_branch else None
        head_oid = target.get("oid") if target else None
        branch_commit_count = (
            int(target["history"]["totalCount"])
            if target and target.get("history")
            else 0
        )

        cached = old_repo_cache.get(name_with_owner)
        cache_is_current = (
            isinstance(cached, dict)
            and cached.get("head_oid") == head_oid
            and cached.get("branch_commit_count") == branch_commit_count
        )

        if cache_is_current:
            authored_commits = int(cached.get("authored_commits", 0))
            additions = int(cached.get("additions", 0))
            deletions = int(cached.get("deletions", 0))
        elif head_oid is None:
            authored_commits = additions = deletions = 0
        else:
            authored_commits, additions, deletions = fetch_authored_repo_stats(
                client,
                repo_owner,
                repo_name,
                user_id,
            )
            print(
                f"Updated cache for {name_with_owner}: "
                f"{authored_commits} commits, +{additions}, -{deletions}"
            )

        new_repo_cache[name_with_owner] = {
            "head_oid": head_oid,
            "branch_commit_count": branch_commit_count,
            "authored_commits": authored_commits,
            "additions": additions,
            "deletions": deletions,
        }
        total_commits += authored_commits
        total_additions += additions
        total_deletions += deletions

    cache["repositories"] = new_repo_cache
    save_cache(STATS_CACHE_PATH, cache)

    return GithubStats(
        repos=repos,
        contributed=contributed,
        stars=stars,
        commits=total_commits,
        followers=followers,
        loc_add=total_additions,
        loc_del=total_deletions,
    )


def right_metric_segments(
    key: object,
    value: object,
    width_chars: int,
) -> list[Segment]:
    """Render a profile field with its value aligned to the right edge."""
    key_text = str(key)
    value_text = format_number(value) if isinstance(value, int) else str(value)

    prefix_length = len(". ") + len(key_text) + len(":")
    gap_length = max(
        1,
        width_chars - prefix_length - len(value_text),
    )

    if gap_length == 1:
        filler = " "
    elif gap_length == 2:
        filler = ". "
    else:
        filler = " " + ("." * (gap_length - 2)) + " "

    return [
        Segment(". ", "cc"),
        Segment(key_text, "key"),
        Segment(":"),
        Segment(filler, "cc"),
        Segment(value_text, "value"),
    ]


def justify_dots(value: object, target_length: int) -> str:
    """Match the compact dotted spacing used by the reference profile."""
    formatted = format_number(value) if isinstance(value, int) else str(value)
    remaining = max(0, target_length - len(formatted))
    if remaining == 0:
        return ""
    if remaining == 1:
        return " "
    if remaining == 2:
        return ". "
    return " " + ("." * remaining) + " "


def github_stats_lines(stats: GithubStats) -> list[list[Segment]]:
    repos = format_number(stats.repos)
    contributed = format_number(stats.contributed)
    stars = format_number(stats.stars)
    commits = format_number(stats.commits)
    followers = format_number(stats.followers)
    loc_net = format_number(stats.loc_net)
    loc_add = format_number(stats.loc_add)
    loc_del = format_number(stats.loc_del)

    return [
        [
            Segment(". ", "cc"),
            Segment("Repos", "key"),
            Segment(":"),
            Segment(justify_dots(stats.repos, 6), "cc", "repo_data_dots"),
            Segment(repos, "value", "repo_data"),
            Segment(" {"),
            Segment("Contributed", "key"),
            Segment(": "),
            Segment(contributed, "value", "contrib_data"),
            Segment("} | "),
            Segment("Stars", "key"),
            Segment(":"),
            Segment(justify_dots(stats.stars, 14), "cc", "star_data_dots"),
            Segment(stars, "value", "star_data"),
        ],
        [
            Segment(". ", "cc"),
            Segment("Commits", "key"),
            Segment(":"),
            Segment(justify_dots(stats.commits, 22), "cc", "commit_data_dots"),
            Segment(commits, "value", "commit_data"),
            Segment(" | "),
            Segment("Followers", "key"),
            Segment(":"),
            Segment(justify_dots(stats.followers, 10), "cc", "follower_data_dots"),
            Segment(followers, "value", "follower_data"),
        ],
        [
            Segment(". ", "cc"),
            Segment("Lines of Code on GitHub", "key"),
            Segment(":"),
            Segment(justify_dots(stats.loc_net, 9), "cc", "loc_data_dots"),
            Segment(loc_net, "value", "loc_data"),
            Segment(" ( "),
            Segment(loc_add, "addColor", "loc_add"),
            Segment("++", "addColor"),
            Segment(", "),
            Segment(justify_dots(stats.loc_del, 7), None, "loc_del_dots"),
            Segment(loc_del, "delColor", "loc_del"),
            Segment("--", "delColor"),
            Segment(" )"),
        ],
    ]


def plain_length(segments: list[Segment]) -> int:
    return sum(len(segment.text) for segment in segments)


def section_header(title: str, width_chars: int) -> list[Segment]:
    prefix = f"- {title} "
    return [Segment(prefix + ("-" * max(0, width_chars - len(prefix))))]


def build_line_model(
    profile: dict[str, Any],
    stats: GithubStats,
) -> tuple[list[list[Segment] | None], int]:
    terminal_user = str(profile["terminal_user"])
    static_sections = profile.get("sections", [])
    stats_rows = github_stats_lines(stats)

    candidate_widths = [MIN_RIGHT_BLOCK_CHARS, len(terminal_user)]
    for section in static_sections:
        candidate_widths.append(len(f"- {section['title']} "))
        for key, value in section.get("items", []):
            value_text = format_number(value) if isinstance(value, int) else str(value)
            candidate_widths.append(
                len(". ") + len(str(key)) + len(": ") + len(value_text)
            )
    candidate_widths.extend(plain_length(row) for row in stats_rows)
    right_width_chars = max(candidate_widths)

    lines: list[list[Segment] | None] = [
        [Segment(terminal_user)],
        [Segment("-" * right_width_chars, "cc")],
        None,
    ]

    for index, section in enumerate(static_sections):
        lines.append(section_header(str(section["title"]), right_width_chars))
        for key, value in section.get("items", []):
            lines.append(right_metric_segments(key, value, right_width_chars))
        if index != len(static_sections) - 1:
            lines.append(None)

    if static_sections:
        lines.append(None)
    lines.append(section_header("GitHub Stats", right_width_chars))
    lines.extend(stats_rows)

    return lines, right_width_chars


def svg_tspan(
    segment: Segment,
    *,
    x: float | None = None,
    y: float | None = None,
) -> str:
    attributes: list[str] = []
    if x is not None:
        attributes.append(f'x="{x:.1f}"')
    if y is not None:
        attributes.append(f'y="{y:.1f}"')
    if segment.css_class:
        attributes.append(f'class="{segment.css_class}"')
    if segment.element_id:
        attributes.append(f'id="{segment.element_id}"')

    attribute_text = " " + " ".join(attributes) if attributes else ""
    return f"<tspan{attribute_text}>{escape_xml(segment.text)}</tspan>"


def render_info_block(
    lines: list[list[Segment] | None],
    x: float,
    start_y: float,
) -> str:
    rendered: list[str] = []
    for index, line in enumerate(lines):
        y = start_y + index * LINE_HEIGHT
        if line is None:
            rendered.append(svg_tspan(Segment(" "), x=x, y=y))
            continue

        first, *rest = line
        parts = [svg_tspan(first, x=x, y=y)]
        parts.extend(svg_tspan(segment) for segment in rest)
        rendered.append("".join(parts))
    return "\n".join(rendered)


def render_portrait(lines: list[str], x: float, start_y: float) -> str:
    return "\n".join(
        svg_tspan(Segment(line), x=x, y=start_y + index * LINE_HEIGHT)
        for index, line in enumerate(lines)
    )


def build_svg(
    theme_name: str,
    profile: dict[str, Any],
    portrait_lines: list[str],
    info_lines: list[list[Segment] | None],
    right_width_chars: int,
) -> str:
    theme = THEMES[theme_name]
    portrait_width_chars = max(len(line) for line in portrait_lines)
    portrait_width_px = portrait_width_chars * CHAR_WIDTH
    right_width_px = right_width_chars * CHAR_WIDTH

    portrait_x = PADDING_X
    info_x = portrait_x + portrait_width_px + GAP_X
    content_line_count = max(len(portrait_lines), len(info_lines))

    width = max(MIN_WIDTH, math.ceil(info_x + right_width_px + PADDING_X))
    height = math.ceil(BODY_TOP + content_line_count * LINE_HEIGHT + BOTTOM_PADDING)

    portrait_svg = render_portrait(portrait_lines, portrait_x, BODY_TOP)
    info_svg = render_info_block(info_lines, info_x, BODY_TOP)
    title = f"{profile['username']} / README.md"

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg
  xmlns="http://www.w3.org/2000/svg"
  width="{width}"
  height="{height}"
  viewBox="0 0 {width} {height}"
  font-family="{FONT_FAMILY}"
  font-size="{FONT_SIZE}"
  xml:space="preserve"
>
  <style>
    .text {{ fill: {theme['text']}; }}
    .muted {{ fill: {theme['muted']}; }}
    .key {{ fill: {theme['key']}; }}
    .value {{ fill: {theme['value']}; }}
    .cc {{ fill: {theme['cc']}; }}
    .addColor {{ fill: {theme['add']}; }}
    .delColor {{ fill: {theme['delete']}; }}
    text, tspan {{
      white-space: pre;
      text-anchor: start;
    }}
  </style>

  <rect width="{width}" height="{height}" rx="16" fill="{theme['page_bg']}"/>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="15"
        fill="{theme['card_bg']}" stroke="{theme['card_stroke']}"/>
  <line x1="1" y1="{TOP_BAR_HEIGHT}" x2="{width - 1}" y2="{TOP_BAR_HEIGHT}"
        stroke="{theme['card_stroke']}"/>

  <circle cx="24" cy="24" r="6" fill="{theme['dot1']}"/>
  <circle cx="44" cy="24" r="6" fill="{theme['dot2']}"/>
  <circle cx="64" cy="24" r="6" fill="{theme['dot3']}"/>

  <text class="muted" text-anchor="start">
    <tspan x="84" y="29">{escape_xml(title)}</tspan>
  </text>

  <text class="text" text-anchor="start">
{portrait_svg}
  </text>

  <text class="text" text-anchor="start">
{info_svg}
  </text>
</svg>
'''


def main() -> None:
    profile = read_json(PROFILE_JSON_PATH)
    portrait_lines = read_portrait(PORTRAIT_PATH)

    username = str(profile.get("username", "")).strip()
    terminal_user = str(profile.get("terminal_user", "")).strip()
    if not username or not terminal_user:
        raise ValueError("profile.json must contain non-empty username and terminal_user")

    token = os.getenv("GITHUB_TOKEN") or os.getenv("ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "Set GITHUB_TOKEN (or ACCESS_TOKEN) before running the generator."
        )

    client = GithubClient(token)
    stats = fetch_github_stats(client, username)
    info_lines, right_width_chars = build_line_model(profile, stats)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for theme_name in ("dark", "light"):
        output_path = ASSETS_DIR / f"{theme_name}_mode.svg"
        output_path.write_text(
            build_svg(
                theme_name,
                profile,
                portrait_lines,
                info_lines,
                right_width_chars,
            ),
            encoding="utf-8",
        )
        print(f"Generated {output_path}")

    print(f"Updated {STATS_CACHE_PATH}")


if __name__ == "__main__":
    main()